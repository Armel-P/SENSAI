from pathlib import Path

import yaml


def load_profile(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def profile_to_prompt(profile: dict) -> str:
    if not profile:
        return ""
    lines = ["Information about the user (always take it into account):"]
    if profile.get("name"):
        lines.append(f"- Name: {profile['name']}")
    for pref in profile.get("preferences") or []:
        lines.append(f"- Preference: {pref}")
    if profile.get("instructions"):
        lines.append("Custom instructions from the user:")
        lines.append(str(profile["instructions"]).strip())
    return "\n".join(lines) if len(lines) > 1 else ""
