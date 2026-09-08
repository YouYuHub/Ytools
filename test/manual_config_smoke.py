"""配置热重载 + 会话级工具选择 手动冒烟（起真实 uvicorn 服务）。

验证项：
1. 服务启动后 config-hot-reload 线程存在；
2. 会话级工具选择 POST/GET（session_id 写 _meta.tool_selection、清除恢复跟随全局）；
3. 全局 tool_selection POST 仍写 mcp_servers.json（备份/还原）；
4. 手工改 setting/mcp_servers.json → [config-watch] 重载日志 + 仅 inputs 变化不重探工具；
5. 手工改 setting/models.json → [config-watch] models.json 内存已同步。

用后还原所有被改文件并清理冒烟会话。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PY = sys.executable
PORT = 48633
BASE = f"http://127.0.0.1:{PORT}"

server_log: list[str] = []


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {path} -> HTTP {exc.code}: {body}") from exc


def _wait_server(deadline: float = 40.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            _request("GET", "/")
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("服务未在期限内启动")


def main() -> None:
    sid = f"config_smoke_{uuid.uuid4().hex[:8]}"
    mcp_path = ROOT / "setting" / "mcp_servers.json"
    models_path = ROOT / "setting" / "models.json"
    mcp_backup = mcp_path.read_bytes()
    models_backup = models_path.read_bytes()
    history_file = ROOT / "history_files" / f"{sid}_chat.jsonl"
    lock_file = ROOT / "history_files" / "lock" / f"{sid}_chat.jsonl.lock"

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    def _pump() -> None:
        for line in proc.stdout:
            server_log.append(line.rstrip())

    threading.Thread(target=_pump, daemon=True).start()

    try:
        _wait_server()
        print("OK: 服务已启动")
        # uvicorn 无 reload 模式下 app 在主进程导入，config-hot-reload 线程应已启动
        # （间接验证：后面的热重载日志）
        time.sleep(1.0)

        # ---- 2. 会话级工具选择（服务名从磁盘动态读取，不硬编码） ----
        servers_before = list(json.loads(mcp_path.read_text(encoding="utf-8"))["servers"].keys())
        primary_server = servers_before[0]
        resp = _request("POST", "/chat_config/tool_selection", {
            "inputs": {primary_server: ["read_file", "", 123, "read_file"], "ghostServer": ["x"]},
            "session_id": sid,
        })
        assert resp["state"] == "succeed", resp
        # 会话级不做未知服务校验（失效服务在生成时按工具名自动忽略），仅规整非法条目
        assert resp["session_selection"] == {primary_server: ["read_file"], "ghostServer": ["x"]}, resp
        assert resp["is_overridden"] is True
        assert history_file.exists(), "会话级保存应创建会话 _meta"
        print("OK: POST(session_id) 写入会话 _meta.tool_selection 并规整非法条目")

        resp = _request("GET", f"/chat_config/tool_selection?session_id={sid}")
        assert resp["session_selection"] == {primary_server: ["read_file"], "ghostServer": ["x"]}, resp
        assert resp["is_overridden"] is True
        assert resp["effective_selection"] == {primary_server: ["read_file"], "ghostServer": ["x"]}, resp
        assert resp["inputs"], "全局 inputs 应同时返回"
        print("OK: GET(session_id) 返回会话覆盖/生效选择")

        resp = _request("POST", "/chat_config/tool_selection", {"inputs": {}, "session_id": sid})
        assert resp["is_overridden"] is False and resp["session_selection"] is None, resp
        resp = _request("GET", f"/chat_config/tool_selection?session_id={sid}")
        assert resp["is_overridden"] is False
        assert resp["effective_selection"] == resp["inputs"], "清除后生效选择应等于全局默认"
        print("OK: 空 inputs 清除会话覆盖，恢复跟随全局")

        # ---- 3. 全局 tool_selection POST 仍写 mcp_servers.json ----
        global_inputs = {server: [] for server in servers_before}
        global_inputs[primary_server] = ["list_dir"]
        resp = _request("POST", "/chat_config/tool_selection", {"inputs": global_inputs})
        assert resp["state"] == "succeed" and resp["inputs"][primary_server] == ["list_dir"], resp
        disk = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert disk["inputs"][primary_server] == ["list_dir"] and set(disk["servers"]) == set(servers_before)
        print("OK: 全局 POST 仍写回 mcp_servers.json（servers 键保留）")

        # ---- 4. 热重载 mcp_servers.json：仅 inputs 变化 ----
        _wait_filesystem()
        base_reloads = _count("已重新加载")
        base_tools = _count("个工具")
        disk["inputs"][primary_server] = ["edit_file"]
        mcp_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _wait_log_count("已重新加载", base_reloads + 1)
        reload_lines = [l for l in server_log if "已重新加载" in l and "mcp_servers.json" in l]
        assert reload_lines and "servers 变更=False" in reload_lines[-1], server_log[-20:]
        assert _count("个工具") == base_tools, "仅 inputs 变化不应重探工具"
        print("OK: mcp_servers.json inputs 变更 → 自动重载，不触发工具重探")

        # ---- 5. 热重载 mcp_servers.json：servers 变化 → 重探工具 ----
        base_reloads = _count("已重新加载")
        base_tools = _count("个工具")
        _wait_filesystem()
        disk["servers"]["ghostSmokeMcp"] = {"type": "stdio", "command": "python", "args": ["__no_such__.py"]}
        mcp_path.write_text(json.dumps(disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _wait_log_count("已重新加载", base_reloads + 1)
        _wait_log_count("个工具", base_tools + 1)
        assert any("servers 变更=True" in l for l in server_log), server_log[-20:]
        print("OK: mcp_servers.json servers 变更 → 自动重新发现工具")

        # ---- 6. 热重载 models.json ----
        _wait_filesystem()
        models = json.loads(models_backup.decode("utf-8"))
        models["variables"] = models.get("variables") or {}
        models["variables"]["CONFIG_SMOKE_MARKER"] = "watcher-ok"
        models_path.write_text(json.dumps(models, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _wait_log("models.json 内存已同步")
        print("OK: models.json 变更 → 内存配置自动重载")

        # ---- 7. 会话级模型选择（写会话 _meta，不动全局 models.json） ----
        disk_models = json.loads(models_path.read_text(encoding="utf-8"))
        catalog = _request("GET", "/chat_config/models?role=chat_model")
        chat_models = [
            m for m in catalog["models"]
            if str(m.get("api_type") or "").casefold() == "chat-completions"
        ]
        assert len(chat_models) >= 2, "需要至少两个 chat-completions 模型"
        current = catalog["role_info"]["selection"]
        target = next(
            m for m in chat_models
            if not (m["provider_name"] == current["provider"] and m["model_name"] == current["model"])
        )
        resp = _request("POST", "/chat_config/models/select", {
            "provider": target["provider_name"],
            "model": target["model_name"],
            "role": "chat_model",
            "session_id": sid,
        })
        assert resp["state"] == "succeed", resp
        assert resp["session_selection"]["chat_model"]["model_name"] == target["model_name"], resp
        disk_after = json.loads(models_path.read_text(encoding="utf-8"))
        assert disk_after["model_selection"] == disk_models["model_selection"], "会话级选择不应写全局 models.json"
        resp = _request("GET", f"/chat_config/models?role=chat_model&session_id={sid}")
        assert resp["role_info"]["is_overridden"] is True, resp.get("role_info", {}).get("is_overridden")
        assert resp["role_info"]["selection"]["model"] == target["model_name"]
        assert resp["session_selection"]["chat_model"]["model_name"] == target["model_name"]
        print("OK: POST/GET(session_id) 会话级模型选择生效，全局 models.json 未变")

        # ---- 8. 清除会话模型覆盖，恢复跟随全局 ----
        resp = _request("POST", "/chat_config/models/select", {
            "role": "chat_model", "session_id": sid, "clear": True,
        })
        assert resp["state"] == "succeed" and resp["session_selection"] is None, resp
        resp = _request("GET", f"/chat_config/models?role=chat_model&session_id={sid}")
        assert resp["role_info"]["is_overridden"] is False
        assert resp["role_info"]["selection"]["model"] == current["model"], "清除后应回退全局默认模型"
        print("OK: clear 清除会话覆盖，回退全局默认模型")

        # ---- 9. 工具列表接口仍正常 ----
        tools = _request("GET", "/tools/list")
        assert isinstance(tools.get("total"), int) and isinstance(tools.get("tools"), list)
        print(f"OK: /tools/list 正常（total={tools.get('total')}）")
        print("ALL PASS")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        mcp_path.write_bytes(mcp_backup)
        models_path.write_bytes(models_backup)
        for f in (history_file, lock_file):
            try:
                f.unlink()
            except OSError:
                pass
        print("CLEANUP: 配置文件已还原，冒烟会话已清理")


def _wait_filesystem() -> None:
    time.sleep(0.3)


def _count(needle: str) -> int:
    return sum(1 for line in server_log if needle in line)


def _wait_log_count(needle: str, minimum: int, timeout: float = 25.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if _count(needle) >= minimum:
            return
        time.sleep(0.3)
    raise AssertionError(f"日志中 {needle!r} 未达到 {minimum} 次；最近日志：\n" + "\n".join(server_log[-30:]))


def _wait_log(*needles: str, expect_no_models: bool = False, min_lines: int = 0, timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        joined = "\n".join(server_log)
        if all(n in joined for n in needles):
            return
        time.sleep(0.3)
    raise AssertionError(f"日志未出现 {needles}；最近日志：\n" + "\n".join(server_log[-30:]))


if __name__ == "__main__":
    main()
