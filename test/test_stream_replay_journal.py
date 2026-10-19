"""Long running tasks retain every tool event across reconnects and ring eviction."""
import asyncio
import json
import unittest
from unittest.mock import patch

import factory.chat_factory as cf
from config import ChatLLMRequest
from factory.agent_runtime.stream_replay import StreamReplayJournal


def frame(value):
    return 'data: ' + json.dumps(value, ensure_ascii=False) + '\n\n'


class ReplayJournalTests(unittest.TestCase):
    def test_spills_to_disk_without_losing_unicode_or_independent_read_cursors(self):
        journal = StreamReplayJournal()
        try:
            chunks = [frame({'content': '中文😀' * 1000 + str(i)}) for i in range(150)]
            for chunk in chunks:
                journal.append(chunk)
            self.assertTrue(journal.file._rolled, 'large tasks must not accumulate unbounded RAM')
            for _ in range(2):
                offset = 0
                for chunk in chunks:
                    actual, offset = journal.read(offset)
                    self.assertEqual(actual, chunk)
                self.assertEqual(offset, journal.end)
        finally:
            journal.close()
        self.assertTrue(journal.file.closed)


class SessionReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ring_patch = patch.object(cf, '_SESSION_STREAM_MAX_BUFFER', 4)
        self.mode_patch = patch.object(cf, '_CHAT_WORKER_MODE', 'inline')
        self.ring_patch.start()
        self.mode_patch.start()
        self.sid = 'mock_full_replay_journal'
        self.stream = cf._set_session_stream(self.sid)
        self.stream.task = asyncio.create_task(asyncio.sleep(3600))
        self.stream.question_text = '检查长任务'
        self.stream.live_round_no = 3
        self.stream.emit(frame({'round_model': {'id': 'test'}}))
        self.stream.set_round_start()

    async def asyncTearDown(self):
        self.stream.task.cancel()
        await asyncio.gather(self.stream.task, return_exceptions=True)
        await self.stream.finish()
        self.stream._replay_journal.close()
        cf._SESSION_STREAMS.pop(self.sid, None)
        self.ring_patch.stop()
        self.mode_patch.stop()

    def subscribe(self):
        return cf.tool_chat_server(ChatLLMRequest(session_id=self.sid, messages=[]))

    async def read_snapshot(self, subscription):
        marker = json.loads((await anext(subscription))[6:])
        self.assertTrue(marker['replay_complete'])
        chunks = []
        async for chunk in subscription:
            if chunk == frame({'replay_end': True}):
                return chunks
            chunks.append(chunk)
        self.fail('missing replay_end')

    async def test_all_tools_replay_after_eviction_and_repeated_refresh_without_restarting_task(self):
        expected = []
        for i in range(80):
            self.stream.set_round_start()  # later model turns never move task start
            for event in ({'tool_calls': [{'id': str(i), 'function': {'name': 'read_file'}}]},
                          {'tool_call_id': str(i), 'tool_result': '结果' + str(i)}):
                chunk = frame(event)
                expected.append(chunk)
                self.stream.emit(chunk)
        self.assertEqual(len(self.stream.buffer), 4)
        original_task = self.stream.task
        for _ in range(3):
            subscription = self.subscribe()
            try:
                self.assertEqual(await self.read_snapshot(subscription), expected)
            finally:
                await subscription.aclose()
            self.assertFalse(self.stream._replay_journal.file.closed)
            self.assertIs(self.stream.task, original_task)
            self.assertTrue(self.stream.is_running())

    async def test_multiple_readers_hot_to_cold_transition_and_live_tail(self):
        first, second = self.subscribe(), self.subscribe()
        try:
            for subscriber in (first, second):
                marker = json.loads((await anext(subscriber))[6:])
                self.assertTrue(marker['replay_complete'])
            one = frame({'content': 'first'})
            self.stream.emit(one)
            self.assertEqual(await anext(first), one)  # consume while still hot
            rest = [frame({'content': 'delta' + str(i)}) for i in range(12)]
            for chunk in rest:
                self.stream.emit(chunk)
            for chunk in rest:
                self.assertEqual(await anext(first), chunk)
            for chunk in [one] + rest:
                self.assertEqual(await anext(second), chunk)
            self.assertEqual(await anext(first), frame({'replay_end': True}))
            self.assertEqual(await anext(second), frame({'replay_end': True}))
            await first.aclose()  # one page leaves; other page keeps following
            live = frame({'tool_result': 'live result'})
            self.stream.emit(live)
            self.assertEqual(await anext(second), live)
            await self.stream.finish()
            self.assertFalse(self.stream._replay_journal.file.closed)
            self.assertEqual(await anext(second), 'data: [DONE]\n\n')
        finally:
            await first.aclose()
            await second.aclose()
        self.assertTrue(self.stream._replay_journal.file.closed)

    async def test_storage_failure_keeps_generation_and_exposes_incomplete_replay(self):
        with patch.object(self.stream._replay_journal, 'append', side_effect=OSError('disk unavailable')):
            self.stream.emit(frame({'content': 'still running'}))
        self.assertTrue(self.stream.is_running())
        for i in range(8):
            self.stream.emit(frame({'content': str(i)}))
        subscription = self.subscribe()
        try:
            marker = json.loads((await anext(subscription))[6:])
            self.assertFalse(marker['replay_complete'])
            warning = json.loads((await anext(subscription))[6:])
            self.assertEqual(warning['warning']['code'], 'STREAM_REPLAY_GAP')
            for chunk in self.stream.buffer:
                self.assertEqual(await anext(subscription), chunk)
        finally:
            await subscription.aclose()

    async def test_read_failure_falls_back_without_cancelling_generation(self):
        for i in range(8):
            self.stream.emit(frame({'content': str(i)}))
        subscription = self.subscribe()
        try:
            await anext(subscription)
            with patch.object(self.stream._replay_journal, 'read', side_effect=OSError('read error')):
                warning = json.loads((await anext(subscription))[6:])
            self.assertEqual(warning['warning']['code'], 'STREAM_REPLAY_GAP')
            self.assertTrue(self.stream.is_running())
        finally:
            await subscription.aclose()

    async def test_permission_resume_keeps_saved_prefix_and_still_replays_entire_resume_segment(self):
        self.stream._replay_has_history_prefix = True
        expected = [frame({'content': str(i)}) for i in range(12)]
        for chunk in expected:
            self.stream.emit(chunk)
        subscription = self.subscribe()
        try:
            marker = json.loads((await anext(subscription))[6:])
            self.assertFalse(marker['replay_complete'])
            for chunk in expected:
                self.assertEqual(await anext(subscription), chunk)
        finally:
            await subscription.aclose()

    async def test_finished_task_without_any_viewer_releases_transient_storage(self):
        self.stream.emit(frame({'content': 'finished while disconnected'}))
        await self.stream.finish()
        self.assertTrue(self.stream._replay_journal.file.closed)
