import yaml
from pathlib import Path

DEFAULTS = {
    "ollama_url": "http://localhost:11434",
    "model": "qwen3:8b",
    "num_ctx": 8192,
    "system_prompt": "You are a helpful assistant.",
    "db_path": "sensai.db",
    "profile_path": "profiles/user.yaml",
    "host": "127.0.0.1",
    "port": 8000
}

def load_config(path: str = "config.yml") -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        cfg.update(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    return cfg
