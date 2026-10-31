from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Union

from .budget import (effective_budget, estimate_tokens, resolve_settings,
                     validate_settings)
from .ollama_client import Done, OllamaError, Thinking, Token, stream_chat
from .profile import load_profile, profile_to_prompt
from .storage import Storage

DEFAULT_TITLE = "New conversation"

SUMMARY_SYSTEM = (
    "You condense conversations. Merge the previous summary (if any) with the new excerpt into "
    "ONE compact summary of at most {words} words. Keep: facts about the user, names, numbers, "
    "decisions, constraints, code identifiers, file names and open questions. Drop pleasantries "
    "and repetition. Reply with the summary only, as plain text.")


@dataclass
class Started:
    user_message_id: int

@dataclass
class Compressed:
    messages: int
    source_tokens: int
    summary_tokens: int

@dataclass
class Finished:
    assistant_message_id: int
    prompt_tokens: int
    gen_tokens: int
    duration_s: float

Event = Union[Started, Compressed, Thinking, Token, Finished]


class ChatService:
    def __init__(self, storage: Storage, cfg: dict, stream_fn: Callable = stream_chat):
        self.storage = storage
        self.cfg = cfg
        self.stream_fn = stream_fn

    def settings(self) -> dict:
        s = resolve_settings(self.cfg, self.storage.all_settings())
        s["effective_budget"] = effective_budget(s)
        return s

    def update_settings(self, patch: dict) -> dict:
        for key, value in validate_settings(patch).items():
            self.storage.set_setting(key, value)
        return self.settings()

    def system_prompt(self) -> str:
        extra = profile_to_prompt(load_profile(self.cfg["profile_path"]))
        base = self.cfg["system_prompt"]
        return f"{base}\n\n{extra}" if extra else base

    def _maybe_compress(self, conv_id: int, model: str, st: dict) -> Optional[Compressed]:
        cap = st["effective_budget"]
        if not cap:
            return None
        path = self.storage.get_path(conv_id)
        summ = self.storage.active_summary(conv_id, path)
        start = 0
        if summ:
            start = next(i for i, m in enumerate(path) if m["id"] == summ["upto_message_id"]) + 1
        pending = path[start:]
        prev = summ["content"] if summ else ""

        tokens = (estimate_tokens(self.system_prompt() + prev)
                  + sum(estimate_tokens(m["content"]) for m in pending))

        last = next((m for m in reversed(path) if m["prompt_tokens"]), None)
        if last and (not summ or last["id"] > summ["upto_message_id"]):
            after = path[path.index(last) + 1:]
            tokens = max(tokens, last["prompt_tokens"] + (last["gen_tokens"] or 0)
                         + sum(estimate_tokens(m["content"]) for m in after))
        if tokens <= cap:
            return None

        old = pending[:-st["keep_recent"]] if len(pending) > st["keep_recent"] else []
        while old and old[-1]["role"] != "assistant":     # cut on a complete exchange
            old.pop()
        if not old:
            return None
        try:
            text = self._summarize(model, prev, old, st, cap)
        except OllamaError:
            return None
        if not text:
            return None
        source = sum(estimate_tokens(m["content"]) for m in old) + estimate_tokens(prev)
        self.storage.add_summary(conv_id, old[-1]["id"], text, source)
        return Compressed(len(old), source, estimate_tokens(text))

    def _summarize(self, model: str, previous: str, msgs: list, st: dict, cap: int) -> str:
        system = SUMMARY_SYSTEM.format(words=max(80, cap // 8))
        limit = max(500, st["num_ctx"] // 2)              # excerpt size per call (rolling fold)

        def run(chunk, summary):
            transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in chunk)
            user = (f"Previous summary:\n{summary}\n\n" if summary else "") + f"New excerpt:\n{transcript}"
            events = self.stream_fn(self.cfg["ollama_url"], model,
                                    [{"role": "system", "content": system},
                                     {"role": "user", "content": user}],
                                    st["num_ctx"], think=False)
            return "".join(e.text for e in events if isinstance(e, Token)).strip()

        summary, chunk, size = previous, [], 0
        for m in msgs:
            t = estimate_tokens(m["content"])
            if chunk and size + t > limit:
                summary, chunk, size = run(chunk, summary), [], 0
                if not summary:
                    return ""
            chunk.append(m)
            size += t
        return run(chunk, summary) if chunk else summary

    def send(self, conv_id: int, content: str, *, edit_message_id: Optional[int] = None,
             model: Optional[str] = None, think: Optional[bool] = None) -> Iterator[Event]:
        """Generator. Closing it early (user interrupt) saves the partial reply."""
        content = content.strip()
        if not content:
            raise ValueError("Empty message")
        conv = self.storage.get_conversation(conv_id)
        if conv is None:
            raise KeyError(f"Conversation {conv_id} not found")

        prev_leaf = conv["active_leaf_id"]
        if edit_message_id is not None:
            user_id = self.storage.edit_message(edit_message_id, content)
        else:
            user_id = self.storage.add_message(conv_id, "user", content)

        model = model or conv["model"] or self.cfg["model"]
        updates = {}
        if conv["title"] == DEFAULT_TITLE and edit_message_id is None:
            updates["title"] = content[:50]
        if not conv["model"]:
            updates["model"] = model
        if updates:
            self.storage.update_conversation(conv_id, **updates)

        st = self.settings()
        text: list[str] = []
        thought: list[str] = []
        stats: Optional[Done] = None
        try:
            yield Started(user_id)
            compressed = self._maybe_compress(conv_id, model, st)
            if compressed:
                yield compressed
            context = self.storage.build_context(conv_id, self.system_prompt())
            for ev in self.stream_fn(self.cfg["ollama_url"], model, context,
                                     st["num_ctx"], think=think):
                if isinstance(ev, Token):
                    text.append(ev.text)
                    yield ev
                elif isinstance(ev, Thinking):
                    thought.append(ev.text)
                    yield ev
                elif isinstance(ev, Done):
                    stats = ev
        except (GeneratorExit, OllamaError):
            if text:
                self.storage.add_message(conv_id, "assistant", "".join(text),
                                         thinking="".join(thought) or None, interrupted=True)
            else:
                self.storage.discard_message(user_id, prev_leaf)
            raise

        reply_id = self.storage.add_message(
            conv_id, "assistant", "".join(text), thinking="".join(thought) or None,
            prompt_tokens=stats.prompt_tokens if stats else None,
            gen_tokens=stats.gen_tokens if stats else None,
            duration_s=stats.duration_s if stats else None)
        yield Finished(reply_id,
                       stats.prompt_tokens if stats else 0,
                       stats.gen_tokens if stats else 0,
                       stats.duration_s if stats else 0.0)
