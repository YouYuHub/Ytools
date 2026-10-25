"""Pure model-input normalization; the executor facade supplies legacy policy overrides."""
import json
from typing import Any

TODO_VALID_STATUSES = ('pending', 'in_progress', 'done')
TODO_MAX_ITEMS = 20
TODO_MAX_CONTENT_CHARS = 200
ASK_MAX_QUESTIONS = 5
ASK_MAX_QUESTION_CHARS = 200
ASK_MAX_OPTIONS = 6
ASK_MAX_OPTION_CHARS = 100
SUB_AGENT_TASK_MAX_CHARS = 20000


def next_todo_id(existing: set[str]) -> str:
    """分配下一个未占用的自增数字 id（字符串形式，"1"、"2"...）。"""
    num = 1
    while str(num) in existing:
        num += 1
    return str(num)


def normalize_todo_items(raw: Any, prev_items: list[dict[str, Any]] | None = None, *, max_items=TODO_MAX_ITEMS,
                         max_content_chars=TODO_MAX_CONTENT_CHARS, valid_statuses=TODO_VALID_STATUSES, id_factory=next_todo_id) -> tuple[list[dict[str, str]] | None, list[str] | None, str | None]:
    """校验并规整模型提交的 todo 列表（唯一校验入口）。

    返回 ``(items, notices, error_reason)``：
    - 成功：items 为规整后的计划（保证 id 唯一且非空、in_progress ≤ 1），
      notices 为自动修正告警，error_reason 为 None；
    - 失败：items/notices 为 None，error_reason 描述首个硬性错误（含条目
      序号与具体值），供工具结果直接回传，帮助模型一次性纠正。

    硬性校验（拒绝并给原因）：非数组/条目非对象/content 为空或超长/
    status 非法/超过 20 项。自动修复（放行并告警）：id 缺失或为空
    （同 content+status 匹配上一版计划则继承其 id，否则本地自增分配）、
    id 重复（为重复者重新分配）、多个 in_progress（保留首个，其余改
    pending）。兼容旧模型不按新 Schema 提交时缺失 id 的情况。
    """
    if not isinstance(raw, list):
        return None, None, "todos 需要是对象数组，且每次调用提交完整列表（全量覆盖）"
    if len(raw) > max_items:
        return None, None, (
            f"todo 数量超过上限：收到 {len(raw)} 项，最多 {max_items} 项，"
            "请合并同类步骤或拆分任务"
        )
    # 状态变化时也要继承同一步骤的 id；先匹配内容与状态，再匹配内容。
    prev_by_key: dict[tuple[str, str], str] = {}
    prev_by_content: dict[str, list[str]] = {}
    prev_ids: set[str] = set()
    for prev in prev_items or []:
        if not isinstance(prev, dict):
            continue
        prev_id = str(prev.get("id") or "").strip()
        prev_content = str(prev.get("content") or "").strip()
        prev_status = str(prev.get("status") or "pending").strip()
        if prev_id and prev_content:
            prev_by_key[(prev_content, prev_status)] = prev_id
            prev_by_content.setdefault(prev_content, []).append(prev_id)
            prev_ids.add(prev_id)
    items: list[dict[str, str]] = []
    notices: list[str] = []
    used_ids: set[str] = set()
    id_missing = id_duplicated = in_progress_fixed = 0
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            return None, None, f"第 {index} 项必须是对象（含 id/content/status 字段）"
        if not isinstance(entry.get("content"), str):
            return None, None, f"第 {index} 项 content 不能为空且必须是字符串"
        content = entry["content"].strip()
        if not content:
            return None, None, f"第 {index} 项 content 不能为空"
        if len(content) > max_content_chars:
            return None, None, (
                f"第 {index} 项 content 超长（{len(content)} > "
                f"{max_content_chars} 字符），请压缩为一句祈使句"
            )
        if not isinstance(entry.get("status", "pending"), str):
            return None, None, f"第 {index} 项 status 必须是字符串"
        status = entry.get("status", "pending").strip().lower()
        if status not in valid_statuses:
            return None, None, (
                f"第 {index} 项 status 非法：{status!r}，"
                "仅支持 pending / in_progress / done"
            )
        if not isinstance(entry.get("id", ""), str) or len(entry.get("id", "")) > 64:
            return None, None, f"第 {index} 项 id 必须是最多 64 字符的字符串"
        raw_id = entry.get("id", "").strip()
        if raw_id:
            if raw_id in used_ids:
                id_duplicated += 1
                raw_id = id_factory(used_ids | prev_ids)
        else:
            inherited = prev_by_key.get((content, status))
            if not inherited or inherited in used_ids:
                inherited = next(
                    (item_id for item_id in prev_by_content.get(content, [])
                     if item_id not in used_ids), None
                )
            if inherited and inherited not in used_ids:
                raw_id = inherited
            else:
                raw_id = id_factory(used_ids | prev_ids)
            id_missing += 1
        used_ids.add(raw_id)
        if status == "in_progress" and any(i["status"] == "in_progress" for i in items):
            in_progress_fixed += 1
            status = "pending"
        items.append({"id": raw_id, "content": content, "status": status})
    if id_missing:
        notices.append(
            f"{id_missing} 个步骤未携带 id，已自动分配/继承"
            "（下次调用请保持相同步骤的 id 不变）"
        )
    if id_duplicated:
        notices.append(f"{id_duplicated} 个步骤 id 重复，已为重复项重新分配")
    if in_progress_fixed:
        notices.append(
            f"{in_progress_fixed} 个步骤多余地标记为 in_progress"
            "（同一时间最多一个），仅保留首个，其余已改为 pending"
        )
    return items, notices, None


