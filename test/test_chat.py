import pytest

from src.core.chat import ChatService, Finished, Started
from src.core.ollama_client import Done, OllamaUnavailable, Thinking, Token
from src.core.profile import profile_to_prompt
from src.core.storage import Storage


def make_service(tmp_path, stream_fn, profile=None):
    prof = tmp_path / "user.yaml"
    if profile:
        prof.write_text(profile, encoding="utf-8")
    cfg = {"ollama_url": "http://x", "model": "m", "num_ctx": 4096,
           "system_prompt": "BASE", "profile_path": str(prof)}
    return Storage(":memory:"), cfg, stream_fn


def ok_stream(url, model, messages, num_ctx=8192, think=None):
    yield Thinking("hmm")
    yield Token("Hel")
    yield Token("lo")
    yield Done(prompt_tokens=12, gen_tokens=2, duration_s=0.5)


@pytest.fixture
def svc(tmp_path):
    st, cfg, fn = make_service(tmp_path, ok_stream)
    return ChatService(st, cfg, fn)


def test_normal_turn_is_persisted(svc):
    c = svc.storage.create_conversation()
    events = list(svc.send(c, "  hi there "))
    assert isinstance(events[0], Started)
    assert isinstance(events[-1], Finished)
    path = svc.storage.get_path(c)
    assert [m["content"] for m in path] == ["hi there", "Hello"]
    assert path[1]["thinking"] == "hmm"
    assert (path[1]["prompt_tokens"], path[1]["gen_tokens"]) == (12, 2)
    conv = svc.storage.get_conversation(c)
    assert conv["title"] == "hi there" and conv["model"] == "m"


def test_empty_message_rejected(svc):
    c = svc.storage.create_conversation()
    with pytest.raises(ValueError):
        list(svc.send(c, "   "))
    assert svc.storage.get_path(c) == []


def test_error_before_any_token_rolls_back(tmp_path):
    def failing(*a, **k):
        raise OllamaUnavailable("down")
        yield
    st, cfg, fn = make_service(tmp_path, failing)
    svc = ChatService(st, cfg, fn)
    c = st.create_conversation()
    with pytest.raises(OllamaUnavailable):
        list(svc.send(c, "hello"))
    assert st.get_path(c) == []
    assert st.get_conversation(c)["active_leaf_id"] is None


def test_error_mid_stream_keeps_partial(tmp_path):
    def broken(*a, **k):
        yield Token("par")
        raise OllamaUnavailable("lost")
    st, cfg, fn = make_service(tmp_path, broken)
    svc = ChatService(st, cfg, fn)
    c = st.create_conversation()
    with pytest.raises(OllamaUnavailable):
        list(svc.send(c, "hello"))
    last = st.get_path(c)[-1]
    assert last["content"] == "par" and last["interrupted"] == 1


def test_interrupt_saves_partial_reply(svc):
    c = svc.storage.create_conversation()
    gen = svc.send(c, "write an essay")
    next(gen)            # Started
    next(gen)            # Thinking
    next(gen)            # Token "Hel"
    gen.close()          # user aborts
    last = svc.storage.get_path(c)[-1]
    assert last["role"] == "assistant" and last["content"] == "Hel"
    assert last["interrupted"] == 1


def test_interrupt_before_any_token_rolls_back(svc):
    c = svc.storage.create_conversation()
    gen = svc.send(c, "hi")
    next(gen)            # Started only
    gen.close()
    assert svc.storage.get_path(c) == []


def test_edit_forks_and_regenerates(svc):
    c = svc.storage.create_conversation()
    list(svc.send(c, "first"))
    u1 = svc.storage.get_path(c)[0]["id"]
    list(svc.send(c, "second"))
    list(svc.send(c, "first, edited", edit_message_id=u1))
    path = svc.storage.get_path(c)
    assert [m["content"] for m in path] == ["first, edited", "Hello"]
    assert svc.storage.branch_info(path[0]["id"]) == (2, 2)
    svc.storage.switch_branch(u1)                      # old branch still intact
    assert [m["content"] for m in svc.storage.get_path(c)][:3] == ["first", "Hello", "second"]


def test_profile_is_injected_in_system_prompt(tmp_path):
    seen = {}

    def spy(url, model, messages, num_ctx=8192, think=None):
        seen["messages"] = messages
        yield Done(1, 1, 0.1)

    st, cfg, fn = make_service(tmp_path, spy, profile="name: Alex\npreferences: [Be brief]\n")
    svc = ChatService(st, cfg, fn)
    c = st.create_conversation()
    list(svc.send(c, "hi"))
    system = seen["messages"][0]["content"]
    assert system.startswith("BASE") and "Alex" in system and "Be brief" in system


def test_empty_profile_gives_empty_prompt():
    assert profile_to_prompt({}) == ""
    assert profile_to_prompt({"unknown": 1}) == ""