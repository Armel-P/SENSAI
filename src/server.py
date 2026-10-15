import argparse
import json
from pathlib import Path
from typing import Callable, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.core.chat import ChatService, Finished, Started
from src.core.config import load_config
from src.core.ollama_client import (OllamaError, Thinking, Token, list_models,
                                    stream_chat)
from src.core.profile import load_profile
from src.core.storage import Storage

_END = object()


class SendBody(BaseModel):
    content: str
    edit_message_id: Optional[int] = None   # set to fork from an earlier user message
    model: Optional[str] = None
    think: Optional[bool] = None

class CreateBody(BaseModel):
    title: str = "New conversation"
    model: Optional[str] = None

class RenameBody(BaseModel):
    title: str = Field(min_length=1, max_length=100)

class ProfileBody(BaseModel):
    name: Optional[str] = None
    preferences: list[str] = []
    instructions: Optional[str] = None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(gen):
    """Turn the (blocking) ChatService generator into an SSE stream.

    If the browser disconnects (Stop button -> AbortController), this async generator
    is closed; `finally` closes the ChatService generator, which saves the partial
    reply as interrupted and drops the Ollama connection.
    """
    try:
        while True:
            ev = await run_in_threadpool(next, gen, _END)
            if ev is _END:
                break
            if isinstance(ev, Started):
                yield sse("started", {"user_message_id": ev.user_message_id})
            elif isinstance(ev, Thinking):
                yield sse("thinking", {"text": ev.text})
            elif isinstance(ev, Token):
                yield sse("token", {"text": ev.text})
            elif isinstance(ev, Finished):
                yield sse("finished", {
                    "assistant_message_id": ev.assistant_message_id,
                    "prompt_tokens": ev.prompt_tokens,
                    "gen_tokens": ev.gen_tokens,
                    "duration_s": ev.duration_s})
    except OllamaError as e:
        yield sse("error", {"kind": type(e).__name__, "message": str(e)})
    finally:
        gen.close()


def create_app(cfg: Optional[dict] = None, storage: Optional[Storage] = None,
               stream_fn: Callable = stream_chat) -> FastAPI:
    cfg = cfg or load_config()
    storage = storage or Storage(cfg["db_path"])
    chat = ChatService(storage, cfg, stream_fn)
    app = FastAPI(title="Sensai")

    def conv_or_404(conv_id: int) -> dict:
        conv = storage.get_conversation(conv_id)
        if conv is None:
            raise HTTPException(404, f"Conversation {conv_id} not found")
        return conv

    def detail(conv_id: int) -> dict:
        conv = conv_or_404(conv_id)
        messages = storage.get_path(conv_id)
        for m in messages:
            m["siblings"] = [s["id"] for s in storage.get_siblings(m["id"])]
        return {"conversation": conv, "messages": messages,
                "usage": {**storage.token_usage(conv_id), "num_ctx": cfg["num_ctx"]}}

    @app.get("/api/config")
    def get_config():
        return {"model": cfg["model"], "num_ctx": cfg["num_ctx"]}

    @app.get("/api/models")
    def get_models():
        try:
            return {"models": list_models(cfg["ollama_url"]), "default": cfg["model"]}
        except OllamaError as e:
            raise HTTPException(503, str(e))

    @app.get("/api/conversations")
    def list_conversations(include_empty: bool = False):
        return storage.list_conversations(include_empty=include_empty)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: CreateBody = CreateBody()):
        cid = storage.create_conversation(body.title, model=body.model or cfg["model"])
        return storage.get_conversation(cid)

    @app.get("/api/conversations/{conv_id}")
    def get_conversation(conv_id: int):
        return detail(conv_id)

    @app.patch("/api/conversations/{conv_id}")
    def rename_conversation(conv_id: int, body: RenameBody):
        conv_or_404(conv_id)
        storage.update_conversation(conv_id, title=body.title)
        return storage.get_conversation(conv_id)

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: int):
        conv_or_404(conv_id)
        storage.delete_conversation(conv_id)
        return {"deleted": conv_id}

    @app.post("/api/conversations/{conv_id}/messages")
    def send_message(conv_id: int, body: SendBody):
        conv_or_404(conv_id)
        if not body.content.strip():
            raise HTTPException(422, "Message is empty")
        if body.edit_message_id is not None:
            target = storage.get_message(body.edit_message_id)
            if target is None or target["conversation_id"] != conv_id:
                raise HTTPException(404, "Message to edit not found in this conversation")
            if target["role"] != "user":
                raise HTTPException(422, "Only user messages can be edited")
        gen = chat.send(conv_id, body.content, edit_message_id=body.edit_message_id,
                        model=body.model, think=body.think)
        return StreamingResponse(_stream(gen), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/messages/{msg_id}/switch")
    def switch_branch(msg_id: int):
        msg = storage.get_message(msg_id)
        if msg is None:
            raise HTTPException(404, f"Message {msg_id} not found")
        storage.switch_branch(msg_id)
        return detail(msg["conversation_id"])

    @app.get("/api/profile")
    def get_profile():
        return load_profile(cfg["profile_path"])

    @app.put("/api/profile")
    def put_profile(body: ProfileBody):
        path = Path(cfg["profile_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        data = body.model_dump(exclude_none=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        return data

    web = Path(__file__).resolve().parent.parent / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")

    return app

def main() -> None:
    ap = argparse.ArgumentParser(description="Sensai web server")
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=args.host or cfg["host"], port=args.port or cfg["port"])


if __name__ == "__main__":
    main()
