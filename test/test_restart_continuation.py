"""Hard restart -> a new user turn must recover history, not a phantom task."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_permission_generation import environment  # noqa: F401
from config import ChatLLMRequest
from factory import chat_factory as cf, session_worker
from memory import chat_memory, permission_store as store


@pytest.fixture
def restart_env(environment, monkeypatch):
    monkeypatch.setattr(cf, "_CHAT_WORKER_MODE", "inline")
    monkeypatch.setattr(cf, "_SESSION_STREAMS", {})
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    return environment


def request(text="继续"):
    return ChatLLMRequest(session_id="restart-test", messages=[{"role":"user", "content":text}], tool_names=["run_command"])


async def orphan():
    manager = await chat_memory.get_chat_memory_manager("restart-test")
    await manager.add_chat_history({"role":"user", "content":"原任务"})
    calls = [{"id":key,"type":"function","function":{"name":"run_command","arguments":"{}"}} for key in ("cached","unknown","planned")]
    assistant = {"role":"assistant","content":"已有进度", "tool_calls":calls}
    await manager.add_chat_history(assistant)
    task = store.create_task("restart-test", {"request":request("原任务").model_dump(), "mode":"full_control", "root":os.getcwd()})
    store.checkpoint(task["id"], "main", {"messages":[assistant],"assistant":assistant,"pending_round":manager._round_store.snapshot_pending_round()})
    store.completed(task["id"], "main", "cached", {"stdout":"持久化的执行结果"})
    store.claim_execution(task["id"], task["epoch"], "main", "unknown")
    pending=json.loads(manager._pending_checkpoint_path.read_text(encoding="utf-8"))
    pending["writer_pid"]=-1
    # The round checkpoint can be newer than the permission snapshot.
    pending["events"].append({"role":"assistant","reasoning_content":"检查点中的最新进度"})
    manager._pending_checkpoint_path.write_text(json.dumps(pending,ensure_ascii=False),encoding="utf-8")
    with store.database() as db:
        db.execute("UPDATE tasks SET owner=-1 WHERE id=?",(task["id"],))
    await chat_memory.cleanup_chat_memory_manager("restart-test")
    return task


async def collect(req):
    return await asyncio.wait_for(_collect(req), 5)


async def _collect(req):
    return [chunk async for chunk in cf.tool_chat_server(req)]


def test_new_message_after_death_retains_results_and_starts_a_real_turn(restart_env, monkeypatch):
    model_calls=[]
    async def model(request=None, **kwargs):
        model_calls.append(request)
        yield 'data: {"content":"继续执行的新回复","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM,"chat_completions",model)
    execution=AsyncMock(side_effect=AssertionError("old tools must not be replayed"))
    monkeypatch.setattr(cf,"try_execute_builtin_command_tool",execution)
    async def run():
        saved=await orphan()
        chunks=await collect(request())
        text="".join(chunks)
        assert "继续执行的新回复" in text and "INTERRUPTED_TASK_CONTINUED" in text and "[DONE]" in text
        assert len(model_calls)==1 and not cf.is_chat_stream_running("restart-test")
        execution.assert_not_called()
        assert store.task(saved["id"])["state"]=="cancelled" and store.resumable("restart-test")==[]
        path=restart_env/"history"/"restart-test_chat.jsonl"
        rows=[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rounds=[row for row in rows if row.get("event")=="chat_round"]
        assert [row["status"] for row in rounds]==["interrupted","done"]
        tools={event["tool_call_id"]:event["content"] for event in rounds[0]["events"] if event.get("role")=="tool"}
        assert "持久化的执行结果" in tools["cached"]
        assert "结果未知" in tools["unknown"] and "未执行" in tools["planned"]
        assert any(e.get("reasoning_content")=="检查点中的最新进度" for e in rounds[0]["events"])
        sent=json.dumps(model_calls[0].model_dump(),ensure_ascii=False)
        assert "原任务" in sent and "结果未知" in sent and "继续" in sent
    asyncio.run(run())


def test_live_old_owner_is_rejected_with_visible_error_and_can_retry(restart_env, monkeypatch):
    async def run():
        saved=await orphan()
        store.set_task_state(saved["id"],"suspended")
        with store.database() as db:
            db.execute("UPDATE tasks SET owner=? WHERE id=?",(os.getpid(),saved["id"]))
        chunks=await collect(request())
        assert "旧任务执行进程仍存在" in "".join(chunks) and chunks[-1]=="data: [DONE]\n\n"
        assert not cf.is_chat_stream_running("restart-test")
        assert store.task(saved["id"])["state"]=="suspended" and store.load_checkpoint(saved["id"])
    asyncio.run(run())


@pytest.mark.parametrize("cached", [False,True])
def test_death_before_first_tool_checkpoint_also_recovers_the_round(restart_env, monkeypatch, cached):
    async def model(**kwargs):
        yield 'data: {"content":"pre-tool continuation","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM,"chat_completions",model)
    async def run():
        manager=await chat_memory.get_chat_memory_manager("restart-test")
        await manager.add_chat_history({"role":"user","content":"等待首次模型响应"})
        saved=store.create_task("restart-test",{"request":{},"mode":"full_control","root":os.getcwd()})
        pending=json.loads(manager._pending_checkpoint_path.read_text(encoding="utf-8")); pending["writer_pid"]=-1
        manager._pending_checkpoint_path.write_text(json.dumps(pending,ensure_ascii=False),encoding="utf-8")
        with store.database() as db: db.execute("UPDATE tasks SET owner=-1 WHERE id=?",(saved["id"],))
        if not cached: await chat_memory.cleanup_chat_memory_manager("restart-test")
        assert "pre-tool continuation" in "".join(await collect(request()))
        assert store.task(saved["id"])["state"]=="cancelled"
        rows=[json.loads(line) for line in manager._file_path.read_text(encoding="utf-8").splitlines()]
        assert [row["status"] for row in rows if row.get("event")=="chat_round"]==["interrupted","done"]
    asyncio.run(run())


@pytest.mark.parametrize("existing", [False,True])
def test_empty_reconnect_after_restart_or_completion_never_starts_a_task(restart_env, monkeypatch, existing):
    start=AsyncMock(side_effect=AssertionError("attach must not start a task"))
    monkeypatch.setattr(cf,"_start_session_generation",start)
    async def run():
        saved=await orphan()
        if existing:
            stream=cf._set_session_stream("restart-test"); await stream.finish()
        attach=ChatLLMRequest(session_id="restart-test",messages=[])
        assert await collect(attach)==["data: [DONE]\n\n"]
        assert store.task(saved["id"])["state"]=="running"
        start.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["false","exception"])
def test_worker_start_failure_is_returned_to_creator(restart_env, monkeypatch, mode):
    def start(payload):
        if mode=="exception": raise RuntimeError("spawn failed")
        return False
    proxy=SimpleNamespace(set_loop=lambda loop:None,is_generation_running=lambda:False,
                          bind_stream=lambda stream:None,start_generation=start)
    monkeypatch.setattr(cf,"_CHAT_WORKER_MODE","process")
    monkeypatch.setattr(cf,"sweep_dead_worker_proxies",lambda:None)
    monkeypatch.setattr(cf,"get_worker_proxy",lambda sid:proxy)
    async def run():
        chunks=await collect(request())
        assert "worker 进程启动失败" in "".join(chunks) and chunks[-1]=="data: [DONE]\n\n"
        assert not cf.is_chat_stream_running("restart-test")
    asyncio.run(run())


def test_inline_preparation_return_still_finishes_the_stream(restart_env, monkeypatch):
    async def early_return(req, stream, **kwargs):
        stream.emit(cf._sse_error_payload("模型配置已失效"))
    monkeypatch.setattr(cf,"_run_chat_generation",early_return)
    async def run():
        chunks=await collect(request())
        assert "模型配置已失效" in "".join(chunks) and chunks[-1]=="data: [DONE]\n\n"
        assert not cf.is_chat_stream_running("restart-test")
    asyncio.run(run())


def test_worker_preparation_return_always_emits_task_done():
    async def run():
        import queue
        events=queue.Queue(); adapter=session_worker._WorkerStreamAdapter("test",events)
        task=asyncio.create_task(asyncio.sleep(0)); await task
        session_worker._complete_worker_generation(task,adapter)
        await asyncio.sleep(0)
        assert adapter.done and events.get_nowait()=={"type":"task_done"}
    asyncio.run(run())


def test_failed_resume_preparation_releases_backend_owned_running_state(restart_env, monkeypatch):
    async def early_return(req,stream,**kwargs):
        stream.emit(cf._sse_error_payload("模型配置已失效"))
    monkeypatch.setattr(cf,"_run_chat_generation",early_return)
    async def run():
        saved=await orphan(); store.pending("restart-test")
        # This dispatch was claimed by the backend but never adopted by a worker.
        with store.database() as db:
            db.execute("UPDATE tasks SET owner=?,state='running' WHERE id=?",(os.getpid(),saved["id"]))
        stream=cf._SessionStream("restart-test")
        assert await cf._start_session_generation(None,stream,request(),permission_resume_id=saved["id"])
        await stream.task
        current=store.task(saved["id"])
        assert current["state"]=="suspended" and current["owner"]==-1 and stream.done
        assert store.load_checkpoint(saved["id"])
    asyncio.run(run())


def test_recovery_write_failure_preserves_checkpoint_and_releases_claim(restart_env, monkeypatch):
    async def run():
        saved=await orphan()
        manager=await chat_memory.get_chat_memory_manager("restart-test")
        monkeypatch.setattr(manager,"finish_saved_permission_round",AsyncMock(side_effect=OSError("disk unavailable")))
        chunks=await collect(request())
        assert "disk unavailable" in "".join(chunks)
        current=store.task(saved["id"])
        assert current["state"]=="suspended" and current["owner"]==-1
        assert store.load_checkpoint(saved["id"]) and not cf.is_chat_stream_running("restart-test")
    asyncio.run(run())


def test_reconnect_during_recovery_waits_for_the_same_generation(restart_env, monkeypatch):
    async def run():
        entered=asyncio.Event(); release=asyncio.Event(); finish=asyncio.Event(); calls=[]
        async def start(proxy, stream, req):
            calls.append(req); entered.set(); await release.wait()
            stream.emit('data: {"content":"shared generation"}\n\n')
            async def produce():
                await finish.wait(); await stream.finish()
            stream.task=asyncio.create_task(produce())
            return True
        monkeypatch.setattr(cf,"_start_session_generation",start)
        first=asyncio.create_task(collect(request()))
        await entered.wait()
        attached=asyncio.create_task(collect(ChatLLMRequest(session_id="restart-test",messages=[])))
        await asyncio.sleep(.02)
        assert not attached.done()
        release.set(); await asyncio.sleep(.02); finish.set()
        results=await asyncio.gather(first,attached)
        assert len(calls)==1 and all("shared generation" in "".join(result) for result in results)
        assert not cf._SESSION_START_GUARDS
    asyncio.run(run())


def test_killed_writer_process_can_be_followed_by_new_message(restart_env, monkeypatch):
    # Only this disposable writer is killed; no real server or model is used.
    script=r'''
import asyncio,json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from memory import chat_memory as memory,permission_store as store
root=Path(sys.argv[2]);memory.HISTORY_ROOT=root/'history';memory.LOCK_ROOT=root/'locks';store.ROOT=root/'control'
async def main():
    manager=await memory.get_chat_memory_manager('restart-test')
    await manager.add_chat_history({'role':'user','content':'process task'})
    await manager.add_chat_history({'role':'assistant','content':'saved before kill'})
    task=store.create_task('restart-test',{'request':{},'mode':'full_control','root':str(root)})
    store.checkpoint(task['id'],'main',{'pending_round':manager._round_store.snapshot_pending_round(),'messages':[]})
    (root/'ready').write_text(task['id'],encoding='utf-8')
    await asyncio.sleep(60)
asyncio.run(main())
'''
    async def model(**kwargs):
        yield 'data: {"content":"continued after real process kill","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM,"chat_completions",model)
    process=subprocess.Popen([sys.executable,"-c",script,str(Path(__file__).resolve().parents[1]),str(restart_env)],
                             stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        async def run():
            async def ready():
                while not (restart_env/"ready").exists():
                    assert process.poll() is None,process.communicate()[1].decode(errors="replace")
                    await asyncio.sleep(.05)
            await asyncio.wait_for(ready(),10)
            task_id=(restart_env/"ready").read_text(encoding="utf-8")
            process.kill(); process.wait(timeout=5)
            assert not store.pid_alive(process.pid)
            chunks=await collect(request())
            assert "continued after real process kill" in "".join(chunks)
            assert store.task(task_id)["state"]=="cancelled" and not cf.is_chat_stream_running("restart-test")
            rows=[json.loads(line) for line in (restart_env/"history"/"restart-test_chat.jsonl").read_text(encoding="utf-8").splitlines()]
            rounds=[row for row in rows if row.get("event")=="chat_round"]
            assert [row["status"] for row in rounds]==["interrupted","done"]
            assert "saved before kill" in json.dumps(rounds,ensure_ascii=False)
        asyncio.run(run())
    finally:
        if process.poll() is None: process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("protocol", ["messages", "responses"])
def test_native_tool_slots_close_with_ledger_results_and_next_payload_is_valid(restart_env, monkeypatch, protocol):
    from chat.protocols import native_store, responses_store
    from chat.protocols.messages import build_payload
    from factory.agent_runtime.chat_runtime import copy_for_request
    from memory.chat_history_format import round_entry_to_context_messages
    from test_responses_api import answer, commit, turn, CONFIG as RESPONSES_CONFIG
    from test_messages_api import CONFIG as MESSAGES_CONFIG
    monkeypatch.setattr(native_store,"_root",lambda:restart_env/"native")
    monkeypatch.setattr(responses_store,"_root",lambda:restart_env/"responses")
    monkeypatch.setattr(responses_store,"_mirror",lambda *args:None)
    async def run():
        saved=await orphan()
        calls=[(key,"run_command","{}") for key in ("cached","unknown","planned")]
        if protocol=="responses":
            ref=commit(turn([{"role":"user","content":"原任务"}],session="restart-test"),answer("resp_old","已有进度",calls=calls))
        else:
            blocks=[{"type":"thinking","thinking":"signed thinking","signature":"original-signature"},
                    {"type":"text","text":"已有进度"}]+[{"type":"tool_use","id":key,"name":name,"input":{}} for key,name,_ in calls]
            ref=native_store.save_record("restart-test",MESSAGES_CONFIG,
                {"messages":[{"role":"user","content":"原任务"}]},
                {"id":"old-message","stop_reason":"tool_use","content":blocks})
        native_store.tool_started("restart-test",ref,"cached")
        native_store.tool_started("restart-test",ref,"unknown")
        snapshot=store.load_checkpoint(saved["id"])
        snapshot["assistant"]["provider_state"]=ref
        # Use the manager's actual sidecar layout rather than assuming its path.
        manager=await chat_memory.get_chat_memory_manager("restart-test")
        path=manager._pending_checkpoint_path
        pending=json.loads(path.read_text(encoding="utf-8"))
        next(event for event in pending["events"] if event.get("tool_calls"))["provider_state"]=ref
        path.write_text(json.dumps(pending,ensure_ascii=False),encoding="utf-8")
        store.checkpoint(saved["id"],"main",snapshot)
        assert await cf._retire_interrupted_tasks("restart-test")==1
        ledger=(responses_store if protocol=="responses" else native_store).load_record("restart-test",ref)
        assert all(item["status"]=="completed" for item in ledger["tool_status"].values())
        assert "结果未知" in ledger["tool_status"]["unknown"].get("content",ledger["tool_status"]["unknown"].get("output",""))
        rows=[json.loads(line) for line in manager._file_path.read_text(encoding="utf-8").splitlines()]
        old=next(row for row in rows if row.get("event")=="chat_round")
        messages=round_entry_to_context_messages(old,max_tool_result_length=-1)+[{"role":"user","content":"继续"}]
        shaped=copy_for_request(messages,api_type=protocol)
        if protocol=="messages":
            payload=build_payload(ChatLLMRequest(session_id="restart-test",messages=shaped),MESSAGES_CONFIG,True)
            assert "original-signature" in json.dumps(ledger,ensure_ascii=False)
            # A new user turn uses fact projection, not an old signed thinking
            # continuation after the explicitly closed exchange.
            assert "original-signature" not in json.dumps(payload,ensure_ascii=False)
            assert "结果未知" in json.dumps(payload,ensure_ascii=False)
        else:
            from factory.agent_runtime.responses_context import prepare
            payload,_,_=prepare(turn(shaped,session="restart-test"),RESPONSES_CONFIG,False)
            assert any(item.get("type")=="function_call_output" for item in payload["input"])
            assert "结果未知" in json.dumps(payload,ensure_ascii=False)
    asyncio.run(run())
