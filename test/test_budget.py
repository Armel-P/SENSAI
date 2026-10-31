import pytest
from fastapi.testclient import TestClient

from src import server
from src.core.budget import effective_budget, validate_settings
from src.core.chat import ChatService, Compressed
from src.core.ollama_client import Done, OllamaUnavailable, Token
from src.core.storage import Storage


def make(tmp_path, **cfg_extra):
    seen = []

    def stream(url, model, messages, num_ctx=8192, think=None):
        seen.append(messages)
        if messages[0]["content"].startswith("You condense"):
            yield Token("SUMMARY: user likes tea")
            yield Done(5, 5, 0.1)
        else:
            yield Token("ok")
            yield Done(prompt_tokens=100, gen_tokens=1, duration_s=0.1)

    cfg = {"ollama_url": "x", "model": "m", "num_ctx": 4096, "system_prompt": "BASE",
           "profile_path": str(tmp_path / "none.yaml"), "db_path": str(tmp_path / "t.db"),
           "token_budget": 300, "keep_recent": 2, **cfg_extra}
    st = Storage(cfg["db_path"])
    return ChatService(st, cfg, stream), st, seen, cfg


def talk(svc, cid, n, size=200):
    events = []
    for i in range(n):
        events += list(svc.send(cid, f"message {i} " + "x" * size))
    return events


def test_no_compression_under_budget(tmp_path):
    svc, st, seen, _ = make(tmp_path, token_budget=0)
    cid = st.create_conversation()
    assert not any(isinstance(e, Compressed) for e in talk(svc, cid, 6))
    assert st.active_summary(cid) is None


def test_compression_condenses_old_turns_and_keeps_recent(tmp_path):
    svc, st, seen, _ = make(tmp_path)
    cid = st.create_conversation()
    events = talk(svc, cid, 6)
    comps = [e for e in events if isinstance(e, Compressed)]
    assert comps and comps[0].summary_tokens < comps[0].source_tokens
    summ = st.active_summary(cid)
    assert summ and "tea" in summ["content"]
    final = seen[-1]
    assert "SUMMARY: user likes tea" in final[0]["content"]
    assert len(final) <= 1 + 2 + 2
    assert "message 0" not in " ".join(m["content"] for m in final)
    assert len(st.get_path(cid)) == 12
    assert st.summary_info(cid)["messages_condensed"] >= 2


def test_summary_only_applies_to_its_own_branch(tmp_path):
    svc, st, seen, _ = make(tmp_path)
    cid = st.create_conversation()
    talk(svc, cid, 6)
    first_user = st.get_path(cid)[0]["id"]
    st.edit_message(first_user, "a different start")
    assert st.active_summary(cid) is None
    assert "SUMMARY" not in st.build_context(cid, "S")[0]["content"]
    st.switch_branch(first_user)
    assert st.active_summary(cid) is not None


def test_summarizer_failure_does_not_break_the_turn(tmp_path):
    svc, st, seen, cfg = make(tmp_path)
    real = svc.stream_fn

    def flaky(url, model, messages, num_ctx=8192, think=None):
        if messages[0]["content"].startswith("You condense"):
            raise OllamaUnavailable("down")
        yield from real(url, model, messages, num_ctx, think)

    svc.stream_fn = flaky
    cid = st.create_conversation()
    events = talk(svc, cid, 6)
    assert not any(isinstance(e, Compressed) for e in events)
    assert st.get_path(cid)[-1]["content"] == "ok"


def test_settings_validation_and_live_change(tmp_path):
    svc, st, *_ = make(tmp_path)
    assert svc.settings()["effective_budget"] == 300
    assert svc.update_settings({"token_budget": 0})["effective_budget"] == 0
    assert svc.update_settings({"num_ctx": 2048, "token_budget": 5000})["effective_budget"] == 2048 - 512
    for bad in ({"token_budget": 100}, {"num_ctx": 10}, {"keep_recent": 99}, {"x": 1}, {"num_ctx": "9"}):
        with pytest.raises(ValueError):
            validate_settings(bad)
    assert effective_budget({"token_budget": 0, "num_ctx": 8192}) == 0
    # persisted: a new service on the same DB sees the override
    assert ChatService(Storage(svc.cfg["db_path"]), svc.cfg).settings()["num_ctx"] == 2048


def test_settings_api_and_compressed_sse_event(tmp_path):
    svc, st, seen, cfg = make(tmp_path)
    client = TestClient(server.create_app(cfg, st, svc.stream_fn))
    assert client.get("/api/config").json()["effective_budget"] == 300
    r = client.put("/api/settings", json={"token_budget": 400, "keep_recent": 2})
    assert r.json()["token_budget"] == 400
    assert client.put("/api/settings", json={"token_budget": 50}).status_code == 422
    cid = client.post("/api/conversations").json()["id"]
    body = ""
    for i in range(6):
        body = client.post(f"/api/conversations/{cid}/messages",
                           json={"content": f"m{i} " + "y" * 300}).text
        if "event: compressed" in body:
            break
    assert "event: compressed" in body
    usage = client.get(f"/api/conversations/{cid}").json()["usage"]
    assert usage["summary"]["messages_condensed"] >= 2
