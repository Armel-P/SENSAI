"""ChatService: ONE conversation turn, shared by the CLI and the web server.

It persists everything (user message, assistant reply, tokens, thinking), keeps the DB
consistent when a turn fails or is interrupted, and (M2) condenses old turns into a
summary when the context approaches the token budget.
"""
import json
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Union

from .budget import (effective_budget, estimate_tokens, resolve_settings,
                     validate_settings)
from .personas import PersonaStore
from .ollama_client import Done, OllamaError, Thinking, Token, ToolCall, stream_chat
from .profile import load_profile, profile_to_prompt
from .storage import Storage
from .tools import MAX_RESULT_CHARS, ToolRegistry, builtin_registry

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
class ToolStart:
    name: str
    arguments: dict

@dataclass
class ToolResult:
    name: str
    result: str
    status: str            # done | error | answered | rejected

@dataclass
class Interaction:
    """The turn is SUSPENDED: the user must answer (kind 'ask') or approve (kind 'confirm'),
    then the turn is resumed with ChatService.resume(id, answer)."""
    id: int
    payload: dict

@dataclass
class Finished:
    assistant_message_id: int
    prompt_tokens: int
    gen_tokens: int
    duration_s: float

Event = Union[Started, Compressed, Thinking, Token, ToolStart, ToolResult, Interaction, Finished]


