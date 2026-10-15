from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Union

from .ollama_client import Done, OllamaError, Thinking, Token, stream_chat
from .profile import load_profile, profile_to_prompt
from .storage import Storage

DEFAULT_TITLE = "New conversation"

@dataclass
class Started:
    user_message_id: int

@dataclass
class Finished:
    assistant_message_id: int
    prompt_tokens: int
    gen_tokens: int
    duration_s: float

Event = Union[Started, Thinking, Token, Finished]


class ChatService:
    def __init__(self, storage: Storage, cfg: dict, stream_fn: Callable = stream_chat):
        self.storage = storage
        self.cfg = cfg
        self.stream_fn = stream_fn

    def system_prompt(self) -> str:
        extra = profile_to_prompt(load_profile(self.cfg["profile_path"]))
        base = self.cfg["system_prompt"]
        return f"{base}\n\n{extra}" if extra else base

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

        text: list[str] = []
        thought: list[str] = []
        stats: Optional[Done] = None
        try:
            yield Started(user_id)
            context = self.storage.build_context(conv_id, self.system_prompt())
            for ev in self.stream_fn(self.cfg["ollama_url"], model, context,
                                     self.cfg["num_ctx"], think=think):
                if isinstance(ev, Token):
                    text.append(ev.text)
                    yield ev
                elif isinstance(ev, Thinking):
                    thought.append(ev.text)
                    yield ev
                elif isinstance(ev, Done):
                    stats = ev
        except (GeneratorExit, OllamaError):
            # Interrupt or failure: keep what we got, or roll the turn back if nothing.
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