def normalize_ask_questions(raw: Any, *, max_questions=ASK_MAX_QUESTIONS, max_question_chars=ASK_MAX_QUESTION_CHARS,
                            max_options=ASK_MAX_OPTIONS, max_option_chars=ASK_MAX_OPTION_CHARS) -> list[dict[str, Any]] | None:
    """校验并规整模型提交的提问列表；整体非法返回 None。

    规整规则：question 非空且 ≤200 字符；options 可选，规整后保留 0-6 个
    非空选项（每项 ≤100 字符），全部无效时视为未提供选项。
    容错：questions 传成单个对象时自动包一层数组；传成 JSON 字符串时尝试解析。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    if not raw or len(raw) > max_questions:
        return None
    questions: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        if not isinstance(entry.get("question"), str) or not isinstance(entry.get("multiple", False), bool):
            return None
        question = entry["question"].strip()
        if not question or len(question) > max_question_chars:
            return None
        raw_options = entry.get("options")
        if raw_options is not None and (not isinstance(raw_options, list) or any(not isinstance(item, str) for item in raw_options)):
            return None
        options: list[str] = []
        if isinstance(raw_options, list):
            for option in raw_options:
                text = str(option or "").strip()
                if not text or len(text) > max_option_chars:
                    continue
                if text not in options:
                    options.append(text)
            options = options[:max_options]
        questions.append({
            "question": question,
            "options": options,
            "multiple": entry.get("multiple", False),
        })
    return questions


def normalize_sub_agent_task(raw: Any, *, max_chars=SUB_AGENT_TASK_MAX_CHARS) -> tuple[str | None, str | None]:
    """Require a self-contained task string; return (task, error)."""
    if isinstance(raw, str):
        task_text = raw.strip()
    else:
        return None, "task 必须是非空字符串"
    if not task_text:
        return None, "task 参数无效：需要一段非空的子任务描述（自包含目标、路径、约束与期望回复）"
    if len(task_text) > max_chars:
        return None, (
            f"task 超长（{len(task_text)} > {max_chars} 字符）；"
            "请精简子任务描述，或拆分为多个子任务"
        )
    return task_text, None


