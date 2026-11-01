import pytest
from fastapi.testclient import TestClient

from src import server
from src.core.chat import ChatService, ToolResult
from src.core.ollama_client import Done, Token, ToolCall
from src.core.personas import PersonaStore
from src.core.storage import Storage
from src.core.tools import ToolRegistry
from src.core.workspace import FilePolicy, Workspace, file_tools


def write_personas(directory):
    directory.mkdir()
    (directory / "coder.yaml").write_text(
        "name: Coder\nrole: Writes code\nsystem_prompt: |\n  You are the CODER.\ntools: [list_dir, write_file]\n"
        "files:\n  read: ['**']\n  write: ['backend/**']\nmodel: coder-model\n")
    (directory / "poet.yaml").write_text("name: Poet\nsystem_prompt: You are the POET.\ntools: []\n")
    (directory / "broken.yaml").write_text("name: no prompt\n")
    (directory / "bad id.yaml").write_text("system_prompt: x\n")


def test_store_loads_lists_and_skips_invalid(tmp_path):
    write_personas(tmp_path / "p")
    store = PersonaStore(str(tmp_path / "p"))
    assert [p.id for p in store.list()] == ["coder", "poet"]
    coder = store.get("coder")
    assert coder.name == "Coder" and coder.system_prompt == "You are the CODER." and coder.model == "coder-model"
    assert coder.tools == ["list_dir", "write_file"] and coder.files["write"] == ["backend/**"]
    assert store.get("broken") is None and store.get("nope") is None
    assert store.get("../coder") is None and store.get(None) is None
    assert PersonaStore(str(tmp_path / "missing")).list() == []


def test_the_shipped_personas_are_valid():
    ids = [p.id for p in PersonaStore("personas").list()]
    assert {"assistant", "pm", "backend", "frontend", "tester", "reviewer"} <= set(ids)
    pm = PersonaStore("personas").get("pm")
    assert pm.files["write"] == ["PROJECT.md"]


def scripted(*rounds):
    seen, it = [], iter(rounds)

    def stream(url, model, messages, num_ctx=8192, think=None, tools=None):
        seen.append({"model": model, "messages": [dict(m) for m in messages],
                     "tools": [t["function"]["name"] for t in tools] if tools else None})
        yield from next(it)
    return stream, seen


def R(*events):
    return [*events, Done(5, 2, 0.1)]


def make(tmp_path, *rounds, policy=None):
    write_personas(tmp_path / "personas")
    ws = Workspace(str(tmp_path / "project"))
    registry = ToolRegistry(file_tools(ws, policy or FilePolicy()))
    stream, seen = scripted(*rounds)
    cfg = {"ollama_url": "x", "model": "base-model", "num_ctx": 8192, "system_prompt": "BASE", "tools": True,
           "profile_path": str(tmp_path / "none.yaml"), "db_path": str(tmp_path / "t.db"),
           "personas_dir": str(tmp_path / "personas")}
    st = Storage(cfg["db_path"])
    return ChatService(st, cfg, stream, registry), st, seen, ws, cfg


def test_persona_sets_prompt_model_and_the_offered_tools(tmp_path):
    svc, st, seen, *_ = make(tmp_path, R(Token("ok")))
    cid = st.create_conversation(persona="coder")
    list(svc.send(cid, "hi"))
    assert seen[0]["messages"][0]["content"] == "You are the CODER."
    assert seen[0]["model"] == "coder-model"
    assert seen[0]["tools"] == ["list_dir", "write_file"]


def test_without_persona_the_default_prompt_and_all_tools_apply(tmp_path):
    svc, st, seen, *_ = make(tmp_path, R(Token("ok")))
    list(svc.send(st.create_conversation(), "hi"))
    assert seen[0]["messages"][0]["content"] == "BASE" and seen[0]["model"] == "base-model"
    assert seen[0]["tools"] == ["list_dir", "read_file", "write_file", "delete_file"]


def test_a_tool_outside_the_persona_is_refused_even_if_the_model_calls_it(tmp_path):
    svc, st, seen, ws, _ = make(tmp_path, R(ToolCall("read_file", {"path": "x"})), R(Token("ok")))
    cid = st.create_conversation(persona="coder")
    result = [e for e in svc.send(cid, "hi") if isinstance(e, ToolResult)][0]
    assert result.status == "error" and "unknown tool" in result.result


def test_switching_persona_mid_conversation_keeps_the_history(tmp_path):
    svc, st, seen, *_ = make(tmp_path, R(Token("as coder")), R(Token("as poet")))
    cid = st.create_conversation(persona="coder")
    list(svc.send(cid, "first question"))
    st.update_conversation(cid, persona="poet")
    list(svc.send(cid, "second question"))
    second = seen[1]["messages"]
    assert second[0]["content"] == "You are the POET."
    assert [m["content"] for m in second[1:]] == ["first question", "as coder", "second question"]
    assert seen[1]["tools"] is None or seen[1]["tools"] == []


def test_an_agent_writes_a_file_only_inside_its_permissions(tmp_path):
    policy = FilePolicy.from_persona({"read": ["**"], "write": ["backend/**"]})
    svc, st, seen, ws, _ = make(
        tmp_path,
        R(ToolCall("write_file", {"path": "backend/app.py", "content": "print(1)\n"}),
          ToolCall("write_file", {"path": "frontend/app.js", "content": "x"})),
        R(Token("done")), policy=policy)
    cid = st.create_conversation(persona="coder")
    results = [e for e in svc.send(cid, "build it") if isinstance(e, ToolResult)]
    assert [r.status for r in results] == ["done", "error"]
    assert "not permitted" in results[1].result and "backend/**" in results[1].result
    assert (ws.root / "backend/app.py").read_text() == "print(1)\n" and not (ws.root / "frontend").exists()
    assert "not permitted" in seen[1]["messages"][-1]["content"]


def test_api_personas_and_switching(tmp_path):
    svc, st, seen, ws, cfg = make(tmp_path, R(Token("x")))
    client = TestClient(server.create_app(cfg, st, svc.stream_fn))
    assert [p["id"] for p in client.get("/api/personas").json()] == ["coder", "poet"]
    cid = client.post("/api/conversations").json()["id"]
    assert client.patch(f"/api/conversations/{cid}", json={"persona": "poet"}).json()["persona"] == "poet"
    assert client.patch(f"/api/conversations/{cid}", json={"persona": "ghost"}).status_code == 422
    assert client.patch(f"/api/conversations/{cid}", json={"persona": ""}).json()["persona"] is None
    both = client.patch(f"/api/conversations/{cid}", json={"title": "T", "persona": "coder"}).json()
    assert both["title"] == "T" and both["persona"] == "coder"
    assert client.patch(f"/api/conversations/{cid}", json={}).status_code == 422
    assert client.patch(f"/api/conversations/{cid}", json={"title": ""}).status_code == 422
