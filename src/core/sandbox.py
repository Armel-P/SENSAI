"""T2: run code the agents wrote, in a STRICT sandbox, and give stdout / tracebacks back to them.

Threat model: not a hostile attacker but a small model making mistakes (rm -rf, infinite loop,
fork bomb, huge allocation, network call, writing to the real project by absolute path).
Layers (each tested):  fresh temporary COPY of the workspace per run (nothing persists, secrets
are not copied) · scrubbed environment · wall-clock timeout + process-group kill · resource limits
· audit hook (no subprocess / fork / signals / sockets / ctypes, reads and writes confined)
· network namespace via `unshare -Urn` when the system allows it.
A subprocess is NOT a security boundary against a determined attacker: Docker is stronger.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tools import Tool
from .workspace import DEFAULT_DENY, FilePolicy, Workspace, matches

RUNNER = Path(__file__).with_name("sandbox_runner.py")
_NS_OK: Optional[bool] = None


def namespaces_available() -> bool:
    """Can we run the child in its own user + network namespace (no network at all)?"""
    global _NS_OK
    if _NS_OK is None:
        _NS_OK = False
        if sys.platform.startswith("linux") and shutil.which("unshare"):
            try:
                _NS_OK = subprocess.run(["unshare", "-Urn", "true"], capture_output=True, timeout=5).returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
    return _NS_OK


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + "\n[… output truncated …]\n" + text[-(limit - head):]


@dataclass
class RunResult:
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    timeout_s: int = 0

    def format(self) -> str:
        note = ""
        if self.timed_out:
            note = f" (killed: timed out after {self.timeout_s}s)"
        elif self.exit_code is not None and self.exit_code < 0:
            sig = {9: "killed, probably out of memory", 24: "CPU time limit exceeded", 25: "output file too large"}
            note = f" ({sig.get(-self.exit_code, f'killed by signal {-self.exit_code}')})"
        return (f"exit code: {self.exit_code}{note}\n--- stdout ---\n{self.stdout or '(empty)'}\n"
                f"--- stderr ---\n{self.stderr or '(empty)'}")


class Sandbox:
    def __init__(self, workspace: Workspace, timeout_s: int = 20, memory_mb: int = 768,
                 output_chars: int = 1800, max_files: int = 500, max_bytes: int = 20_000_000):
        self.ws, self.timeout_s, self.memory_mb = workspace, int(timeout_s), int(memory_mb)
        self.output_chars, self.max_files, self.max_bytes = int(output_chars), max_files, max_bytes

    def info(self) -> dict:
        return {"network": "none (namespace)" if namespaces_available() else "blocked by audit hook only",
                "timeout_s": self.timeout_s, "memory_mb": self.memory_mb}

    # ---- public API
    def run_code(self, code: str, args=()) -> RunResult:
        return self._run("script", ["_snippet.py", *map(str, args)], snippet=code)

    def run_script(self, rel_path: str, args=()) -> RunResult:
        return self._run("script", [rel_path, *map(str, args)])

    def run_tests(self, target: str = ".") -> RunResult:
        return self._run("pytest", [target])

    # ---- internals
    def _copy_workspace(self, dest: Path) -> None:
        """Copy the project without secrets, symlinks or huge trees: the run works on the copy only."""
        dest.mkdir(parents=True)
        count = total = 0
        for folder, dirs, files in os.walk(self.ws.root, followlinks=False):
            rel_dir = Path(folder).relative_to(self.ws.root).as_posix()
            child = lambda d: d if rel_dir == "." else f"{rel_dir}/{d}"
            dirs[:] = [d for d in dirs if not matches(DEFAULT_DENY, child(d), True)
                       and not os.path.islink(os.path.join(folder, d))]
            (dest / rel_dir).mkdir(parents=True, exist_ok=True)
            for name in files:
                src = Path(folder, name)
                rel = src.relative_to(self.ws.root).as_posix()
                if src.is_symlink() or matches(DEFAULT_DENY, rel):
                    continue
                size = src.stat().st_size
                count, total = count + 1, total + size
                if count > self.max_files or total > self.max_bytes:
                    raise ValueError("the project is too large to run in the sandbox")
                shutil.copy2(src, dest / rel)

    def _run(self, mode: str, rest: list, snippet: Optional[str] = None) -> RunResult:
        tmp = Path(tempfile.mkdtemp(prefix="sensai-sbx-"))
        try:
            work = tmp / "work"
            self._copy_workspace(work)
            (work / ".sandbox_tmp").mkdir(exist_ok=True)
            if snippet is not None:
                (work / "_snippet.py").write_text(snippet, encoding="utf-8")
            cmd = [sys.executable, "-I", "-B", str(RUNNER), str(work), mode, str(self.memory_mb),
                   str(self.timeout_s + 1), *rest]
            if namespaces_available():
                cmd = ["unshare", "-Urn", "--", *cmd]
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(work), "TMPDIR": str(work / ".sandbox_tmp")}
            timed_out, start = False, time.monotonic()
            with open(tmp / "out.txt", "wb") as out, open(tmp / "err.txt", "wb") as err:
                proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                        start_new_session=True, close_fds=True)
                try:
                    proc.wait(timeout=self.timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)       # the whole process group, not just the child
                    except ProcessLookupError:
                        pass
                    proc.wait()
            clean = lambda p: (tmp / p).read_bytes()[: 200_000].decode("utf-8", "replace").replace(str(work) + os.sep, "")
            return RunResult(None if timed_out else proc.returncode, clip(clean("out.txt"), self.output_chars),
                             clip(clean("err.txt"), self.output_chars), round(time.monotonic() - start, 2),
                             timed_out, self.timeout_s)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def sandbox_tools(ws: Workspace, policy: FilePolicy, sandbox: Sandbox) -> list:
    """`run_python` and `run_tests` for one agent (the agent's read permissions apply to what it may run)."""

    def run_python(args):
        path, code = args.get("path"), args.get("code")
        if bool(path) == bool(code):
            raise ValueError("give exactly one of 'path' (a .py file of the project) or 'code' (a snippet)")
        extra = [str(a) for a in (args.get("args") or [])][:10]
        if code:
            return sandbox.run_code(str(code)[:20_000], extra).format()
        target, rel = ws.resolve(path)
        policy.check("read", rel)
        if not target.is_file() or not rel.endswith(".py"):
            raise ValueError(f"'{rel}' is not a .py file of the project")
        return sandbox.run_script(rel, extra).format()

    def run_tests(args):
        target, rel = ws.resolve(args.get("path") or ".")
        if rel:
            policy.check("read", rel, is_dir=target.is_dir())
        return sandbox.run_tests(rel or ".").format()

    return [
        Tool("run_python", "Run a Python file of the project, or a code snippet, in a sandbox (no network, no subprocess, "
             "fresh copy of the project, nothing persists, limited time and memory). Returns the exit code, stdout and "
             "any traceback.",
             {"type": "object", "properties": {"path": {"type": "string", "description": "e.g. backend/app.py"},
                                                "code": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}},
             run_python),
        Tool("run_tests", "Run the pytest tests of the project in the sandbox. Returns the pytest output.",
             {"type": "object", "properties": {"path": {"type": "string", "description": "a test file or folder, default: everything"}}},
             run_tests),
    ]


def make_extra_tools(cfg: dict):
    """The `extra_tools` hook of the orchestrator: gives each agent sandbox tools under ITS permissions."""
    def extra(ws: Workspace, persona):
        sandbox = Sandbox(ws, timeout_s=cfg.get("sandbox_timeout_s", 20), memory_mb=cfg.get("sandbox_memory_mb", 768),
                          output_chars=cfg.get("sandbox_output_chars", 1800))
        return sandbox_tools(ws, FilePolicy.from_persona(persona.files), sandbox)
    return extra
