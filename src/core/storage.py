import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

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
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv   ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS facts (            -- reserved for M3
    id                INTEGER PRIMARY KEY,
    key               TEXT NOT NULL,
    value             TEXT NOT NULL,
    source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    updated_at        TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Storage:
    def __init__(self, path: str = "sensai.db"):
        # One SQLite connection per thread (FastAPI runs handlers in a thread pool).
        # NB: with ":memory:" each thread would get its own empty DB -> use a file
        # when several threads are involved.
        self._path = path
        self._local = threading.local()
        self.db  # create the schema right away

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self._path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
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
        return dict(row) if row else None

    def add_message(self, conv_id: int, role: str, content: str, *,
                    parent_id: Optional[int] = ..., thinking: Optional[str] = None,
                    prompt_tokens: Optional[int] = None, gen_tokens: Optional[int] = None,
                    duration_s: Optional[float] = None, interrupted: bool = False) -> int:
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
            "prompt_tokens, gen_tokens, duration_s, interrupted, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (conv_id, parent_id, role, content, thinking, prompt_tokens, gen_tokens,
             duration_s, int(interrupted), now))
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
        return [dict(r) for r in rows]

    def build_context(self, conv_id: int, system_prompt: str) -> list[dict]:
        """The list you send to Ollama: system prompt + active path."""
        msgs = [{"role": "system", "content": system_prompt}]
        msgs += [{"role": m["role"], "content": m["content"]}
                 for m in self.get_path(conv_id) if m["role"] != "system"]
        return msgs

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

    def get_siblings(self, msg_id: int) -> list[dict]:
        """All messages sharing this message's parent (including itself), oldest first."""
        msg = self.get_message(msg_id)
        if msg is None:
            return []
        rows = self.db.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND parent_id IS ? ORDER BY id",
            (msg["conversation_id"], msg["parent_id"])).fetchall()
        return [dict(r) for r in rows]

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

    def token_usage(self, conv_id: int) -> dict:
        """Totals over the ACTIVE path (what the model actually sees)."""
        path = self.get_path(conv_id)
        gen = sum(m["gen_tokens"] or 0 for m in path)
        last_prompt = next((m["prompt_tokens"] for m in reversed(path)
                            if m["prompt_tokens"]), 0)
        return {"last_prompt_tokens": last_prompt, "generated_total": gen,
                "messages": len(path)}
