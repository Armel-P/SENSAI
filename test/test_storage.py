import pytest
from src.core.storage import Storage

@pytest.fixture
def st():
    s = Storage(":memory:")
    yield s
    s.close()


def chat(st, conv):
    u1 = st.add_message(conv, "user", "hello")
    a1 = st.add_message(conv, "assistant", "hi!", prompt_tokens=10, gen_tokens=3)
    u2 = st.add_message(conv, "user", "what is 2+2?")
    a2 = st.add_message(conv, "assistant", "4", prompt_tokens=25, gen_tokens=2)
    return u1, a1, u2, a2


def test_linear_path_and_context(st):
    c = st.create_conversation("t")
    chat(st, c)
    ctx = st.build_context(c, "SYS")
    assert [m["role"] for m in ctx] == ["system", "user", "assistant", "user", "assistant"]
    assert ctx[0]["content"] == "SYS"
    assert ctx[-1]["content"] == "4"


def test_edit_creates_branch_and_keeps_original(st):
    c = st.create_conversation("t")
    u1, a1, u2, a2 = chat(st, c)
    u2b = st.edit_message(u2, "what is 3+3?")
    path = st.get_path(c)
    assert [m["id"] for m in path] == [u1, a1, u2b]
    assert st.get_message(u2) and st.get_message(a2)
    assert st.branch_info(u2b) == (2, 2)


def test_switch_back_to_original_branch(st):
    c = st.create_conversation("t")
    u1, a1, u2, a2 = chat(st, c)
    st.edit_message(u2, "alt")
    st.switch_branch(u2)
    assert st.get_path(c)[-1]["id"] == a2


def test_edit_first_message(st):
    c = st.create_conversation("t")
    u1, *_ = chat(st, c)
    u1b = st.edit_message(u1, "bonjour")
    assert st.get_path(c) == [st.get_message(u1b)]
    assert st.branch_info(u1b) == (2, 2)


def test_only_user_messages_editable(st):
    c = st.create_conversation("t")
    _, a1, *_ = chat(st, c)
    with pytest.raises(ValueError):
        st.edit_message(a1, "nope")


def test_parent_must_belong_to_conversation(st):
    c1, c2 = st.create_conversation("a"), st.create_conversation("b")
    m = st.add_message(c1, "user", "x")
    with pytest.raises(ValueError):
        st.add_message(c2, "user", "y", parent_id=m)


def test_interrupted_partial_reply_is_stored(st):
    c = st.create_conversation("t")
    st.add_message(c, "user", "write an essay")
    a = st.add_message(c, "assistant", "Once upon", interrupted=True)
    assert st.get_message(a)["interrupted"] == 1
    nxt = st.add_message(c, "user", "make it shorter")       # redirect = next child
    assert [m["id"] for m in st.get_path(c)][-2:] == [a, nxt]


def test_token_usage_and_delete_cascade(st):
    c = st.create_conversation("t")
    chat(st, c)
    assert st.token_usage(c) == {"last_prompt_tokens": 25, "generated_total": 5, "messages": 4}
    st.delete_conversation(c)
    assert st.list_conversations() == []
    assert st.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_list_orders_by_recent_activity(st):
    a, b = st.create_conversation("a"), st.create_conversation("b")
    st.add_message(a, "user", "bump")
    assert st.list_conversations()[0]["id"] == a
