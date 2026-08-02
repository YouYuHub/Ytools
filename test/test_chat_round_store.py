import unittest

from memory.chat_round_store import ChatRoundStore


class ChatRoundStoreTests(unittest.TestCase):
    def test_round_lifecycle_records_question_usage_and_finalize(self):
        store = ChatRoundStore("demo")

        self.assertIsNone(store.record_message({"role": "user", "content": "hello"}))
        self.assertIsNone(store.record_message({"role": "assistant", "content": "hi"}))
        self.assertEqual(
            store.add_usage({"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}, completion_count=1),
            "记录成功",
        )

        round_record = store.record_message({"role": "assistant", "done": "[DONE]"})

        self.assertIsNotNone(round_record)
        self.assertEqual(round_record["event"], "chat_round")
        self.assertEqual(round_record["question"], "hello")
        self.assertEqual(round_record["status"], "done")
        self.assertEqual(round_record["completion_count"], 1)
        self.assertEqual(round_record["usage_total"]["total_tokens"], 10)
        self.assertEqual(len(round_record["events"]), 3)

    def test_finalize_without_pending_round_returns_none(self):
        store = ChatRoundStore("demo")
        self.assertIsNone(store.finalize_round("done"))


if __name__ == "__main__":
    unittest.main()
