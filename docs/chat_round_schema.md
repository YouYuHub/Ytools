# chat_round JSONL Schema

## File Layout
- Line 1: `{"_meta": {...}}`
- Lines 2+: one `chat_round` object per user round.

## `_meta`
Required fields:
- `session_id`: string
- `title`: string
- `user_questions`: list of strings
- `usage`: dict, aggregated token usage across all rounds
- `created_at`: string timestamp
- `updated_at`: string timestamp
- `record_count`: number of `chat_round` records
- `completion_count`: total number of model completions counted across rounds

## `chat_round`
Each round is stored as one JSON object with:
- `event`: always `chat_round`
- `question`: the user question that started the round
- `started_at`: round start timestamp
- `ended_at`: round end timestamp
- `status`: `running`, `done`, `stopped`, `error`, or `interrupted`
- `events`: ordered raw messages for this round
- `usage_total`: aggregated token usage for the round
- `completion_count`: number of unique completion ids counted in the round

## `events`
Raw events inside a round follow chat roles:
- `user`
- `assistant`
- `tool`
- `system` when needed

Operational events such as `done` and `error` are stored in the round and used to close it.

## Example
```json
{
  "event": "chat_round",
  "question": "hello world",
  "started_at": "2026-07-28 10:57:06",
  "ended_at": "2026-07-28 10:57:06",
  "status": "done",
  "events": [
    {"role": "user", "content": "hello world"},
    {"role": "assistant", "content": "hi"},
    {"role": "tool", "tool_call_id": "x", "content": "tool result"},
    {"role": "assistant", "done": "[DONE]"}
  ],
  "usage_total": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10},
  "completion_count": 1
}
```
