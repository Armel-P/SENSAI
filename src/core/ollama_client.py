import json
from dataclasses import dataclass
from typing import Iterator, Optional, Union

import requests


class OllamaError(Exception):
    pass

class OllamaUnavailable(OllamaError):
    """Server not running / unreachable."""

class ModelNotFound(OllamaError):
    """Model not pulled on this machine."""


@dataclass
class Token:
    text: str

@dataclass
class Thinking:
    text: str

@dataclass
class Done:
    prompt_tokens: int
    gen_tokens: int
    duration_s: float


def stream_chat(url: str, model: str, messages: list[dict],
                num_ctx: int = 8192, think: Optional[bool] = None
                ) -> Iterator[Union[Thinking, Token, Done]]:
    payload = {"model": model, "messages": messages, "stream": True,
               "options": {"num_ctx": num_ctx}}
    if think is not None:
        payload["think"] = think
    try:
        r = requests.post(
            f"{url}/api/chat",
            json=payload,
            stream=True,
            timeout=(5, 300),
        )
    except requests.ConnectionError as e:
        raise OllamaUnavailable(
            f"Cannot reach Ollama at {url}. Is it running? (try: ollama serve)") from e
    except requests.Timeout as e:
        raise OllamaError("Ollama took too long to respond.") from e

    if r.status_code == 404:
        raise ModelNotFound(f"Model '{model}' not found. Run: ollama pull {model}")
    if not r.ok:
        raise OllamaError(f"Ollama error {r.status_code}: {r.text[:200]}")

    try:
        for line in r.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise OllamaError(chunk["error"])
            msg = chunk.get("message", {})
            if msg.get("thinking"):
                yield Thinking(msg["thinking"])
            content = msg.get("content", "")
            if content:
                yield Token(content)
            if chunk.get("done"):
                yield Done(
                    prompt_tokens=chunk.get("prompt_eval_count", 0),
                    gen_tokens=chunk.get("eval_count", 0),
                    duration_s=chunk.get("total_duration", 0) / 1e9,
                )
    except requests.RequestException as e:
        raise OllamaUnavailable("Connection lost during generation.") from e
    finally:
        r.close()


def list_models(url: str) -> list[str]:
    """Names of the models pulled on this machine."""
    try:
        r = requests.get(f"{url}/api/tags", timeout=5)
        r.raise_for_status()
    except requests.ConnectionError as e:
        raise OllamaUnavailable(f"Cannot reach Ollama at {url}. Is it running?") from e
    except requests.RequestException as e:
        raise OllamaError(f"Ollama error: {e}") from e
    return [m["name"] for m in r.json().get("models", [])]
