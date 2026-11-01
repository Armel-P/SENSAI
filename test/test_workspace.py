import os

import pytest

from src.core.tools import ToolRegistry
from src.core.workspace import FilePolicy, PermissionDenied, Workspace, file_tools, matches


@pytest.fixture
def ws(tmp_path):
    return Workspace(str(tmp_path / "project"))


def tools(ws, **policy):
    return {t.name: t for t in file_tools(ws, FilePolicy(**policy) if policy else FilePolicy())}


def call(tool, **args):
    return tool.handler(args)


def test_glob_matching():
    assert matches(["backend/**"], "backend/a/b.py")
    assert matches(["backend/**"], "backend", is_dir=True)
    assert not matches(["backend/**"], "frontend/a.js")
    assert matches(["**/*.md"], "README.md") and matches(["**/*.md"], "docs/x/y.md")
    assert not matches(["*.md"], "docs/x.md") and matches(["*.md"], "x.md")
    assert matches(["tests/**"], "tests/test_a.py") and not matches(["tests/**"], "tests_old/a.py")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.txt", "a/../../outside.txt", "~/x", "C:\\Windows\\x", "a\\..\\..\\x"])
def test_paths_that_escape_are_refused(ws, bad):
    with pytest.raises(PermissionDenied):
        ws.resolve(bad)


def test_a_symlink_cannot_lead_outside_the_workspace(ws, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key.txt").write_text("top secret")
    os.symlink(outside, ws.root / "link")
    with pytest.raises(PermissionDenied):
        call(tools(ws)["read_file"], path="link/key.txt")
    with pytest.raises(PermissionDenied):
        call(tools(ws)["write_file"], path="link/new.txt", content="x")
    assert not (outside / "new.txt").exists()


def test_write_then_read_with_nested_folders(ws):
    t = tools(ws)
    assert call(t["write_file"], path="backend/app/main.py", content="print('hi')\n").startswith("Wrote 12")
    assert (ws.root / "backend/app/main.py").read_text() == "print('hi')\n"
    assert call(t["read_file"], path="backend/app/main.py") == "print('hi')\n"
    assert "backend/" in call(t["list_dir"], path=".") and "main.py" in call(t["list_dir"], path="backend/app")


def test_write_permission_is_per_folder_and_the_error_teaches_the_agent(ws):
    t = tools(ws, read=["**"], write=["backend/**"])
    call(t["write_file"], path="backend/ok.py", content="x")
    with pytest.raises(PermissionDenied) as err:
        call(t["write_file"], path="frontend/app.js", content="x")
    assert "Allowed to write: backend/**" in str(err.value)
    assert not (ws.root / "frontend").exists()
    with pytest.raises(PermissionDenied):
        call(t["delete_file"], path="frontend/app.js")


def test_a_read_only_agent_cannot_write_anything(ws):
    t = tools(ws, read=["**"], write=[])
    with pytest.raises(PermissionDenied):
        call(t["write_file"], path="a.txt", content="x")


def test_deny_list_wins_over_allow_all(ws):
    (ws.root / ".env").write_text("SECRET=1")
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "config").write_text("x")
    (ws.root / "notes.txt").write_text("fine")
    t = tools(ws)
    for secret in (".env", ".git/config"):
        with pytest.raises(PermissionDenied):
            call(t["read_file"], path=secret)
    with pytest.raises(PermissionDenied):
        call(t["write_file"], path=".env", content="x")
    listing = call(t["list_dir"], path=".")
    assert "notes.txt" in listing and ".env" not in listing and ".git" not in listing


def test_list_dir_hides_what_the_agent_may_not_read(ws):
    (ws.root / "public").mkdir(); (ws.root / "private").mkdir()
    (ws.root / "public" / "a.txt").write_text("a"); (ws.root / "private" / "b.txt").write_text("b")
    t = tools(ws, read=["public/**"], write=[])
    assert call(t["list_dir"], path=".").strip() == "public/"
    with pytest.raises(PermissionDenied):
        call(t["list_dir"], path="private")


def test_read_file_truncates_and_pages_with_offset(ws):
    (ws.root / "big.txt").write_text("x" * 9000)
    t = tools(ws)
    first = call(t["read_file"], path="big.txt")
    assert "offset=3500" in first and len(first) < 3700
    last = call(t["read_file"], path="big.txt", offset=7000)
    assert last == "x" * 2000


def test_read_and_write_limits_and_bad_files(ws):
    small = Workspace(str(ws.root), max_bytes=100)
    t = tools(small)
    with pytest.raises(ValueError, match="too large"):
        call(t["write_file"], path="a.txt", content="x" * 101)
    (ws.root / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ValueError, match="not a text file"):
        call(tools(ws)["read_file"], path="bin.dat")
    with pytest.raises(FileNotFoundError):
        call(tools(ws)["read_file"], path="missing.txt")


def test_delete_is_flagged_for_user_approval_and_writes_optionally(ws):
    t = tools(ws)
    assert t["delete_file"].confirm is True and t["write_file"].confirm is False
    strict = {x.name: x for x in file_tools(ws, FilePolicy(), confirm_writes=True)}
    assert strict["write_file"].confirm is True


def test_policy_from_a_persona_file_section():
    policy = FilePolicy.from_persona({"read": ["**"], "write": ["tests/**"]})
    policy.check("write", "tests/test_a.py")
    with pytest.raises(PermissionDenied):
        policy.check("write", "backend/a.py")
    with pytest.raises(PermissionDenied):
        policy.check("read", ".env")
    assert FilePolicy.from_persona({}).write == []
