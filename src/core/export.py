import re
from datetime import datetime, timezone


def slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:50] or "conversation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def export_json(storage, conv_id: int) -> dict:
    return {
        "format": "sensai-conversation/1",
        "exported_at": _now(),
        "conversation": storage.get_conversation(conv_id),
        "active_path": [m["id"] for m in storage.get_path(conv_id)],   # the branch on screen
        "messages": storage.list_messages(conv_id),                     # all branches (parent_id = tree)
        "summaries": storage.list_summaries(conv_id),                   # M2 condensed blocks
    }


def export_markdown(storage, conv_id: int, user_name: str = "You", include_thinking: bool = False) -> str:
    conv = storage.get_conversation(conv_id)
    lines = [f"# {conv['title']}", "",
             f"*Exported from Sensai on {_now()[:10]} · model: {conv['model'] or 'unknown'}*", ""]
    for m in storage.get_path(conv_id):
        lines += ["---", "", f"### {user_name if m['role'] == 'user' else 'Sensai'}", ""]
        if include_thinking and m["thinking"]:
            lines += ["<details><summary>Thinking</summary>", "", m["thinking"], "", "</details>", ""]
        lines += [m["content"], ""]
        if m["role"] == "assistant":
            bits = []
            if m["gen_tokens"] is not None:
                bits.append(f"{m['gen_tokens']} tokens")
            if m["duration_s"]:
                bits.append(f"{m['duration_s']:.1f}s")
            if m["interrupted"]:
                bits.append("stopped early")
            if bits:
                lines += [f"*({' · '.join(bits)})*", ""]
    return "\n".join(lines).rstrip() + "\n"
