"""T1: the tools a model may call. A registry of plain Python functions + two special kinds
that the chat loop handles itself: `confirm` (A3, needs approval) and `ask` (X4, answered in the UI)."""
import ast
import operator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

MAX_RESULT_CHARS = 4000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Optional[Callable[[dict], str]] = None
    confirm: bool = False
    ask: bool = False


class ToolRegistry:
    def __init__(self, tools=()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        """The `tools` array of Ollama's /api/chat."""
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}}
                for t in self._tools.values()]


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression (numbers and + - * / // % ** only)")


def calculate(args: dict) -> str:
    expression = str(args.get("expression", "")).strip()
    if not expression or len(expression) > 200:
        raise ValueError("expression is empty or too long")
    return str(_eval(ast.parse(expression, mode="eval").body))   # never uses eval()


def get_current_time(args: dict) -> str:
    return datetime.now().astimezone().strftime("%A %Y-%m-%d %H:%M:%S %Z")


def make_save_note(path: str) -> Callable[[dict], str]:
    def save_note(args: dict) -> str:
        title = str(args.get("title", "")).strip() or "Untitled"
        text = str(args.get("text", "")).strip()
        if not text:
            raise ValueError("text is empty")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(f"## {title}\n\n{text}\n\n")
        return f"Note saved to {target}."
    return save_note


def builtin_registry(notes_path: str = "data/notes.md") -> ToolRegistry:
    return ToolRegistry([
        Tool("get_current_time", "Get the current local date and time.",
             {"type": "object", "properties": {}}, get_current_time),
        Tool("calculate", "Evaluate an arithmetic expression exactly. Use it for any calculation.",
             {"type": "object", "properties": {"expression": {"type": "string", "description": "e.g. (12 + 3) * 4"}},
              "required": ["expression"]}, calculate),
        Tool("ask_user", "Ask the user a question when you need information you cannot infer. "
             "Prefer this to guessing. Give `options` for a multiple-choice question.",
             {"type": "object", "properties": {"question": {"type": "string"},
                                                "options": {"type": "array", "items": {"type": "string"}}},
              "required": ["question"]}, ask=True),
        Tool("save_note", "Save a note to the user's notes file. The user must approve it first.",
             {"type": "object", "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
              "required": ["text"]}, make_save_note(notes_path), confirm=True),
    ])
