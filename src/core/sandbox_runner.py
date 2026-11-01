"""Runs INSIDE the sandbox subprocess:  python -I -B sandbox_runner.py ROOT MODE MEM_MB CPU_S [ARGS...]

  MODE script <relpath> [args]   run a Python file          MODE pytest <target>   run the tests

It never imports project code before the restrictions are installed. The restrictions are
  1. resource limits (memory, CPU time, file size, open files),
  2. an audit hook (PEP 578) that refuses: subprocesses / exec / fork, signals, sockets, ctypes,
     reading outside the interpreter + the project copy, writing outside the project copy.
The parent adds a wall-clock timeout, a scrubbed environment and (when available) a network namespace.
"""
import os
import sys
import traceback

ROOT = os.path.realpath(sys.argv[1])
MODE, MEM_MB, CPU_S, REST = sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5:]

BLOCKED_EVENTS = {
    "os.system", "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "subprocess.Popen",
    "os.kill", "os.killpg", "signal.pthread_kill",
    "socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata",
}
WRITE_PATH_EVENTS = {"os.remove": (0,), "os.rename": (0, 1), "os.rmdir": (0,), "os.mkdir": (0,), "os.chmod": (0,),
                     "os.chown": (0,), "os.truncate": (0,), "os.symlink": (1,), "os.link": (0, 1), "os.utime": (0,)}
BLOCKED_IMPORTS = {"ctypes", "_ctypes"}
ALWAYS_OK = {"/dev/null", "/dev/zero", "/dev/urandom", "/dev/random"}
READ_EXTRA = ["/usr/lib", "/usr/local/lib", "/lib", "/lib64", "/usr/share/zoneinfo", "/etc/localtime",
              "/proc/self", "/proc/cpuinfo", "/proc/meminfo", "/sys/devices/system/cpu"]


def apply_limits():
    try:
        import resource
    except ImportError:
        return
    for name, soft, hard in (("RLIMIT_AS", MEM_MB << 20, MEM_MB << 20), ("RLIMIT_CPU", CPU_S, CPU_S + 2),
                             ("RLIMIT_FSIZE", 5 << 20, 5 << 20), ("RLIMIT_NOFILE", 256, 256), ("RLIMIT_CORE", 0, 0)):
        try:
            resource.setrlimit(getattr(resource, name), (soft, hard))
        except (ValueError, OSError, AttributeError):
            pass


def install_audit_hook():
    real = lambda p: os.path.realpath(os.fsdecode(p))
    inside = lambda path, bases: any(path == b or path.startswith(b.rstrip(os.sep) + os.sep) for b in bases)
    read_ok = {ROOT}
    for p in list(sys.path) + [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix] + READ_EXTRA:
        if p and os.path.exists(p):
            read_ok.add(real(p))
    read_ok = list(read_ok)
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def deny(what, path):
        raise PermissionError(f"sandbox: {what} '{path}' is not allowed")

    def hook(event, args):
        if event in BLOCKED_EVENTS:
            raise PermissionError(f"sandbox: '{event}' is not allowed")
        if event == "import":
            if str(args[0]).split(".")[0] in BLOCKED_IMPORTS:
                raise ImportError("sandbox: importing ctypes is not allowed")
        elif event == "open":
            path, mode, flags = args[0], args[1], args[2]
            if isinstance(path, int):
                return
            p = real(path)
            if p in ALWAYS_OK:
                return
            writing = bool(set(str(mode or "")) & set("wax+")) or bool(int(flags or 0) & write_flags)
            if writing and not inside(p, [ROOT]):
                deny("writing", p)
            if not writing and not inside(p, read_ok):
                deny("reading", p)
        elif event in WRITE_PATH_EVENTS:
            for i in WRITE_PATH_EVENTS[event]:
                if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)) and not inside(real(args[i]), [ROOT]):
                    deny("modifying", real(args[i]))
        elif event in ("os.listdir", "os.scandir"):
            if args and isinstance(args[0], (str, bytes, os.PathLike)) and not inside(real(args[0]), read_ok):
                deny("listing", real(args[0]))
        elif event == "sqlite3.connect":
            if args and str(args[0]) not in (":memory:", "") and not inside(real(args[0]), [ROOT]):
                deny("opening database", real(args[0]))

    sys.addaudithook(hook)


def main():
    apply_limits()
    if MODE == "pytest":
        import pytest
    install_audit_hook()
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    try:
        if MODE == "script":
            import runpy
            sys.argv = [REST[0]] + REST[1:]
            runpy.run_path(os.path.join(ROOT, REST[0]), run_name="__main__")
        else:
            sys.exit(int(pytest.main(["-q", "-x", "--tb=short", "-p", "no:cacheprovider",
                                      "--rootdir", ROOT, *REST])))
    except SystemExit as e:
        sys.stdout.flush()
        code = e.code
        sys.exit(code if isinstance(code, int) else (0 if code is None else 1))
    except BaseException as e:
        frames = [f for f in traceback.extract_tb(e.__traceback__)
                  if not f.filename.endswith(("sandbox_runner.py", "runpy.py"))]
        print("Traceback (most recent call last):", file=sys.stderr)
        traceback.print_list(frames, file=sys.stderr)
        print("".join(traceback.format_exception_only(type(e), e)).rstrip(), file=sys.stderr)
        sys.exit(1)


main()
