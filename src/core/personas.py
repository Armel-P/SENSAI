"""A5: agent personas. One YAML file per persona in `personas/` (no code change to add one)."""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ID_RE = re.compile(r"^[a-z0-9_-]+$")


class PersonaError(Exception):
    pass


@dataclass
class Persona:
    id: str
    name: str
    system_prompt: str
    role: str = ""
    model: Optional[str] = None
    tools: Optional[list] = None
    files: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "model": self.model, "tools": self.tools}


def _load(path: Path) -> Persona:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PersonaError(f"{path.name}: expected a YAML mapping")
    prompt = data.get("system_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PersonaError(f"{path.name}: 'system_prompt' is required")
    tools = data.get("tools")
    if tools is not None and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
        raise PersonaError(f"{path.name}: 'tools' must be a list of tool names")
    files = data.get("files") or {}
    if not isinstance(files, dict) or any(not isinstance(v, list) for v in files.values()):
        raise PersonaError(f"{path.name}: 'files' must map read/write to lists of globs")
    return Persona(id=path.stem, name=str(data.get("name") or path.stem.title()), system_prompt=prompt.strip(),
                   role=str(data.get("role") or ""), model=data.get("model") or None, tools=tools, files=files)


class PersonaStore:
    """Reads the YAML files on every call: edit a persona and it applies immediately."""

    def __init__(self, directory: str = "personas"):
        self.dir = Path(directory)

    def get(self, persona_id: Optional[str]) -> Optional[Persona]:
        if not persona_id or not ID_RE.match(persona_id):
            return None
        path = self.dir / f"{persona_id}.yaml"
        if not path.is_file():
            return None
        try:
            return _load(path)
        except (PersonaError, yaml.YAMLError):
            return None

    def list(self) -> list[Persona]:
        found = []
        for path in sorted(self.dir.glob("*.yaml")) if self.dir.is_dir() else []:
            if ID_RE.match(path.stem):
                persona = self.get(path.stem)
                if persona:
                    found.append(persona)
        return found