class ChatService:
    def __init__(self, storage: Storage, cfg: dict, stream_fn: Callable = stream_chat,
                 tools: Optional[ToolRegistry] = None):
        self.storage = storage
        self.cfg = cfg
        self.stream_fn = stream_fn
        self.tools = tools or builtin_registry(cfg.get("notes_path", "data/notes.md"))
        self.personas = PersonaStore(cfg.get("personas_dir", "personas"))

    def settings(self) -> dict:
        s = resolve_settings(self.cfg, self.storage.all_settings())
        s["effective_budget"] = effective_budget(s)
        return s

    def update_settings(self, patch: dict) -> dict:
        for key, value in validate_settings(patch).items():
            self.storage.set_setting(key, value)
        return self.settings()

    def _persona(self, conv: Optional[dict]):
        return self.personas.get(conv.get("persona")) if conv else None

    def system_prompt(self, persona=None) -> str:
        # Profile and persona files are re-read on every turn: editing them applies immediately.
        extra = profile_to_prompt(load_profile(self.cfg["profile_path"]))
        base = persona.system_prompt if persona else self.cfg["system_prompt"]
        return f"{base}\n\n{extra}" if extra else base

    @staticmethod
    def _size(m: dict) -> int:
        return estimate_tokens(m["content"]) + (estimate_tokens(json.dumps(m["tool_trace"])) if m.get("tool_trace") else 0)

    @staticmethod
    def _line(m: dict) -> str:
        line = f"{m['role'].upper()}: {m['content']}"
        for step in m.get("tool_trace") or []:
            for c in step.get("calls", []):
                line += f"\n[tool {c['name']}({json.dumps(c['arguments'], ensure_ascii=False)}) -> {str(c.get('result'))[:300]}]"
        return line

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

        persona = self._persona(self.storage.get_conversation(conv_id))
        tokens = estimate_tokens(self.system_prompt(persona) + prev) + sum(self._size(m) for m in pending)
        last = next((m for m in reversed(path) if m["prompt_tokens"]), None)
        if last and (not summ or last["id"] > summ["upto_message_id"]):
            after = path[path.index(last) + 1:]
            tokens = max(tokens, last["prompt_tokens"] + (last["gen_tokens"] or 0)
                         + sum(self._size(m) for m in after))
        if tokens <= cap:
            return None

        old = pending[:-st["keep_recent"]] if len(pending) > st["keep_recent"] else []
        while old and old[-1]["role"] != "assistant":
            old.pop()
        if not old:
            return None
        try:
            text = self._summarize(model, prev, old, st, cap)
        except OllamaError:
            return None
        if not text:
            return None
        source = sum(self._size(m) for m in old) + estimate_tokens(prev)
        self.storage.add_summary(conv_id, old[-1]["id"], text, source)
        return Compressed(len(old), source, estimate_tokens(text))

    def _summarize(self, model: str, previous: str, msgs: list, st: dict, cap: int) -> str:
        system = SUMMARY_SYSTEM.format(words=max(80, cap // 8))
        limit = max(500, st["num_ctx"] // 2)

        def run(chunk, summary):
            transcript = "\n".join(self._line(m) for m in chunk)
            user = (f"Previous summary:\n{summary}\n\n" if summary else "") + f"New excerpt:\n{transcript}"
            events = self.stream_fn(self.cfg["ollama_url"], model,
                                    [{"role": "system", "content": system},
                                     {"role": "user", "content": user}],
                                    st["num_ctx"], think=False)
            return "".join(e.text for e in events if isinstance(e, Token)).strip()

        summary, chunk, size = previous, [], 0
        for m in msgs:
            t = self._size(m)
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
        if self.storage.get_conversation(conv_id) is None:
            raise KeyError(f"Conversation {conv_id} not found")
        for pending in self.storage.pending_for(conv_id):
            self.abort_pending(pending["id"])
        conv = self.storage.get_conversation(conv_id)

        prev_leaf = conv["active_leaf_id"]
        if edit_message_id is not None:
            user_id = self.storage.edit_message(edit_message_id, content)
        else:
            user_id = self.storage.add_message(conv_id, "user", content)

        persona = self._persona(conv)
        model = model or (persona.model if persona else None) or conv["model"] or self.cfg["model"]
        updates = {}
        if conv["title"] == DEFAULT_TITLE and edit_message_id is None:
            updates["title"] = content[:50]
        if not conv["model"]:
            updates["model"] = model
        if updates:
            self.storage.update_conversation(conv_id, **updates)

        try:
            yield Started(user_id)
            compressed = self._maybe_compress(conv_id, model, self.settings())
            if compressed:
                yield compressed
            context = self.storage.build_context(conv_id, self.system_prompt(persona))
        except (GeneratorExit, OllamaError):
            self.storage.discard_message(user_id, prev_leaf)
            raise
        state = {"conv_id": conv_id, "user_id": user_id, "prev_leaf": prev_leaf, "model": model,
                 "think": think, "messages": context, "trace": [], "queue": [], "rounds": 0,
                 "tool_names": persona.tools if persona else None,
                 "prompt_tokens": None, "gen_tokens": None, "duration_s": 0.0}
        yield from self._run(state)

    def resume(self, interaction_id: int, answer: dict) -> Iterator[Event]:
        """Continue a suspended turn with the user's answer ({'value': str} | {'approved': bool}
        | {'cancelled': True})."""
        pending = self.storage.take_pending(interaction_id)
        if pending is None:
            raise KeyError(f"Interaction {interaction_id} not found")
        yield from self._run(pending["state"], answer)

    def abort_pending(self, interaction_id: int) -> bool:
        """Give up on a suspended turn (Stop button): the reply so far is saved as interrupted."""
        pending = self.storage.take_pending(interaction_id)
        if pending is None:
            return False
        self._abort_turn(pending["state"], [], [])
        return True

    def _run(self, st: dict, answer: Optional[dict] = None) -> Iterator[Event]:
        """model -> (tool calls -> results -> model)* -> final reply.
        Suspends (saves `st`, yields Interaction, returns) when a tool needs the user."""
        names = st.get("tool_names")                    # a persona may restrict the tools
        schemas = self.tools.schemas(names) if self.cfg.get("tools", False) else []
        extra = {"tools": schemas} if schemas else {}
        max_rounds = int(self.cfg.get("max_tool_rounds", 6))
        settings = self.settings()
        text: list[str] = []
        thought: list[str] = []
        try:
            while True:
                if not st["queue"]:
                    if st["rounds"] >= max_rounds:
                        reply = self._save_reply(st, f"(Stopped after {max_rounds} tool rounds.)", "")
                        yield self._finished(st, reply)
                        return
                    st["rounds"] += 1
                    text, thought, calls, stats = [], [], [], None
                    for ev in self.stream_fn(self.cfg["ollama_url"], st["model"], st["messages"],
                                             settings["num_ctx"], think=st["think"], **extra):
                        if isinstance(ev, Token):
                            text.append(ev.text)
                            yield ev
                        elif isinstance(ev, Thinking):
                            thought.append(ev.text)
                            yield ev
                        elif isinstance(ev, ToolCall):
                            calls.append(ev)
                        elif isinstance(ev, Done):
                            stats = ev
                    if stats:
                        st["prompt_tokens"], st["gen_tokens"] = stats.prompt_tokens, stats.gen_tokens
                        st["duration_s"] += stats.duration_s
                    if not calls:
                        reply = self._save_reply(st, "".join(text), "".join(thought))
                        yield self._finished(st, reply)
                        return
                    st["messages"].append({"role": "assistant", "content": "".join(text),
                                           "tool_calls": [{"function": {"name": c.name, "arguments": c.arguments}}
                                                          for c in calls]})
                    st["trace"].append({"content": "".join(text), "thinking": "".join(thought) or None,
                                        "calls": [{"name": c.name, "arguments": c.arguments,
                                                   "result": None, "status": "pending"} for c in calls]})
                    st["queue"] = list(range(len(calls)))
                    text, thought = [], []

                step = st["trace"][-1]
                while st["queue"]:
                    call = step["calls"][st["queue"][0]]
                    tool = self.tools.get(call["name"]) if names is None or call["name"] in names else None
                    if call["status"] == "pending":
                        yield ToolStart(call["name"], call["arguments"])
                    if tool and (tool.ask or tool.confirm) and answer is None:
                        call["status"] = "waiting"
                        payload = self._interaction(tool, call["arguments"])
                        yield Interaction(self.storage.save_pending(st["conv_id"], st["user_id"], st, payload),
                                          payload)
                        return                                        # suspended until the user answers
                    result, status = self._execute(tool, call, answer)
                    answer = None
                    call["result"], call["status"] = result, status
                    st["messages"].append({"role": "tool", "tool_name": call["name"], "content": result})
                    st["queue"].pop(0)
                    yield ToolResult(call["name"], result, status)
        except (GeneratorExit, OllamaError):
            self._abort_turn(st, text, thought)
            raise

    @staticmethod
    def _interaction(tool, args: dict) -> dict:
        if tool.ask:
            return {"kind": "ask", "tool": tool.name, "question": str(args.get("question", "")),
                    "options": [str(o) for o in (args.get("options") or [])]}
        return {"kind": "confirm", "tool": tool.name, "arguments": args,
                "question": f"Allow the assistant to run '{tool.name}'?", "description": tool.description}

    @staticmethod
    def _execute(tool, call: dict, answer: Optional[dict]) -> tuple[str, str]:
        if tool is None:
            return f"Error: unknown tool '{call['name']}'.", "error"
        if answer is not None:
            if answer.get("cancelled") or (tool.confirm and not answer.get("approved")):
                return ("The user rejected this action." if tool.confirm else "The user declined to answer."), "rejected"
            if tool.ask:
                return str(answer.get("value", "")), "answered"
        try:
            return str(tool.handler(call["arguments"]))[:MAX_RESULT_CHARS], "done"
        except Exception as e:
            return f"Error: {e}", "error"

    def _save_reply(self, st: dict, content: str, thinking: str, interrupted: bool = False) -> int:
        return self.storage.add_message(
            st["conv_id"], "assistant", content, thinking=thinking or None,
            prompt_tokens=st["prompt_tokens"], gen_tokens=st["gen_tokens"],
            duration_s=st["duration_s"] or None, interrupted=interrupted,
            tool_trace=st["trace"] or None)

    def _abort_turn(self, st: dict, text: list, thought: list) -> None:
        """Interrupt or failure: keep what exists, or roll the turn back if nothing happened."""
        content = "".join(text)
        if content or st["trace"]:
            self._save_reply(st, content, "".join(thought), interrupted=True)
        else:
            self.storage.discard_message(st["user_id"], st["prev_leaf"])

    @staticmethod
    def _finished(st: dict, reply_id: int) -> Finished:
        return Finished(reply_id, st["prompt_tokens"] or 0, st["gen_tokens"] or 0, st["duration_s"])
