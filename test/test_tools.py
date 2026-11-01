import json

import pytest
from fastapi.testclient import TestClient

from src import server
from src.core.chat import (ChatService, Finished, Interaction, Started, ToolResult, ToolStart)
from src.core.ollama_client import Done, Token, ToolCall
from src.core.storage import Storage


def R(*events):
    return [*events, Done(10, 2, 0.1)]


def scripted(*rounds):
    seen, it = [], iter(rounds)

    def stream(url, model, messages, num_ctx=8192, think=None, tools=None):
        seen.append({"messages": [dict(m) for m in messages], "tools": tools})
        yield from next(it)
    return stream, seen


def make(tmp_path, *rounds, **cfg_extra):
    stream, seen = scripted(*rounds)
    cfg = {"ollama_url": "x", "model": "m", "num_ctx": 8192, "system_prompt": "BASE", "tools": True,
           "profile_path": str(tmp_path / "none.yaml"), "db_path": str(tmp_path / "t.db"),
           "notes_path": str(tmp_path / "notes.md"), **cfg_extra}
    st = Storage(cfg["db_path"])
    return ChatService(st, cfg, stream), st, seen, cfg


def kinds(events):
    return [type(e).__name__ for e in events]


def test_tool_loop_calls_a_tool_and_continues(tmp_path):
    svc, st, seen, _ = make(tmp_path,
                            R(ToolCall("calculate", {"expression": "2+3*4"})),
                            R(Token("It is 14.")))
    cid = st.create_conversation()
    events = list(svc.send(cid, "what is 2+3*4?"))
    assert kinds(events) == ["Started", "ToolStart", "ToolResult", "Token", "Finished"]
    assert [t["function"]["name"] for t in seen[0]["tools"]] == ["get_current_time", "calculate", "ask_user", "save_note"]
    # the 2nd model call saw the assistant tool call and the tool result
    roles = [m["role"] for m in seen[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert seen[1]["messages"][2]["tool_calls"][0]["function"]["name"] == "calculate"
    assert seen[1]["messages"][3] == {"role": "tool", "tool_name": "calculate", "content": "14"}
    reply = st.get_path(cid)[-1]
    assert reply["content"] == "It is 14." and reply["tool_trace"][0]["calls"][0]["result"] == "14"
    assert reply["tool_trace"][0]["calls"][0]["status"] == "done"


def test_tool_trace_is_replayed_to_the_model_on_the_next_turn(tmp_path):
    svc, st, seen, _ = make(tmp_path, R(ToolCall("calculate", {"expression": "6*7"})), R(Token("42")), R(Token("sure")))
    cid = st.create_conversation()
    list(svc.send(cid, "6*7?"))
    list(svc.send(cid, "thanks"))
    roles = [m["role"] for m in seen[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]


def test_tools_are_not_offered_when_disabled(tmp_path):
    svc, st, seen, _ = make(tmp_path, R(Token("hi")), tools=False)
    list(svc.send(st.create_conversation(), "hello"))
    assert seen[0]["tools"] is None


def test_unknown_tool_and_tool_errors_do_not_break_the_turn(tmp_path):
    svc, st, seen, _ = make(tmp_path,
                            R(ToolCall("nope", {}), ToolCall("calculate", {"expression": "__import__('os')"}),
                              ToolCall("calculate", {"expression": "9**9**9"})),
                            R(Token("sorry")))
    cid = st.create_conversation()
    results = [e for e in svc.send(cid, "x") if isinstance(e, ToolResult)]
    assert [r.status for r in results] == ["error", "error", "error"]
    assert "unknown tool" in results[0].result and "unsupported" in results[1].result
    assert "exponent too large" in results[2].result
    assert st.get_path(cid)[-1]["content"] == "sorry"


def test_ask_user_suspends_then_resumes_with_the_answer(tmp_path):
    svc, st, seen, _ = make(tmp_path,
                            R(ToolCall("ask_user", {"question": "Which city?", "options": ["Paris", "Tokyo"]})),
                            R(Token("Tokyo it is.")))
    cid = st.create_conversation()
    events = list(svc.send(cid, "plan a trip"))
    assert kinds(events) == ["Started", "ToolStart", "Interaction"]
    ask = events[-1]
    assert ask.payload == {"kind": "ask", "tool": "ask_user", "question": "Which city?", "options": ["Paris", "Tokyo"]}
    assert [m["role"] for m in st.get_path(cid)] == ["user"]
    assert len(st.pending_for(cid)) == 1 and len(seen) == 1

    resumed = list(svc.resume(ask.id, {"value": "Tokyo"}))
    assert kinds(resumed) == ["ToolResult", "Token", "Finished"]
    assert resumed[0].result == "Tokyo" and resumed[0].status == "answered"
    assert seen[1]["messages"][-1] == {"role": "tool", "tool_name": "ask_user", "content": "Tokyo"}
    assert st.get_path(cid)[-1]["content"] == "Tokyo it is." and st.pending_for(cid) == []
    with pytest.raises(KeyError):
        list(svc.resume(ask.id, {"value": "again"}))


def test_confirmation_approved_runs_the_tool_rejected_does_not(tmp_path):
    call = ToolCall("save_note", {"title": "Idea", "text": "buy tea"})
    svc, st, seen, cfg = make(tmp_path, R(call), R(Token("saved")), R(call), R(Token("ok, not saved")))
    cid = st.create_conversation()
    ev = list(svc.send(cid, "note it"))[-1]
    assert isinstance(ev, Interaction) and ev.payload["kind"] == "confirm" and ev.payload["arguments"]["text"] == "buy tea"
    assert not (tmp_path / "notes.md").exists()
    done = list(svc.resume(ev.id, {"approved": True}))
    assert "buy tea" in (tmp_path / "notes.md").read_text() and done[0].status == "done"

    (tmp_path / "notes.md").unlink()
    ev = list(svc.send(cid, "again"))[-1]
    rejected = list(svc.resume(ev.id, {"approved": False}))
    assert rejected[0].status == "rejected" and "rejected" in rejected[0].result
    assert not (tmp_path / "notes.md").exists()
    assert seen[3]["messages"][-1]["content"] == "The user rejected this action."


def test_a_suspended_turn_survives_a_restart(tmp_path):
    svc, st, _, cfg = make(tmp_path, R(ToolCall("ask_user", {"question": "Name?"})))
    cid = st.create_conversation()
    ev = list(svc.send(cid, "hi"))[-1]
    st.close()
    stream2, seen2 = scripted(R(Token("Hello Zoe")))
    svc2 = ChatService(Storage(cfg["db_path"]), cfg, stream2)
    list(svc2.resume(ev.id, {"value": "Zoe"}))
    assert svc2.storage.get_path(cid)[-1]["content"] == "Hello Zoe"
    assert seen2[0]["messages"][-1]["content"] == "Zoe"


def test_new_message_abandons_a_suspended_turn(tmp_path):
    svc, st, seen, _ = make(tmp_path, R(ToolCall("ask_user", {"question": "Which?"})), R(Token("fine")))
    cid = st.create_conversation()
    list(svc.send(cid, "first"))
    list(svc.send(cid, "never mind"))
    path = st.get_path(cid)
    assert [m["role"] for m in path] == ["user", "assistant", "user", "assistant"]
    assert path[1]["interrupted"] == 1 and path[1]["tool_trace"][0]["calls"][0]["status"] == "waiting"
    assert st.pending_for(cid) == []
    assert seen[1]["messages"][2]["role"] == "assistant"
    assert any("no result" in m["content"] for m in seen[1]["messages"] if m["role"] == "tool")


def test_abort_pending_saves_an_interrupted_reply(tmp_path):
    svc, st, *_ = make(tmp_path, R(ToolCall("ask_user", {"question": "?"})))
    cid = st.create_conversation()
    ev = list(svc.send(cid, "hi"))[-1]
    assert svc.abort_pending(ev.id) is True and svc.abort_pending(ev.id) is False
    assert st.get_path(cid)[-1]["interrupted"] == 1


def test_closing_the_stream_during_a_tool_call_keeps_the_trace(tmp_path):
    svc, st, *_ = make(tmp_path, R(ToolCall("calculate", {"expression": "1+1"})))
    cid = st.create_conversation()
    gen = svc.send(cid, "go")
    assert isinstance(next(gen), Started) and isinstance(next(gen), ToolStart)
    gen.close()
    last = st.get_path(cid)[-1]
    assert last["role"] == "assistant" and last["interrupted"] == 1 and last["tool_trace"]


def test_tool_rounds_are_capped(tmp_path):
    loop = R(ToolCall("calculate", {"expression": "1+1"}))
    svc, st, *_ = make(tmp_path, loop, loop, loop, loop, max_tool_rounds=2)
    cid = st.create_conversation()
    events = list(svc.send(cid, "loop forever"))
    assert isinstance(events[-1], Finished)
    assert st.get_path(cid)[-1]["content"] == "(Stopped after 2 tool rounds.)"


def test_trace_survives_fork_and_shows_in_exports(tmp_path):
    svc, st, *_ = make(tmp_path, R(ToolCall("calculate", {"expression": "2*21"})), R(Token("42")))
    cid = st.create_conversation("Math")
    list(svc.send(cid, "2*21?"))
    forked = st.fork_conversation(cid)
    assert st.get_path(forked)[-1]["tool_trace"][0]["calls"][0]["result"] == "42"
    from src.core.export import export_json, export_markdown
    assert export_json(st, cid)["messages"][1]["tool_trace"][0]["calls"][0]["name"] == "calculate"
    assert "🔧 `calculate(" in export_markdown(st, cid)


def test_old_databases_get_the_tool_trace_column(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, parent_id INTEGER, "
                      "role TEXT, content TEXT, thinking TEXT, prompt_tokens INT, gen_tokens INT, duration_s REAL, "
                      "interrupted INT DEFAULT 0, created_at TEXT);")
    con.commit(); con.close()
    st = Storage(path)
    assert "tool_trace" in {r[1] for r in st.db.execute("PRAGMA table_info(messages)")}


def sse(text):
    return [(b.splitlines()[0][7:], json.loads(b.splitlines()[1][6:])) for b in text.strip().split("\n\n")]


def test_api_tool_events_ask_answer_and_abort(tmp_path):
    svc, st, seen, cfg = make(tmp_path,
                              R(ToolCall("calculate", {"expression": "1+2"})), R(Token("3")),                 # turn 1
                              R(ToolCall("ask_user", {"question": "Colour?", "options": ["red", "blue"]})),   # turn 2
                              R(Token("blue then")),
                              R(ToolCall("ask_user", {"question": "Size?"})))                                  # turn 3
    client = TestClient(server.create_app(cfg, st, svc.stream_fn))
    assert client.get("/api/config").json()["tools_enabled"] is True
    cid = client.post("/api/conversations").json()["id"]

    ev = sse(client.post(f"/api/conversations/{cid}/messages", json={"content": "1+2?"}).text)
    assert [e for e, _ in ev] == ["started", "tool_call", "tool_result", "token", "finished"]
    assert ev[2][1] == {"name": "calculate", "result": "3", "status": "done"}
    detail = client.get(f"/api/conversations/{cid}").json()
    assert detail["messages"][-1]["tool_trace"][0]["calls"][0]["name"] == "calculate" and detail["pending"] is None

    ev = sse(client.post(f"/api/conversations/{cid}/messages", json={"content": "pick"}).text)
    assert [e for e, _ in ev] == ["started", "tool_call", "interaction"]
    question = ev[-1][1]
    assert question["kind"] == "ask" and question["options"] == ["red", "blue"]
    assert client.get(f"/api/conversations/{cid}").json()["pending"]["id"] == question["id"]

    url = f"/api/interactions/{question['id']}/answer"
    assert client.post(url, json={}).status_code == 422
    assert client.post("/api/interactions/999/answer", json={"value": "x"}).status_code == 404
    ev = sse(client.post(url, json={"value": "blue"}).text)
    assert [e for e, _ in ev] == ["tool_result", "token", "finished"] and ev[0][1]["status"] == "answered"
    assert client.post(url, json={"value": "blue"}).status_code == 404

    ev = sse(client.post(f"/api/conversations/{cid}/messages", json={"content": "again"}).text)
    pid = ev[-1][1]["id"]
    detail = client.post(f"/api/interactions/{pid}/abort").json()
    assert detail["pending"] is None and detail["messages"][-1]["interrupted"] == 1
    assert client.post(f"/api/interactions/{pid}/abort").status_code == 404


def test_api_confirm_requires_an_explicit_decision(tmp_path):
    svc, st, seen, cfg = make(tmp_path, R(ToolCall("save_note", {"text": "milk"})), R(Token("done")))
    client = TestClient(server.create_app(cfg, st, svc.stream_fn))
    cid = client.post("/api/conversations").json()["id"]
    pid = sse(client.post(f"/api/conversations/{cid}/messages", json={"content": "note"}).text)[-1][1]["id"]
    assert client.post(f"/api/interactions/{pid}/answer", json={}).status_code == 422
    ev = sse(client.post(f"/api/interactions/{pid}/answer", json={"approved": True}).text)
    assert ev[0][1]["status"] == "done" and "milk" in (tmp_path / "notes.md").read_text()
