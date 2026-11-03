# coding: utf-8
"""附接回放紧凑快照：连续分片合并为少帧，渲染语义与逐帧回放一致。

覆盖：纯函数合并规则、与 merge_tool_call_delta 的等价性、
附接链路（marker → 紧凑帧 → replay_end → live 直通）。
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import factory.chat_factory as cf
from config import ChatLLMRequest
from factory.agent_runtime.chat_runtime import merge_tool_call_delta
from factory.agent_runtime.stream_replay import (
    COMPACT_MERGED_FRAME_CHARS,
    compact_replay_chunks,
)


def frame(value):
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


def payload(chunk):
    return json.loads(chunk.split('data:', 1)[1].strip())


class CompactPureTests(unittest.TestCase):
    def test_content_run_merges_and_keeps_last_id(self):
        chunks = [frame({'id': 'c1', 'content': 'a'}),
                  frame({'id': 'c1', 'content': 'b'}),
                  frame({'content': 'c'})]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 1)
        self.assertEqual(payload(out[0])['content'], 'abc')

    def test_reasoning_run_merges(self):
        chunks = [frame({'reasoning_content': '想'}),
                  frame({'reasoning_content': '想想'})]
        out = compact_replay_chunks(chunks)
        self.assertEqual([payload(c)['reasoning_content'] for c in out], ['想想想'])

    def test_tool_calls_run_merges_by_index(self):
        chunks = [
            frame({'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'read', 'arguments': '{"p":'}},
                                  {'index': 1, 'id': 'b', 'function': {'name': 'list', 'arguments': '{"d":'}}]}),
            frame({'tool_calls': [{'index': 0, 'function': {'arguments': '1}'}},
                                  {'index': 1, 'function': {'arguments': '2}'}}]}),
        ]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 1)
        calls = {c['id']: c['function'] for c in payload(out[0])['tool_calls']}
        self.assertEqual(calls['a'], {'name': 'read', 'arguments': '{"p":1}'})
        self.assertEqual(calls['b'], {'name': 'list', 'arguments': '{"d":2}'})

    def test_structural_frames_break_runs_and_pass_through_verbatim(self):
        ret = frame({'tool_return': {'function_name': 'read', 'result': 'ok'}})
        chunks = [frame({'content': 'a'}), frame({'content': 'b'}),
                  ret,
                  frame({'content': 'c'}), frame({'content': 'd'}),
                  frame({'usage': {'total_tokens': 5}, 'id': 'u1'})]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 4)
        self.assertEqual(payload(out[0])['content'], 'ab')
        self.assertEqual(out[1], ret)  # 原字节透传
        self.assertEqual(payload(out[2])['content'], 'cd')
        self.assertEqual(out[3], chunks[5])

    def test_single_frame_runs_pass_through_verbatim(self):
        chunk = frame({'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 'x', 'arguments': '{}'}}]})
        self.assertEqual(compact_replay_chunks([chunk]), [chunk])

    def test_empty_shards_dropped_without_breaking_runs(self):
        chunks = [frame({'content': 'a'}), frame({'content': ''}),
                  frame({'id': 'c1', 'content': ''}), frame({'content': 'b'})]
        out = compact_replay_chunks(chunks)
        self.assertEqual(len(out), 1)
        self.assertEqual(payload(out[0])['content'], 'ab')

    def test_unparseable_chunks_pass_through(self):
        bad = 'data: {not json}\n\n'
        ping = ': ping\n\n'
        self.assertEqual(compact_replay_chunks([bad, ping]), [bad, ping])

    def test_content_with_usage_stays_structural(self):
        chunk = frame({'content': 'x', 'usage': {'total_tokens': 1}})
        self.assertEqual(compact_replay_chunks([chunk]), [chunk])

    def test_cap_splits_giant_runs(self):
        big = 'x' * (COMPACT_MERGED_FRAME_CHARS + 100)
        out = compact_replay_chunks([frame({'content': big[:1000]}),
                                     frame({'content': big[1000:]})])
        self.assertEqual(len(out), 2)
        self.assertEqual(''.join(payload(c)['content'] for c in out), big)

    def test_empty_input(self):
        self.assertEqual(compact_replay_chunks([]), [])

    def test_equivalence_with_merge_tool_call_delta(self):
        raw = [
            {'tool_calls': [{'index': 0, 'id': 'a', 'function': {'name': 're', 'arguments': 'ab'}}]},
            {'tool_calls': [{'index': 0, 'function': {'name': 'ad', 'arguments': 'cd'}}]},
        ]
        chunks = [frame(event) for event in raw]
        expected, actual = [], []
        for event in raw:
            merge_tool_call_delta(expected, event['tool_calls'])
        for chunk in compact_replay_chunks(chunks):
            merge_tool_call_delta(actual, payload(chunk)['tool_calls'])
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]['function'], {'name': 'read', 'arguments': 'abcd'})


class CompactAttachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mode_patch = patch.object(cf, '_CHAT_WORKER_MODE', 'inline')
        self.mode_patch.start()
        self.sid = 'mock_compact_replay'
        self.stream = cf._set_session_stream(self.sid)
        self.stream.task = asyncio.create_task(asyncio.sleep(3600))
        self.stream.question_text = '紧凑快照'
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

    async def test_attach_receives_compacted_snapshot_then_live_passthrough(self):
        for i in range(30):
            self.stream.emit(frame({'id': 'c1', 'content': '正' + str(i)}))
        for i in range(10):
            self.stream.emit(frame({'tool_calls': [{'index': 0, 'id': 't1', 'function': {'arguments': 'a' + str(i)}}]}))
        ret = frame({'tool_return': {'function_name': 'f', 'tool_call_id': 't1', 'result': 'ok'}})
        self.stream.emit(ret)
        subscription = self.subscribe()
        try:
            marker = json.loads((await anext(subscription))[6:])
            self.assertTrue(marker['replay'])
            got = []
            async for chunk in subscription:
                if chunk == frame({'replay_end': True}):
                    break
                got.append(chunk)
            # 30 正文 + 10 参数分片 + 1 结果 → 3 帧
            self.assertEqual(len(got), 3)
            self.assertEqual(payload(got[0])['content'], ''.join('正' + str(i) for i in range(30)))
            merged_calls = payload(got[1])['tool_calls']
            self.assertEqual(len(merged_calls), 1)
            self.assertEqual(merged_calls[0]['function']['arguments'], ''.join('a' + str(i) for i in range(10)))
            self.assertEqual(got[2], ret)
            # 排空后新帧走 live 原样直通
            live = frame({'content': 'live'})
            self.stream.emit(live)
            self.assertEqual(await anext(subscription), live)
        finally:
            await subscription.aclose()
