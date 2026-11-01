"""T4: file access inside a workspace folder, with a permission layer that is checked
BEFORE every filesystem operation (read / write / delete are separate permissions)."""
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .tools import Tool

DEFAULT_DENY = [".git", ".git/**", ".env", "**/.env", "**/*.pem", "**/id_rsa*", "**/node_modules/**", "**/__pycache__/**"]


class PermissionDenied(Exception):
    pass


def _glob_regex(pattern: str) -> "re.Pattern":
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3; continue
        if pattern.startswith("**", i):
            out += ".*"; i += 2; continue
        c = pattern[i]
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(f"^{out}$")


def matches(patterns, rel: str, is_dir: bool = False) -> bool:
    candidate = rel + "/" if is_dir and rel else rel
    return any(_glob_regex(p).match(candidate) or _glob_regex(p).match(rel) for p in patterns)


@dataclass
class FilePolicy:
    """Which paths (globs relative to the workspace root) may be read / written. Deny always wins."""
    read: list = field(default_factory=lambda: ["**"])
    write: list = field(default_factory=lambda: ["**"])
    deny: list = field(default_factory=lambda: list(DEFAULT_DENY))

    @classmethod
    def from_persona(cls, files: dict) -> "FilePolicy":
        return cls(read=files.get("read", ["**"]), write=files.get("write", []),
                   deny=list(DEFAULT_DENY) + list(files.get("deny", [])))

    def check(self, action: str, rel: str, is_dir: bool = False) -> None:
        allowed = self.read if action == "read" else self.write
        if matches(self.deny, rel, is_dir) or not matches(allowed, rel, is_dir):
            hint = f" Allowed to {action}: {', '.join(allowed) or 'nothing'}."
            raise PermissionDenied(f"{action} on '{rel or '.'}' is not permitted.{hint}")


class Workspace:
    def __init__(self, root: str, max_bytes: int = 200_000):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    def resolve(self, rel) -> tuple[Path, str]:
        """Map a model-supplied path to a real path INSIDE the workspace (or refuse)."""
        text = str(rel if rel not in (None, "") else ".").strip().replace("\\", "/")
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
            raise PermissionDenied("absolute paths are not allowed: use a path relative to the workspace")
        if ".." in PurePosixPath(text).parts:
            raise PermissionDenied("'..' is not allowed in paths")
        target = (self.root / text).resolve()
        if target != self.root and self.root not in target.parents:
            raise PermissionDenied("this path leads outside the workspace")
        norm = target.relative_to(self.root).as_posix()
        return target, ("" if norm == "." else norm)


def file_tools(ws: Workspace, policy: FilePolicy, confirm_writes: bool = False) -> list:
    """The file tools one agent gets. Every handler checks the policy first."""

    def list_dir(args):
        target, rel = ws.resolve(args.get("path", "."))
        if rel:
            policy.check("read", rel, is_dir=True)
        if not target.is_dir():
            raise FileNotFoundError(f"'{rel or '.'}' is not a directory")
        entries = []
        for child in sorted(target.iterdir()):
            child_rel = child.relative_to(ws.root).as_posix()
            try:
                policy.check("read", child_rel, child.is_dir())
            except PermissionDenied:
                continue
            entries.append(child_rel + ("/" if child.is_dir() else ""))
        return "\n".join(entries[:200]) or "(empty)"

    def read_file(args):
        target, rel = ws.resolve(args.get("path"))
        policy.check("read", rel)
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {rel}")
        if target.stat().st_size > ws.max_bytes:
            raise ValueError(f"file too large (> {ws.max_bytes} bytes)")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError("not a text file")
        offset = max(0, int(args.get("offset") or 0))
        chunk = text[offset:offset + 3500]
        more = offset + len(chunk)
        return chunk + (f"\n[truncated: call read_file again with offset={more}]" if more < len(text) else "")

    def write_file(args):
        target, rel = ws.resolve(args.get("path"))
        content = str(args.get("content", ""))
        if not rel:
            raise PermissionDenied("a file path is required")
        policy.check("write", rel)
        if len(content.encode("utf-8")) > ws.max_bytes:
            raise ValueError(f"content too large (> {ws.max_bytes} bytes)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {rel}."

    def delete_file(args):
        target, rel = ws.resolve(args.get("path"))
        policy.check("write", rel)
        if not target.is_file():
            raise FileNotFoundError(f"no such file: {rel}")
        target.unlink()
        return f"Deleted {rel}."

    path = {"type": "string", "description": "path relative to the workspace root, e.g. backend/app.py"}
    return [
        Tool("list_dir", "List the files and folders of a workspace directory.",
             {"type": "object", "properties": {"path": path}}, list_dir),
        Tool("read_file", "Read a text file from the workspace.",
             {"type": "object", "properties": {"path": path, "offset": {"type": "integer"}}, "required": ["path"]}, read_file),
        Tool("write_file", "Create or overwrite a text file in the workspace (parent folders are created). "
             "Always write the COMPLETE file content.",
             {"type": "object", "properties": {"path": path, "content": {"type": "string"}},
              "required": ["path", "content"]}, write_file, confirm=confirm_writes),
        Tool("delete_file", "Delete a file from the workspace. The user must approve it first.",
             {"type": "object", "properties": {"path": path}, "required": ["path"]}, delete_file, confirm=True),
    ]
