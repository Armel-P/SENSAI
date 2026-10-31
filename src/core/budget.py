DEFAULTS = {"num_ctx": 8192, "token_budget": 6000, "keep_recent": 4}
LIMITS = {"num_ctx": (1024, 262144), "token_budget": (0, 262144), "keep_recent": (2, 20)}
REPLY_RESERVE = 512


def estimate_tokens(text: str) -> int:
    """Rough, deliberately pessimistic estimate (~3 chars/token). Ollama's real counts
    are used whenever available; this only covers text that has not been sent yet."""
    return len(text) // 3 + 4


def resolve_settings(cfg: dict, overrides: dict) -> dict:
    """defaults < config.yaml < values saved from the UI/CLI."""
    s = {k: int(cfg.get(k, v)) for k, v in DEFAULTS.items()}
    s.update({k: int(v) for k, v in overrides.items() if k in DEFAULTS})
    return s


def effective_budget(s: dict) -> int:
    """Token cap that actually triggers compression (0 = feature off)."""
    if s["token_budget"] <= 0:
        return 0
    return max(1, min(s["token_budget"], s["num_ctx"] - REPLY_RESERVE))


def validate_settings(patch: dict) -> dict:
    clean = {}
    for key, value in patch.items():
        if key not in LIMITS:
            raise ValueError(f"Unknown setting: {key}")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        lo, hi = LIMITS[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        if key == "token_budget" and 0 < value < 256:
            raise ValueError("token_budget must be 0 (off) or at least 256")
        clean[key] = value
    return clean
