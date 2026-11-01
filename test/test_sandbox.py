import os
import sys
import time

import pytest

from src.core.sandbox import Sandbox, namespaces_available, sandbox_tools
from src.core.workspace import FilePolicy, PermissionDenied, Workspace

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the strict sandbox targets Linux")


@pytest.fixture
def ws(tmp_path):
    w = Workspace(str(tmp_path / "project"))
    (w.root / "backend").mkdir()
    (w.root / "backend" / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (w.root / ".env").write_text("SECRET=hunter2")
    return w


@pytest.fixture
def box(ws):
    return Sandbox(ws, timeout_s=4, memory_mb=256, output_chars=600)


def test_runs_code_and_captures_output(box):
    r = box.run_code("print('hello', 6 * 7)")
    assert r.exit_code == 0 and r.stdout.strip() == "hello 42" and not r.timed_out
    assert r.format().startswith("exit code: 0")


def test_a_traceback_comes_back_to_the_agent_without_internal_frames(box):
    r = box.run_code("def f():\n    return 1 / 0\nf()")
    assert r.exit_code == 1 and "ZeroDivisionError" in r.stderr and "_snippet.py" in r.stderr
    assert "sandbox_runner" not in r.stderr and "/tmp/" not in r.stderr


def test_project_modules_can_be_imported_and_args_are_passed(box):
    r = box.run_code("import sys\nfrom backend.app import add\nprint(add(2, 3), sys.argv[1:])", args=["x", "y"])
    assert r.stdout.strip() == "5 ['x', 'y']"
    assert box.run_script("backend/app.py").exit_code == 0


def test_infinite_loop_is_killed_by_the_timeout(box):
    start = time.monotonic()
    r = box.run_code("while True:\n    pass")
    assert r.timed_out and r.exit_code is None and time.monotonic() - start < 10
    assert "timed out after 4s" in r.format()


def test_memory_bomb_is_stopped(box):
    r = box.run_code("x = bytearray(1024 * 1024 * 1024)\nprint('allocated')")
    assert "allocated" not in r.stdout and ("MemoryError" in r.stderr or (r.exit_code or 0) != 0)


def test_huge_output_is_truncated_not_buffered(box):
    r = box.run_code("print('x' * 3_000_000)")
    assert len(r.stdout) <= 700 and "truncated" in r.stdout


@pytest.mark.parametrize("code,needle", [
    ("import os\nos.fork()", "os.fork"),
    ("import os\nos.system('echo pwned')", "os.system"),
    ("import subprocess\nsubprocess.run(['ls'])", "subprocess.Popen"),
    ("import os\nos.execv('/bin/ls', ['ls'])", "os.exec"),
    ("import os, signal\nos.kill(os.getppid(), signal.SIGKILL)", "os.kill"),
    ("import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=1)", "socket"),
    ("import socket\nsocket.socket().bind(('0.0.0.0', 8000))", "socket.bind"),
    ("import ctypes", "ctypes"),
])
def test_dangerous_calls_are_refused(box, code, needle):
    r = box.run_code("try:\n" + "".join("    " + l + "\n" for l in code.splitlines()) +
                     "except BaseException as e:\n    print(type(e).__name__, e)\n    raise SystemExit(3)")
    assert r.exit_code == 3 and "not allowed" in r.stdout and needle in r.stdout, r.format()


def test_secrets_and_the_host_are_not_readable(box, ws, monkeypatch):
    monkeypatch.setenv("SENSAI_SECRET_TOKEN", "leak-me")
    r = box.run_code(
        "import os\n"
        "print('env:', 'SENSAI_SECRET_TOKEN' in os.environ)\n"
        "for p in ('/etc/passwd', os.path.expanduser('~/.ssh/id_rsa'), '/proc/1/environ'):\n"
        "    try:\n        open(p).read(); print('READ', p)\n"
        "    except (PermissionError, FileNotFoundError) as e:\n        print('blocked', type(e).__name__)\n"
        "try:\n    open('.env').read(); print('READ .env')\n"
        "except FileNotFoundError:\n    print('no .env')\n"
        "try:\n    os.listdir('/home'); print('LISTED')\nexcept PermissionError:\n    print('list blocked')")
    assert "env: False" in r.stdout and "READ" not in r.stdout and "LISTED" not in r.stdout
    assert r.stdout.count("blocked") == 4 and "no .env" in r.stdout and "list blocked" in r.stdout


def test_the_real_project_cannot_be_touched_even_by_absolute_path(box, ws):
    target = ws.root / "backend" / "app.py"
    r = box.run_code(f"import os\nfor f in (lambda: open({str(target)!r}, 'w').write('HACKED'),\n"
                     f"          lambda: os.remove({str(target)!r}),\n"
                     f"          lambda: open({str(ws.root / 'new.txt')!r}, 'w')):\n"
                     "    try:\n        f(); print('DID IT')\n    except PermissionError:\n        print('denied')")
    assert r.stdout.count("denied") == 3 and "DID IT" not in r.stdout
    assert "return a + b" in target.read_text() and not (ws.root / "new.txt").exists()


def test_writes_inside_the_copy_work_but_never_persist(box, ws):
    r = box.run_code("open('out.txt', 'w').write('data')\nimport os\nos.makedirs('sub/dir')\n"
                     "print(open('out.txt').read(), os.path.exists('sub/dir'))")
    assert r.stdout.strip() == "data True"
    assert not (ws.root / "out.txt").exists() and not (ws.root / "sub").exists()
    assert not (ws.root / "_snippet.py").exists() and not (ws.root / ".sandbox_tmp").exists()


def test_symlinks_and_secrets_are_not_copied(box, ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside data")
    os.symlink(outside, ws.root / "link.txt")
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "config").write_text("x")
    r = box.run_code("import os\nprint(sorted(os.listdir('.')))")
    assert "link.txt" not in r.stdout and ".env" not in r.stdout and ".git" not in r.stdout and "backend" in r.stdout


def test_sqlite_files_outside_the_copy_are_refused(box, tmp_path):
    r = box.run_code(f"import sqlite3\ntry:\n    sqlite3.connect({str(tmp_path / 'x.db')!r}); print('OPENED')\n"
                     "except PermissionError:\n    print('denied')\nsqlite3.connect(':memory:').execute('select 1'); print('memory ok')\n"
                     "sqlite3.connect('local.db').execute('create table t(x)'); print('local ok')")
    assert r.stdout.split() == ["denied", "memory", "ok", "local", "ok"], r.format()


def test_run_tests_reports_passes_and_failures(ws):
    (ws.root / "tests").mkdir()
    (ws.root / "tests" / "test_app.py").write_text(
        "from backend.app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    box = Sandbox(ws, timeout_s=30, memory_mb=768, output_chars=1500)
    ok = box.run_tests()
    assert ok.exit_code == 0 and "1 passed" in ok.stdout, ok.format()
    (ws.root / "tests" / "test_bad.py").write_text("from backend.app import add\n\ndef test_bad():\n    assert add(2, 2) == 5\n")
    bad = box.run_tests("tests")
    assert bad.exit_code == 1 and "assert 4 == 5" in bad.stdout and "test_bad.py" in bad.stdout, bad.format()
    assert not (ws.root / ".pytest_cache").exists() and not list(ws.root.rglob("__pycache__"))


def test_tests_can_use_tmp_path_and_fastapi_testclient(ws):
    (ws.root / "backend" / "api.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ping')\ndef ping():\n    return {'ok': True}\n")
    (ws.root / "tests").mkdir()
    (ws.root / "tests" / "test_api.py").write_text(
        "from fastapi.testclient import TestClient\nfrom backend.api import app\n\n"
        "def test_ping(tmp_path):\n    (tmp_path / 'f.txt').write_text('x')\n    assert TestClient(app).get('/ping').json() == {'ok': True}\n")
    r = Sandbox(ws, timeout_s=60, memory_mb=1024, output_chars=1500).run_tests("tests")
    assert r.exit_code == 0 and "1 passed" in r.stdout, r.format()


def test_too_large_projects_are_refused(ws):
    for i in range(6):
        (ws.root / f"f{i}.txt").write_text("x")
    with pytest.raises(ValueError, match="too large"):
        Sandbox(ws, max_files=5).run_code("print(1)")


def test_isolation_info_is_reported(box):
    info = box.info()
    assert info["timeout_s"] == 4 and info["network"].startswith("none" if namespaces_available() else "blocked")


def tools_for(ws, **policy):
    box = Sandbox(ws, timeout_s=30, memory_mb=768, output_chars=1500)
    return {t.name: t for t in sandbox_tools(ws, FilePolicy(**policy) if policy else FilePolicy(), box)}


def test_run_python_tool_modes_and_validation(ws):
    t = tools_for(ws)["run_python"]
    assert "exit code: 0" in t.handler({"code": "print('hi')"}) and "hi" in t.handler({"code": "print('hi')"})
    assert "exit code: 0" in t.handler({"path": "backend/app.py"})
    for bad in ({}, {"path": "backend/app.py", "code": "1"}):
        with pytest.raises(ValueError, match="exactly one"):
            t.handler(bad)
    with pytest.raises(ValueError, match="not a .py file"):
        t.handler({"path": "backend"})
    with pytest.raises(PermissionDenied):
        t.handler({"path": "../outside.py"})
    with pytest.raises(PermissionDenied):
        t.handler({"path": ".env"})


def test_the_tool_respects_the_agents_read_permissions(ws):
    t = tools_for(ws, read=["tests/**"], write=[])
    with pytest.raises(PermissionDenied):
        t["run_python"].handler({"path": "backend/app.py"})
    with pytest.raises(PermissionDenied):
        t["run_tests"].handler({"path": "backend"})
