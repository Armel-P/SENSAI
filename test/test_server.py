import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from src import server
from src.core.chat import ChatService
from src.core.ollama_client import Done, OllamaUnavailable, Thinking, Token
from src.core.storage import Storage


def ok_stream(url, model, messages, num_ctx=8192, think=None):
    yield Thinking("hmm")
    yield Token("Hel")
    yield Token("lo")
    yield Done(prompt_tokens=12, gen_tokens=2, duration_s=0.5)


def make_app(tmp_path, stream_fn=ok_stream):
    cfg = {"ollama_url": "http://x", "model": "m", "num_ctx": 4096,
           "system_prompt": "BASE", "profile_path": str(tmp_path / "user.yaml"),
           "db_path": str(tmp_path / "t.db")}
    st = Storage(cfg["db_path"])
    return server.create_app(cfg, st, stream_fn), st, cfg


@pytest.fixture
def env(tmp_path):
    app, st, cfg = make_app(tmp_path)
    return TestClient(app), st, cfg


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def send(client, cid, content, **kw):
    r = client.post(f"/api/conversations/{cid}/messages", json={"content": content, **kw})
    return r, (parse_sse(r.text) if r.status_code == 200 else None)


def test_create_list_and_hide_empty(env):
    client, *_ = env
    cid = client.post("/api/conversations").json()["id"]
    assert client.get("/api/conversations").json() == []            # empty: hidden
    assert len(client.get("/api/conversations?include_empty=true").json()) == 1
    send(client, cid, "hello")
    assert [c["id"] for c in client.get("/api/conversations").json()] == [cid]


def test_streaming_turn_and_detail(env):
    client, *_ = env
    cid = client.post("/api/conversations").json()["id"]
    r, events = send(client, cid, "hi")
    assert r.headers["content-type"].startswith("text/event-stream")
    assert [e for e, _ in events] == ["started", "thinking", "token", "token", "finished"]
    assert "".join(d["text"] for e, d in events if e == "token") == "Hello"
    assert events[-1][1]["gen_tokens"] == 2

    d = client.get(f"/api/conversations/{cid}").json()
    assert [m["content"] for m in d["messages"]] == ["hi", "Hello"]
    assert d["messages"][1]["thinking"] == "hmm"
    assert d["usage"]["last_prompt_tokens"] == 12 and d["usage"]["num_ctx"] == 4096
    assert d["messages"][0]["siblings"] == [d["messages"][0]["id"]]


def test_validation_errors(env):
    client, *_ = env
    cid = client.post("/api/conversations").json()["id"]
    assert send(client, cid, "   ")[0].status_code == 422
    assert send(client, 999, "x")[0].status_code == 404
    assert client.get("/api/conversations/999").status_code == 404
    r, _ = send(client, cid, "hi")
    d = client.get(f"/api/conversations/{cid}").json()
    assistant_id = d["messages"][1]["id"]
    assert send(client, cid, "x", edit_message_id=assistant_id)[0].status_code == 422
    assert send(client, cid, "x", edit_message_id=9999)[0].status_code == 404


def test_edit_forks_and_switch_branch(env):
    client, *_ = env
    cid = client.post("/api/conversations").json()["id"]
    send(client, cid, "first")
    send(client, cid, "second")
    first_id = client.get(f"/api/conversations/{cid}").json()["messages"][0]["id"]

    send(client, cid, "first, edited", edit_message_id=first_id)
    d = client.get(f"/api/conversations/{cid}").json()
    assert [m["content"] for m in d["messages"]] == ["first, edited", "Hello"]
    assert len(d["messages"][0]["siblings"]) == 2

    d = client.post(f"/api/messages/{first_id}/switch").json()   # back to the original
    assert [m["content"] for m in d["messages"]] == ["first", "Hello", "second", "Hello"]
    assert client.post("/api/messages/9999/switch").status_code == 404


def test_ollama_error_becomes_sse_event_and_rolls_back(tmp_path):
    def down(*a, **k):
        raise OllamaUnavailable("Ollama is down")
        yield
    app, st, _ = make_app(tmp_path, down)
    client = TestClient(app)
    cid = client.post("/api/conversations").json()["id"]
    r, events = send(client, cid, "hi")
    assert r.status_code == 200
    assert events[-1] == ("error", {"kind": "OllamaUnavailable", "message": "Ollama is down"})
    assert client.get(f"/api/conversations/{cid}").json()["messages"] == []


def test_rename_and_delete(env):
    client, *_ = env
    cid = client.post("/api/conversations").json()["id"]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "Renamed"}).json()["title"] == "Renamed"
    assert client.patch(f"/api/conversations/{cid}", json={"title": ""}).status_code == 422
    assert client.delete(f"/api/conversations/{cid}").json() == {"deleted": cid}
    assert client.delete(f"/api/conversations/{cid}").status_code == 404


def test_profile_roundtrip_reaches_the_prompt(tmp_path):
    seen = {}

    def spy(url, model, messages, num_ctx=8192, think=None):
        seen["system"] = messages[0]["content"]
        yield Done(1, 1, 0.1)

    app, *_ = make_app(tmp_path, spy)
    client = TestClient(app)
    assert client.get("/api/profile").json() == {}
    body = {"name": "Léa", "preferences": ["Be brief"], "instructions": "I use Python."}
    assert client.put("/api/profile", json=body).json() == body
    assert client.get("/api/profile").json() == body
    cid = client.post("/api/conversations").json()["id"]
    send(client, cid, "hi")
    assert "Léa" in seen["system"] and "Be brief" in seen["system"]


def test_models_endpoint(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(server, "list_models", lambda url: ["qwen3:8b", "bge-m3"])
    assert client.get("/api/models").json() == {"models": ["qwen3:8b", "bge-m3"], "default": "m"}

    def boom(url):
        raise OllamaUnavailable("not running")
    monkeypatch.setattr(server, "list_models", boom)
    assert client.get("/api/models").status_code == 503


def test_stop_button_saves_partial_reply(tmp_path):
    """Browser aborts the fetch -> async generator closed -> partial reply saved."""
    app, st, cfg = make_app(tmp_path)
    cid = st.create_conversation()
    svc = ChatService(st, cfg, ok_stream)

    async def scenario():
        agen = server._stream(svc.send(cid, "write an essay"))
        await agen.__anext__()   # started
        await agen.__anext__()   # thinking
        await agen.__anext__()   # token "Hel"
        await agen.aclose()      # <- what Starlette does when the client disconnects

    asyncio.run(scenario())
    last = st.get_path(cid)[-1]
    assert last["role"] == "assistant" and last["content"] == "Hel" and last["interrupted"] == 1


def test_storage_is_usable_from_several_threads(tmp_path):
    st = Storage(str(tmp_path / "t.db"))
    ids = []
    t = threading.Thread(target=lambda: ids.append(st.create_conversation("from thread")))
    t.start(); t.join()
    assert st.get_conversation(ids[0])["title"] == "from thread"
