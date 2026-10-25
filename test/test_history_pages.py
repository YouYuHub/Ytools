"""Page snapshots keep original files, round IDs and account boundaries intact."""
import hashlib
import json

from test_security_resources import accounts


def write_rounds(root, count=80):
    path = root / "chat-a_chat.jsonl"
    rows = [{"_meta": {"title": "paged", "usage": {"total_tokens": 123}}}]
    rows += [{"event":"chat_round","question":f"question-{n}","events":[
        {"role":"user","content":f"question-{n}"},
        {"role":"assistant","content":f"answer-{n}"},
        {"role":"tool","name":"fixture","content":"工具结果"}],"status":"done"} for n in range(1,count+1)]
    path.write_text("\n".join(json.dumps(row,ensure_ascii=False) for row in rows)+"\n",encoding="utf-8")
    return path


def test_round_pages_are_read_only_and_complete_without_overlaps(accounts):
    _,_,_,(first,_,_),root=accounts
    path=write_rounds(root)
    original=hashlib.sha256(path.read_bytes()).hexdigest()
    numbers=[];before=None;revision=None
    while True:
        params={"session_id":"chat-a","limit":13,"include_questions":before is None}
        if before is not None:params["before"]=before;params["revision"]=revision
        response=first.get("/chat_history/page",params=params)
        assert response.status_code==200,(response.text,dict(response.headers))
        data=response.json();revision=data["revision"]
        rows=[json.loads(line) for line in data["text"].splitlines()]
        entries=[row for row in rows if row.get("event")=="chat_round"]
        assert len(entries)==data["end_round"]-data["start_round"]+1
        assert data["round_offset"]==data["start_round"]-1
        numbers[:0]=list(range(data["start_round"],data["end_round"]+1))
        if before is None:assert len(data["questions"])==80
        else:assert "questions" not in data
        if not data["has_older"]:break
        before=data["start_round"]
    assert numbers==list(range(1,81))
    assert hashlib.sha256(path.read_bytes()).hexdigest()==original


def test_jump_snapshot_change_and_foreign_read(accounts):
    _,_,_,(first,second,_),root=accounts
    path=write_rounds(root)
    response=first.get("/chat_history/page?session_id=chat-a&around=12&limit=5").json()
    assert response["start_round"]<=12<=response["end_round"]
    assert response["total_rounds"]==80
    assert second.get("/chat_history/page?session_id=chat-a").status_code==404
    assert first.get("/chat_history/page?session_id=not-owned").status_code==404
    assert not list(root.rglob("*not-owned*"))
    path.write_text(path.read_text(encoding="utf-8").replace("answer-1","changed-1"),encoding="utf-8")
    stale=first.get("/chat_history/page",params={"session_id":"chat-a","revision":response["revision"],"before":response["start_round"]})
    assert stale.status_code==409 and stale.headers.get("X-History-Changed")=="true"
    assert first.get("/chat_history/page?session_id=chat-a&before=3&around=4").status_code==422


def test_budget_never_splits_a_round_or_drops_external_compaction(accounts,monkeypatch):
    from memory import history_pages
    _,_,_,(first,_,_),root=accounts
    path=write_rounds(root,4)
    with path.open("a",encoding="utf-8") as stream:
        stream.write(json.dumps({"event":"context_compaction","scope":"session","phase":"done",
                                 "display_round":2,"display_event_index":0,"summary_text":"summary"})+"\n")
    monkeypatch.setattr(history_pages,"_PAGE_BYTES",20)
    data=first.get("/chat_history/page?session_id=chat-a&around=2&limit=4").json()
    assert data["start_round"]==data["end_round"]==2
    rows=[json.loads(line) for line in data["text"].splitlines()]
    assert any(row.get("summary_text")=="summary" for row in rows)
    assert len([r for r in rows if r.get("event")=="chat_round"])==1


def test_saved_permission_checkpoints_keep_the_full_overlay_path(accounts,monkeypatch):
    from memory import permission_store
    _,_,_,(first,_,_),_=accounts
    monkeypatch.setattr(permission_store,"has_restorable_round",lambda sid:True)
    assert first.get("/chat_history/page?session_id=chat-a").json()=={"fallback_full":True}


def test_large_tool_result_is_complete_on_demand_and_stale_or_foreign_refs_are_rejected(accounts):
    _,_,_,(first,second,_),root=accounts
    path=write_rounds(root,2)
    rows=[json.loads(line) for line in path.read_text(encoding='utf8').splitlines()]
    large='complete-result-'+('x'*100000)
    rows[-1]['events'].append({'role':'tool','tool_name':'read_file','tool_call_id':'large','result':large})
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n',encoding='utf8')
    original=path.read_bytes()
    preview=first.get('/chat_history/page?session_id=chat-a').json()
    assert large not in preview['text'] and len(preview['text'])<10000
    event=json.loads(preview['text'].splitlines()[-1])['events'][-1]
    ref=event['_history_detail'];params={'session_id':'chat-a','round_no':ref['round'],'event_index':ref['event_index'],'revision':ref['revision']}
    full=first.get('/chat_history/event',params=params)
    assert full.status_code==200 and full.json()['event']['result']==large
    assert path.read_bytes()==original
    assert second.get('/chat_history/event',params=params).status_code==404
    assert first.get('/chat_history/event',params={**params,'event_index':0}).status_code==404
    path.write_bytes(original.replace(b'answer-1',b'changed-1'))
    assert first.get('/chat_history/event',params=params).status_code==409


def test_header_only_changes_rebuild_offsets_without_invalidating_identical_rounds(accounts):
    _,_,_,(first,_,_),root=accounts
    path=write_rounds(root,4)
    original=first.get('/chat_history/page?session_id=chat-a&limit=2').json()
    path.write_text(path.read_text(encoding='utf8').replace('"title": "paged"','"title": "a much longer new title"'),encoding='utf8')
    updated=first.get('/chat_history/page',params={'session_id':'chat-a','before':original['start_round'],'revision':original['revision']})
    assert updated.status_code==200 and updated.json()['revision']==original['revision']
    assert 'a much longer new title' in updated.json()['text']
