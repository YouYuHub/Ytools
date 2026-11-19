# coding: utf-8
"""附接回放吞吐优化：子任务 delta 合并 + 追赶期批量下发。

长任务切回会话时"一点点加载十几秒"的根因是下发节奏：每帧一次 yield
（= 一次 ASGI send = 浏览器一次 reader.read() 唤醒 + 一次绘制机会），
子任务 delta 作为结构帧 1:1 透传占帧数大头。本套用例守护：
- 合并只发生在同 agent/seq/通道的连续 delta，语义等价于逐帧追加；
- 其余结构帧原字节透传，顺序与分块边界不变；
- 批量下发不丢帧、不改顺序，replay_end 必须排在全部回放帧之后。
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import factory.chat_factory as cf
from config import ChatLLMRequest
from factory.agent_runtime.stream_replay import (
    COMPACT_MERGED_FRAME_CHARS,
    compact_replay_chunks,
)


def frame(value):
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


def payload(chunk):
    return json.loads(chunk.split('data:', 1)[1].strip())


def payloads(chunks):
    out = []
    for chunk in chunks:
        for line in chunk.split('\n'):
            line = line.strip()
            if line.startswith('data:'):
                out.append(json.loads(line[5:].strip()))
    return out


def sub_delta(agent='a1', seq=1, channel='content_delta', text='x', **extra):
    return {'event': 'sub_agent', 'phase': 'delta', 'agent_id': agent, 'seq': seq, channel: text, **extra}


class SubAgentDeltaCompactTests(unittest.TestCase):
    def test_same_agent_seq_channel_merges_in_order(self):
        chunks = [frame(sub_delta(text='一')), frame(sub_delta(text='二')), frame(sub_delta(text='三'))]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 1)
        merged = payload(out[0])
        self.assertEqual(merged['content_delta'], '一二三')
        self.assertEqual(merged['agent_id'], 'a1')
        self.assertEqual(merged['seq'], 1)
        self.assertEqual(merged['phase'], 'delta')

    def test_reasoning_delta_merges_separately_from_content(self):
        chunks = [frame(sub_delta(channel='reasoning_delta', text='想1')),
                  frame(sub_delta(channel='reasoning_delta', text='想2')),
                  frame(sub_delta(text='正文'))]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 2)
        self.assertEqual(payload(out[0])['reasoning_delta'], '想1想2')
        self.assertEqual(payload(out[1])['content_delta'], '正文')

    def test_agent_and_seq_boundaries_break_runs(self):
        chunks = [frame(sub_delta(agent='a1', text='A')),
                  frame(sub_delta(agent='a2', text='B')),
                  frame(sub_delta(agent='a2', seq=2, text='C')),
                  frame(sub_delta(agent='a2', seq=2, text='D'))]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], chunks[0])  # 单帧 run 原字节透传
        self.assertEqual(out[1], chunks[1])
        self.assertEqual(payload(out[2])['content_delta'], 'CD')

    def test_structural_sub_phases_pass_through_verbatim(self):
        start = frame({'event': 'sub_agent', 'phase': 'start', 'agent_id': 'a1', 'task': '目标'})
        call = frame({'event': 'sub_agent', 'phase': 'model_call', 'agent_id': 'a1', 'seq': 1, 'content': '整轮'})
        done = frame({'event': 'sub_agent', 'phase': 'done', 'agent_id': 'a1', 'status': 'done'})
        chunks = [frame(sub_delta(text='一')), start, frame(sub_delta(text='二')), call, done]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[1], start)
        self.assertEqual(out[3], call)
        self.assertEqual(out[4], done)

    def test_delta_with_extra_keys_stays_structural(self):
        chunk = frame(sub_delta(text='x', usage={'total_tokens': 3}))
        self.assertEqual(compact_replay_chunks([chunk, chunk]), [chunk, chunk])

    def test_dual_channel_delta_not_merged(self):
        chunk = frame(sub_delta(text='正文', reasoning_delta='思考'))
        self.assertEqual(compact_replay_chunks([chunk, chunk]), [chunk, chunk])

    def test_missing_agent_id_not_merged(self):
        chunk = frame({'event': 'sub_agent', 'phase': 'delta', 'seq': 1, 'content_delta': 'x'})
        self.assertEqual(compact_replay_chunks([chunk, chunk]), [chunk, chunk])

    def test_empty_delta_passes_through_keeps_side_effects(self):
        # 空增量在前端仍触发 autoOpen 等副作用，不能像顶层空分片那样丢弃
        chunk = frame(sub_delta(text=''))
        self.assertEqual(compact_replay_chunks([chunk]), [chunk])

    def test_cap_splits_giant_sub_delta_run(self):
        big = 'x' * (COMPACT_MERGED_FRAME_CHARS + 100)
        out = compact_replay_chunks([frame(sub_delta(text=big[:1000])), frame(sub_delta(text=big[1000:]))])
        self.assertEqual(len(out), 2)
        self.assertEqual(''.join(payload(c)['content_delta'] for c in out), big)

    def test_timestamp_of_last_frame_kept(self):
        chunks = [frame(sub_delta(text='一', timestamp='2026-11-19 10:00:00')),
                  frame(sub_delta(text='二', timestamp='2026-11-19 10:00:05'))]
        out = compact_replay_chunks(chunks)
        self.assertEqual(payload(out[0])['timestamp'], '2026-11-19 10:00:05')

    def test_top_level_text_merge_unchanged(self):
        chunks = [frame({'content': 'a'}), frame({'content': 'b'}),
                  frame(sub_delta(text='子')), frame({'content': 'c'})]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 3)
        self.assertEqual(payload(out[0])['content'], 'ab')
        self.assertEqual(payload(out[1])['content_delta'], '子')
        self.assertEqual(payload(out[2])['content'], 'c')

    def test_equivalence_of_accumulated_text(self):
        """合并后按序累积的文本 == 逐帧累积（前端 appendLiveDelta 口径）。"""
        raw = [sub_delta(agent='a1', seq=1, text='A'),
               sub_delta(agent='a1', seq=1, channel='reasoning_delta', text='R'),
               sub_delta(agent='a1', seq=2, text='B'),
               sub_delta(agent='a2', seq=1, text='C')]
        def accumulate(events):
            acc = {}
            for evt in events:
                key = (evt['agent_id'], evt['seq'])
                for channel in ('content_delta', 'reasoning_delta'):
                    if evt.get(channel):
                        acc.setdefault(key, {}).setdefault(channel, '')
                        acc[key][channel] += evt[channel]
            return acc
        self.assertEqual(accumulate(payloads(compact_replay_chunks([frame(e) for e in raw]))),
                         accumulate(raw))


class BatchSseFramesTests(unittest.TestCase):
    def test_concatenates_without_changing_frames_or_order(self):
        frames = [frame({'content': str(i)}) for i in range(50)]
        batches = cf._batch_sse_frames(frames)
        self.assertLess(len(batches), len(frames))
        self.assertEqual(''.join(batches), ''.join(frames))
        self.assertEqual(payloads(batches), payloads(frames))

    def test_budget_splits_into_multiple_batches(self):
        one = frame({'content': 'x' * 1000})
        count = cf._SSE_REPLAY_BATCH_CHARS // len(one) + 3
        batches = cf._batch_sse_frames([one] * count)
        self.assertGreaterEqual(len(batches), 2)
        self.assertEqual(''.join(batches), one * count)

    def test_single_frame_and_empty(self):
        self.assertEqual(cf._batch_sse_frames([]), [])
        one = frame({'content': 'a'})
        self.assertEqual(cf._batch_sse_frames([one]), [one])

    def test_unparseable_frames_preserved(self):
        raw = ['data: {bad}\n\n', ': ping\n\n', frame({'content': 'ok'})]
        self.assertEqual(''.join(cf._batch_sse_frames(raw)), ''.join(raw))


class AttachBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode_patch = patch.object(cf, '_CHAT_WORKER_MODE', 'inline')
        self.mode_patch.start()
        self.sid = 'mock_batch_replay'
        self.stream = cf._set_session_stream(self.sid)
        self.stream.task = asyncio.create_task(asyncio.sleep(3600))
        self.stream.question_text = '批量回放'
        self.stream.live_round_no = 1
        self.stream.set_round_start()

    async def asyncTearDown(self):
        self.stream.task.cancel()
        await asyncio.gather(self.stream.task, return_exceptions=True)
        await self.stream.finish()
        self.stream._replay_journal.close()
        cf._SESSION_STREAMS.pop(self.sid, None)
        self.mode_patch.stop()

    def subscribe(self):
        return cf.tool_chat_server(ChatLLMRequest(session_id=self.sid, messages=[]))

    async def drain_until_replay_end(self, subscription):
        chunks = []
        async for chunk in subscription:
            chunks.append(chunk)
            if '"replay_end"' in chunk:
                break
        return chunks

    async def test_long_backlog_arrives_in_few_chunks_without_losing_frames(self):
        events = []
        for r in range(6):
            for i in range(30):
                events.append({'content': f'正文{r}-{i} '})
            for i in range(40):
                events.append(sub_delta(agent=f'a{r}', seq=1, text=f'子正文{i} '))
            events.append({'event': 'sub_agent', 'phase': 'done', 'agent_id': f'a{r}', 'status': 'done'})
        for evt in events:
            self.stream.emit(frame(evt))
        subscription = self.subscribe()
        try:
            marker = json.loads((await anext(subscription))[6:])
            self.assertTrue(marker['replay'])
            chunks = await self.drain_until_replay_end(subscription)
            got = payloads(chunks[:-1])
            # 帧数远小于原始事件数，且 chunk 数远小于帧数
            self.assertLess(len(got), len(events))
            self.assertLess(len(chunks), len(got))
            # 内容零丢失：顶层正文与子任务正文按序累积一致
            self.assertEqual(''.join(e.get('content', '') for e in got),
                             ''.join(e.get('content', '') for e in events))
            for r in range(6):
                self.assertEqual(''.join(e.get('content_delta', '') for e in got if e.get('agent_id') == f'a{r}'),
                                 ''.join(e.get('content_delta', '') for e in events if e.get('agent_id') == f'a{r}'))
            self.assertTrue(payloads([chunks[-1]])[0]['replay_end'])
        finally:
            await subscription.aclose()

    async def test_replay_end_never_precedes_remaining_backlog(self):
        # 积压超过单批预算：replay_end 必须等全部回放帧下发后才出现。
        # 用结构帧（不可合并）构造积压，避免连续正文先被紧凑成单帧。
        big = frame({'event': 'sub_agent', 'phase': 'done', 'agent_id': 'a',
                     'status': 'done', 'final_reply': 'x' * 4000})
        count = cf._SSE_REPLAY_BATCH_CHARS // len(big) + 5
        for _ in range(count):
            self.stream.emit(big)
        subscription = self.subscribe()
        try:
            await anext(subscription)  # marker
            chunks = await self.drain_until_replay_end(subscription)
            self.assertGreaterEqual(len(chunks), 3)  # 至少两批 + replay_end
            self.assertTrue(payloads([chunks[-1]])[0]['replay_end'])
            got = payloads(chunks[:-1])
            self.assertEqual(len(got), count)
            self.assertEqual(''.join(e['final_reply'] for e in got), 'x' * 4000 * count)
            for chunk in chunks[:-1]:
                self.assertNotIn('replay_end', chunk)
        finally:
            await subscription.aclose()

    async def test_live_single_frame_still_passes_through_immediately(self):
        self.stream.emit(frame({'content': '历史'}))
        subscription = self.subscribe()
        try:
            await anext(subscription)
            await self.drain_until_replay_end(subscription)
            live = frame({'content': '实时首字'})
            self.stream.emit(live)
            # 实时只有一帧：批量等价于直通，首字延迟不变
            self.assertEqual(await anext(subscription), live)
        finally:
            await subscription.aclose()


if __name__ == '__main__':
    unittest.main()
