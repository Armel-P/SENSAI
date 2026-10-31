"""SQLite storage. Messages form a tree (parent_id) so a conversation can branch.

A conversation always displays ONE path: root -> active_leaf_id.
Editing a past message = new sibling + move the active leaf. Nothing is copied or deleted.
"""
import sqlite3
import threading
from datetime import datetime, timezone
import json
from typing import Optional

from .budget import estimate_tokens

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id             INTEGER PRIMARY KEY,
    title          TEXT NOT NULL,
    persona        TEXT,
    model          TEXT,
    active_leaf_id INTEGER,
    share_token    TEXT UNIQUE,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT NOT NULL,
    thinking        TEXT,
    prompt_tokens   INTEGER,
    gen_tokens      INTEGER,
    duration_s      REAL,
    interrupted     INTEGER NOT NULL DEFAULT 0,
    tool_trace      TEXT,              -- JSON: tool calls made while producing this reply
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv   ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS pending_turns (     -- turns suspended for A3 / X4 (survive a restart)
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_message_id INTEGER NOT NULL,
    state           TEXT NOT NULL,
    interaction     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (        -- M2: condensed older messages
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    upto_message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    source_tokens   INTEGER,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompts (          -- saved prompt library
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (            -- reserved for M3
    id                INTEGER PRIMARY KEY,
    key               TEXT NOT NULL,
    value             TEXT NOT NULL,
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    updated_at        TEXT NOT NULL
);
"""

DEFAULT_PROMPTS = [
    ("Get unstuck", "Help me break down a big task into small, manageable steps."),
    ("Brainstorm ideas", "Help me brainstorm a few creative ideas for a project."),
    ("Learn something", "Explain a tricky concept clearly, with a practical example."),
    ("Build something", "Help me write or improve a piece of code. Ask me what I\u2019m building first."),
]


def _msg(row) -> dict:
    """Message row -> dict, with tool_trace decoded from JSON."""
    d = dict(row)
    if d.get("tool_trace"):
        d["tool_trace"] = json.loads(d["tool_trace"])
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Storage:
    def __init__(self, path: str = "sensai.db"):
        self._path = path
        self._local = threading.local()
        self.db

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='prompts'").fetchone() is None
            conn.executescript(SCHEMA)
            if "tool_trace" not in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}:
                conn.execute("ALTER TABLE messages ADD COLUMN tool_trace TEXT")
                conn.commit()
            if fresh:
                conn.executemany(
                    "INSERT INTO prompts(title, content, created_at) VALUES (?,?,?)",
                    [(t, c, _now()) for t, c in DEFAULT_PROMPTS])
                conn.commit()
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def create_conversation(self, title="New conversation",
                            persona: Optional[str] = None,
                            model: Optional[str] = None) -> int:
        now = _now()
        cur = self.db.execute(
            "INSERT INTO conversations(title, persona, model, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (title, persona, model, now, now))
        self.db.commit()
        return cur.lastrowid

    def get_conversation(self, conv_id: int) -> Optional[dict]:
        row = self.db.execute(
            "SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        return dict(row) if row else None

    def list_conversations(self, include_empty: bool = False) -> list[dict]:
        """Most recently active first. Conversations without messages are hidden by default."""
        where = "" if include_empty else "WHERE active_leaf_id IS NOT NULL"
        rows = self.db.execute(
            f"SELECT * FROM conversations {where} ORDER BY updated_at DESC, id DESC").fetchall()
        return [dict(r) for r in rows]

    def update_conversation(self, conv_id: int, **fields) -> None:
        allowed = {"title", "persona", "model"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update fields: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE conversations SET {sets}, updated_at=? WHERE id=?",
                        (*fields.values(), _now(), conv_id))
        self.db.commit()

    def delete_conversation(self, conv_id: int) -> None:
        self.db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        self.db.commit()

    def get_message(self, msg_id: int) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        return _msg(row) if row else None

    def add_message(self, conv_id: int, role: str, content: str, *,
                    parent_id: Optional[int] = ..., thinking: Optional[str] = None,
                    prompt_tokens: Optional[int] = None, gen_tokens: Optional[int] = None,
                    duration_s: Optional[float] = None, interrupted: bool = False,
                    tool_trace: Optional[list] = None) -> int:
        """Add a message and make it the active leaf.
        parent_id defaults to the current active leaf (normal chat flow)."""
        conv = self.get_conversation(conv_id)
        if conv is None:
            raise KeyError(f"Conversation {conv_id} not found")
        if parent_id is ...:
            parent_id = conv["active_leaf_id"]
        if parent_id is not None:
            parent = self.get_message(parent_id)
            if parent is None or parent["conversation_id"] != conv_id:
                raise ValueError("parent_id does not belong to this conversation")
        now = _now()
        cur = self.db.execute(
            "INSERT INTO messages(conversation_id, parent_id, role, content, thinking, "
            "prompt_tokens, gen_tokens, duration_s, interrupted, tool_trace, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, parent_id, role, content, thinking, prompt_tokens, gen_tokens,
             duration_s, int(interrupted), json.dumps(tool_trace) if tool_trace else None, now))
        msg_id = cur.lastrowid
        self.db.execute("UPDATE conversations SET active_leaf_id=?, updated_at=? WHERE id=?",
                        (msg_id, now, conv_id))
        self.db.commit()
        return msg_id

    def get_path(self, conv_id: int, leaf_id: Optional[int] = None) -> list[dict]:
        """Messages from root to leaf (default: the active leaf), oldest first."""
        if leaf_id is None:
            conv = self.get_conversation(conv_id)
            leaf_id = conv["active_leaf_id"] if conv else None
        if leaf_id is None:
            return []
        rows = self.db.execute("""
            WITH RECURSIVE path(id, depth) AS (
                SELECT id, 0 FROM messages WHERE id = ? AND conversation_id = ?
                UNION ALL
                SELECT m.id, p.depth + 1
                FROM messages m
                JOIN path p ON m.id = (SELECT parent_id FROM messages WHERE id = p.id)
            )
            SELECT m.* FROM path p JOIN messages m ON m.id = p.id
            ORDER BY p.depth DESC
        """, (leaf_id, conv_id)).fetchall()
        return [_msg(r) for r in rows]

    def build_context(self, conv_id: int, system_prompt: str) -> list[dict]:
        """What is sent to Ollama: system prompt (+ summary of condensed turns) + the
        messages after the summary's cut-off, along the active path."""
        path = self.get_path(conv_id)
        summ = self.active_summary(conv_id, path)
        if summ:
            system_prompt += ("\n\nSummary of the earlier conversation (older messages were "
                              "condensed to save context):\n" + summ["content"])
            cut = next(i for i, m in enumerate(path) if m["id"] == summ["upto_message_id"])
            path = path[cut + 1:]
        out = [{"role": "system", "content": system_prompt}]
        for m in path:
            if m["role"] == "system":
                continue
            for step in m.get("tool_trace") or []:
                calls = step.get("calls", [])
                out.append({"role": "assistant", "content": step.get("content") or "",
                            "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                           for c in calls]})
                out += [{"role": "tool", "tool_name": c["name"],
                         "content": c["result"] if c.get("result") is not None else "(no result: interrupted)"}
                        for c in calls]
            if m["content"] or not m.get("tool_trace"):
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def save_pending(self, conv_id: int, user_message_id: int, state: dict, interaction: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO pending_turns(conversation_id, user_message_id, state, interaction, created_at) "
            "VALUES (?,?,?,?,?)", (conv_id, user_message_id, json.dumps(state), json.dumps(interaction), _now()))
        self.db.commit()
        return cur.lastrowid

    @staticmethod
    def _pending(row) -> Optional[dict]:
        if row is None:
            return None
        return {"id": row["id"], "conversation_id": row["conversation_id"],
                "state": json.loads(row["state"]), "interaction": json.loads(row["interaction"])}

    def get_pending(self, pending_id: int) -> Optional[dict]:
        return self._pending(self.db.execute("SELECT * FROM pending_turns WHERE id=?", (pending_id,)).fetchone())

    def take_pending(self, pending_id: int) -> Optional[dict]:
        """Read AND delete (a suspended turn is resumed at most once)."""
        pending = self.get_pending(pending_id)
        if pending:
            self.db.execute("DELETE FROM pending_turns WHERE id=?", (pending_id,))
            self.db.commit()
        return pending

    def pending_for(self, conv_id: int) -> list[dict]:
        return [self._pending(r) for r in self.db.execute(
            "SELECT * FROM pending_turns WHERE conversation_id=? ORDER BY id", (conv_id,))]

    def add_summary(self, conv_id: int, upto_message_id: int, content: str,
                    source_tokens: int) -> int:
        cur = self.db.execute(
            "INSERT INTO summaries(conversation_id, upto_message_id, content, source_tokens, "
            "created_at) VALUES (?,?,?,?,?)",
            (conv_id, upto_message_id, content, source_tokens, _now()))
        self.db.commit()
        return cur.lastrowid

    def active_summary(self, conv_id: int, path: Optional[list] = None) -> Optional[dict]:
        """The summary covering the deepest point of the current branch (None if none).
        Summaries hang on a message id, so other branches never see them."""
        path = self.get_path(conv_id) if path is None else path
        pos = {m["id"]: i for i, m in enumerate(path)}
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM summaries WHERE conversation_id=?", (conv_id,))]
        rows = [r for r in rows if r["upto_message_id"] in pos]
        return max(rows, key=lambda r: pos[r["upto_message_id"]], default=None)

    def summary_info(self, conv_id: int) -> Optional[dict]:
        path = self.get_path(conv_id)
        summ = self.active_summary(conv_id, path)
        if not summ:
            return None
        cut = next(i for i, m in enumerate(path) if m["id"] == summ["upto_message_id"])
        return {"messages_condensed": cut + 1, "summary_tokens": estimate_tokens(summ["content"]),
                "source_tokens": summ["source_tokens"]}

    def list_messages(self, conv_id: int) -> list[dict]:
        """Every message of the conversation (all branches), oldest first."""
        return [_msg(r) for r in self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))]

    def list_summaries(self, conv_id: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM summaries WHERE conversation_id=? ORDER BY id", (conv_id,))]

    def all_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT * FROM settings")}

    def set_setting(self, key: str, value) -> None:
        self.db.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        self.db.commit()

    def discard_message(self, msg_id: int, restore_leaf_id: Optional[int]) -> None:
        """Remove a leaf message (used to roll back a turn that failed) and
        restore the previously active leaf."""
        msg = self.get_message(msg_id)
        if msg is None:
            return
        if self.db.execute("SELECT 1 FROM messages WHERE parent_id=?", (msg_id,)).fetchone():
            raise ValueError("Cannot discard a message that has children")
        self.db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
        self.db.execute("UPDATE conversations SET active_leaf_id=?, updated_at=? WHERE id=?",
                        (restore_leaf_id, _now(), msg["conversation_id"]))
        self.db.commit()

    def fork_conversation(self, conv_id: int, from_message_id: Optional[int] = None,
                          title: Optional[str] = None) -> int:
        """Copy a conversation's displayed path (up to from_message_id if given) into a
        NEW independent conversation. The original is never touched."""
        conv = self.get_conversation(conv_id)
        if conv is None:
            raise KeyError(f"Conversation {conv_id} not found")
        if from_message_id is None:
            path = self.get_path(conv_id)
        else:
            msg = self.get_message(from_message_id)
            if msg is None or msg["conversation_id"] != conv_id:
                raise ValueError("from_message_id does not belong to this conversation")
            path = self.get_path(conv_id, leaf_id=from_message_id)
        if not path:
            raise ValueError("Nothing to fork: the conversation is empty")
        new_id = self.create_conversation(title or f"{conv['title']} (fork)"[:100],
                                          conv["persona"], conv["model"])
        parent = None
        for m in path:
            parent = self.add_message(
                new_id, m["role"], m["content"], parent_id=parent, thinking=m["thinking"],
                prompt_tokens=m["prompt_tokens"], gen_tokens=m["gen_tokens"],
                duration_s=m["duration_s"], interrupted=bool(m["interrupted"]),
                tool_trace=m.get("tool_trace"))
        return new_id

    def get_siblings(self, msg_id: int) -> list[dict]:
        """All messages sharing this message's parent (including itself), oldest first."""
        msg = self.get_message(msg_id)
        if msg is None:
            return []
        rows = self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND parent_id IS ? ORDER BY id",
            (msg["conversation_id"], msg["parent_id"])).fetchall()
        return [_msg(r) for r in rows]

    def branch_info(self, msg_id: int) -> tuple[int, int]:
        """(position, total) among siblings, 1-based -- e.g. '2/3' in the UI."""
        ids = [m["id"] for m in self.get_siblings(msg_id)]
        return ids.index(msg_id) + 1, len(ids)

    def edit_message(self, msg_id: int, new_content: str) -> int:
        """Fork: create a sibling with new content, make it active. Original is kept."""
        orig = self.get_message(msg_id)
        if orig is None:
            raise KeyError(f"Message {msg_id} not found")
        if orig["role"] != "user":
            raise ValueError("Only user messages can be edited")
        return self.add_message(orig["conversation_id"], "user", new_content,
                                parent_id=orig["parent_id"])

    def _latest_leaf(self, msg_id: int) -> int:
        """Follow the most recent child repeatedly until a leaf is reached."""
        current = msg_id
        while True:
            row = self.db.execute(
                "SELECT id FROM messages WHERE parent_id=? ORDER BY id DESC LIMIT 1",
                (current,)).fetchone()
            if row is None:
                return current
            current = row["id"]

    def switch_branch(self, msg_id: int) -> None:
        """Display the branch containing msg_id (jumps to its latest descendant)."""
        msg = self.get_message(msg_id)
        if msg is None:
            raise KeyError(f"Message {msg_id} not found")
        self.db.execute("UPDATE conversations SET active_leaf_id=?, updated_at=? WHERE id=?",
                        (self._latest_leaf(msg_id), _now(), msg["conversation_id"]))
        self.db.commit()

    def list_prompts(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM prompts ORDER BY id")]

    def create_prompt(self, title: str, content: str) -> dict:
        cur = self.db.execute("INSERT INTO prompts(title, content, created_at) VALUES (?,?,?)",
                              (title, content, _now()))
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM prompts WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_prompt(self, prompt_id: int, title: str, content: str) -> Optional[dict]:
        cur = self.db.execute("UPDATE prompts SET title=?, content=? WHERE id=?",
                              (title, content, prompt_id))
        self.db.commit()
        if cur.rowcount == 0:
            return None
        return dict(self.db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone())

    def delete_prompt(self, prompt_id: int) -> bool:
        cur = self.db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        self.db.commit()
        return cur.rowcount > 0

    def token_usage(self, conv_id: int) -> dict:
        """Totals over the ACTIVE path (what the model actually sees)."""
        path = self.get_path(conv_id)
        gen = sum(m["gen_tokens"] or 0 for m in path)
        last_prompt = next((m["prompt_tokens"] for m in reversed(path)
                            if m["prompt_tokens"]), 0)
        return {"last_prompt_tokens": last_prompt, "generated_total": gen,
                "messages": len(path)}
