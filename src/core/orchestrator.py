"""A2: multi-agent orchestration.

  1. plan():  the PM persona answers with a JSON plan (T5: Ollama `format` schema), which we VALIDATE
              (known agents, dependencies, writable paths) and send back for correction when invalid.
  2. the user reviews / edits the plan and approves it (A3).
  3. run():   specialised personas (A5) execute the tasks in dependency order, each in its own
              conversation, inside the project workspace with its own file permissions (T4).
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import yaml

from .budget import resolve_settings
from .chat import ChatService, Finished, Interaction, ToolResult, ToolStart
from .ollama_client import (OllamaError, OllamaUnavailable, Token, stream_chat)
from .personas import Persona, PersonaStore
from .storage import Storage
from .tools import ToolRegistry
from .workspace import DEFAULT_DENY, FilePolicy, PermissionDenied, Workspace, file_tools, matches

RUNNABLE = ("awaiting_approval", "paused", "failed", "cancelled")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")


class ProjectError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class _Cancelled(Exception):
    pass


@dataclass
class Evt:
    """An orchestrator event, sent to the browser as Server-Sent Event `name` with JSON `data`."""
    name: str
    data: dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "project"


def load_team_ids(path: str) -> list:
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(data.get("active"), list):
            return [str(x) for x in data["active"]]
    return ["pm", "backend", "frontend", "tester", "reviewer"]


def plan_schema(agent_ids: list, max_tasks: int) -> dict:
    task = {"type": "object", "required": ["id", "title", "agent", "description", "depends_on", "files"],
            "properties": {"id": {"type": "string"}, "title": {"type": "string"},
                           "agent": {"type": "string", "enum": agent_ids},
                           "description": {"type": "string"},
                           "depends_on": {"type": "array", "items": {"type": "string"}},
                           "files": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "required": ["summary", "tasks"],
            "properties": {"summary": {"type": "string"},
                           "tasks": {"type": "array", "minItems": 1, "maxItems": max_tasks, "items": task}}}


def parse_json(raw: str):
    text = raw.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


def normalize_plan(plan: dict) -> dict:
    tasks = []
    for t in plan.get("tasks", []) if isinstance(plan, dict) else []:
        if not isinstance(t, dict):
            tasks.append(t)
            continue
        tasks.append({"id": str(t.get("id", "")).strip(), "title": str(t.get("title", "")).strip(),
                      "agent": str(t.get("agent", "")).strip(), "description": str(t.get("description", "")).strip(),
                      "depends_on": [str(d).strip() for d in (t.get("depends_on") or [])],
                      "files": [str(f).strip().replace("\\", "/") for f in (t.get("files") or [])]})
    return {"summary": str(plan.get("summary", "")).strip() if isinstance(plan, dict) else "", "tasks": tasks}


def validate_plan(plan: dict, team: dict, max_tasks: int) -> list:
    """Return a list of human-readable problems (empty = valid). They are fed back to the PM."""
    tasks = plan.get("tasks") if isinstance(plan, dict) else None
    if not isinstance(tasks, list) or not tasks:
        return ["'tasks' must be a non-empty list"]
    errors, seen = [], set()
    if len(tasks) > max_tasks:
        errors.append(f"too many tasks ({len(tasks)}): at most {max_tasks}")
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            errors.append(f"task #{i + 1} is not an object")
            continue
        label = t.get("id") or f"#{i + 1}"
        for field in ("id", "title", "agent", "description"):
            if not t.get(field):
                errors.append(f"task {label}: '{field}' is required")
        if t.get("id") in seen:
            errors.append(f"task {label}: duplicate id")
        agent = t.get("agent")
        if agent == "pm":
            errors.append(f"task {label}: the project manager cannot be assigned tasks")
        elif agent and agent not in team:
            errors.append(f"task {label}: unknown agent '{agent}' (team: {', '.join(a for a in team if a != 'pm')})")
        for dep in t.get("depends_on", []):
            if dep not in seen:
                errors.append(f"task {label}: depends on '{dep}', which is not defined EARLIER in the list")
        for f in t.get("files", []):
            if f.startswith("/") or ".." in f.split("/") or not f:
                errors.append(f"task {label}: invalid file path '{f}'")
            elif agent in team:
                try:
                    FilePolicy.from_persona(team[agent].files).check("write", f)
                except PermissionDenied as e:
                    errors.append(f"task {label}: agent '{agent}' cannot create '{f}'. {e}")
        seen.add(t.get("id"))
    return errors


def list_files(ws: Workspace, limit: int = 60) -> list:
    out = []
    for folder, dirs, files in os.walk(ws.root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            path = Path(folder, name)
            rel = path.relative_to(ws.root).as_posix()
            if name.startswith(".") or path.is_symlink() or matches(DEFAULT_DENY, rel):
                continue
            out.append(rel)
            if len(out) >= limit:
                return out + ["… (more files not shown)"]
    return out


def previews(ws: Workspace, files: list, max_files: int = 6, chars: int = 1200) -> str:
    wanted = [f for f in files if f.endswith((".py", ".js", ".html", ".json", ".yaml", ".md")) and "…" not in f]
    parts = []
    for rel in wanted[:max_files]:
        path = ws.root / rel
        try:
            if path.stat().st_size <= 20_000:
                parts.append(f"--- {rel}\n{path.read_text(encoding='utf-8')[:chars]}")
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


def render_project_md(project: dict, tasks: list) -> str:
    icon = {"done": "✅", "failed": "❌", "skipped": "⏭", "running": "⏳", "pending": "·"}
    lines = [f"# {project['title']}", "", f"**Goal:** {project['goal']}", "",
             f"**Status:** {project['status']} · updated {_now()}",
             f"**Team:** {', '.join(project['team'])}", ""]
    if project.get("summary"):
        lines += [f"**Plan:** {project['summary']}", ""]
    lines += ["## Tasks", "", "| # | Task | Agent | Status | Files |", "|---|---|---|---|---|"]
    for t in tasks:
        shown = t["written"] or t["files"]
        lines.append(f"| {t['key']} | {t['title']} | {t['agent']} | {icon.get(t['status'], '')} {t['status']} | "
                     f"{', '.join(shown) or '-'} |")
    logs = [t for t in tasks if t["summary"] or t["error"]]
    if logs:
        lines += ["", "## Task log", ""]
        for t in logs:
            lines += [f"### {t['key']} — {t['title']} ({t['agent']})", "", t["summary"] or f"*{t['error']}*", ""]
    return "\n".join(lines).rstrip() + "\n"


class Orchestrator:
    def __init__(self, storage: Storage, cfg: dict, stream_fn=stream_chat, extra_tools=None):
        self.storage, self.cfg, self.stream_fn = storage, cfg, stream_fn
        self.personas = PersonaStore(cfg.get("personas_dir", "personas"))
        self.extra_tools = extra_tools
        self.workspaces_dir = Path(cfg.get("workspaces_dir", "workspaces"))

    def team_for(self, ids: list) -> dict:
        team = {}
        for pid in ids:
            persona = self.personas.get(pid)
            if persona is None:
                raise ProjectError(f"Unknown persona '{pid}' in the team")
            team[pid] = persona
        return team

    def workspace(self, project: dict) -> Workspace:
        return Workspace(str(self.workspaces_dir / project["workspace"]))

    def create_project(self, goal: str, team_ids: Optional[list] = None, workspace: Optional[str] = None,
                       title: Optional[str] = None) -> int:
        ids = list(dict.fromkeys(team_ids or load_team_ids(self.cfg.get("team_path", "team.yaml"))))
        if "pm" not in ids:
            ids.insert(0, "pm")
        team = self.team_for(ids)
        if not [a for a in team if a != "pm"]:
            raise ProjectError("The team needs at least one agent besides the project manager")
        if workspace is not None and not SLUG_RE.match(workspace):
            raise ProjectError("workspace must be a simple folder name (a-z, 0-9, - or _)")
        title = (title or goal).strip()[:60]
        pid = self.storage.create_project(title, goal.strip(), ids, workspace or "pending")
        if workspace is None:
            self.storage.update_project(pid, workspace=f"{pid}-{slug(title)}")
        self.workspace(self.storage.get_project(pid))
        return pid

    def _structured(self, messages: list, schema: dict, model: str) -> str:
        num_ctx = resolve_settings(self.cfg, self.storage.all_settings())["num_ctx"]
        events = self.stream_fn(self.cfg["ollama_url"], model, messages, num_ctx, think=False, format_schema=schema)
        return "".join(e.text for e in events if isinstance(e, Token))

    def plan(self, project_id: int) -> Iterator[Evt]:
        project = self.storage.get_project(project_id)
        team, ws = self.team_for(project["team"]), self.workspace(project)
        pm, max_tasks = team["pm"], int(self.cfg.get("max_plan_tasks", 8))
        model = pm.model or self.cfg["model"]
        schema = plan_schema([a for a in team if a != "pm"], max_tasks)
        files = list_files(ws)
        system = [pm.system_prompt, "", "TEAM (assign tasks ONLY to these agents):"]
        for p in team.values():
            if p.id != "pm":
                system.append(f"- {p.id}: {p.name}. {p.role}. Can create files in: {', '.join(p.files.get('write', [])) or 'nowhere'}")
        system += ["", f"RULES: at most {max_tasks} small tasks, listed in execution order. 'depends_on' may only name "
                   "EARLIER task ids. 'files' = the files the task must create, inside the agent's folders.", "",
                   "Current project files:", "\n".join(files) or "(empty project)"]
        shown = previews(ws, files)
        if shown:
            system += ["", "Contents of some existing files:", shown]
        messages = [{"role": "system", "content": "\n".join(system)}, {"role": "user", "content": project["goal"]}]

        yield Evt("project", {"id": project_id, "title": project["title"], "workspace": project["workspace"],
                              "team": project["team"]})
        plan, errors = None, []
        for attempt in range(int(self.cfg.get("plan_retries", 2)) + 1):
            yield Evt("planning", {"attempt": attempt + 1})
            try:
                raw = self._structured(messages, schema, model)
            except OllamaError as e:
                self.storage.update_project(project_id, status="failed", error=str(e))
                yield Evt("error", {"message": str(e)})
                return
            try:
                plan = normalize_plan(parse_json(raw))
                errors = validate_plan(plan, team, max_tasks)
            except ValueError:
                plan, errors = None, ["the answer is not valid JSON"]
            if not errors:
                break
            messages += [{"role": "assistant", "content": raw}, {"role": "user", "content":
                         "Your plan is invalid:\n- " + "\n- ".join(errors) + "\nReturn the corrected complete plan."}]
        if errors:
            msg = "The PM could not produce a valid plan: " + "; ".join(errors[:3])
            self.storage.update_project(project_id, status="failed", error=msg)
            yield Evt("error", {"message": msg, "problems": errors})
            return
        self.storage.set_tasks(project_id, plan["tasks"])
        self.storage.update_project(project_id, status="awaiting_approval", summary=plan["summary"], error=None)
        self._write_md(project_id)
        yield Evt("plan", {"id": project_id, "summary": plan["summary"], "tasks": plan["tasks"]})

    def check_runnable(self, project_id: int, plan: Optional[dict] = None) -> None:
        project = self.storage.get_project(project_id)
        if project is None:
            raise ProjectError(f"Project {project_id} not found", 404)
        if project["status"] not in RUNNABLE:
            raise ProjectError(f"Project is {project['status']}: it cannot be started now", 409)
        if plan is not None:
            if project["status"] != "awaiting_approval":
                raise ProjectError("The plan can only be edited before the project has started", 409)
            clean = normalize_plan(plan)
            errors = validate_plan(clean, self.team_for(project["team"]), int(self.cfg.get("max_plan_tasks", 8)))
            if errors:
                raise ProjectError("Invalid plan: " + "; ".join(errors))

    def run(self, project_id: int, plan: Optional[dict] = None) -> Iterator[Evt]:
        self.check_runnable(project_id, plan)
        if plan is not None:
            self.storage.set_tasks(project_id, normalize_plan(plan)["tasks"])
        if not self.storage.claim_project(project_id, RUNNABLE, "running"):
            yield Evt("error", {"message": "The project is already running"})
            return
        self.storage.reset_tasks_for_run(project_id)
        project = self.storage.get_project(project_id)
        team, ws = self.team_for(project["team"]), self.workspace(project)
        yield Evt("project_start", {"id": project_id, "tasks": len(self.storage.list_tasks(project_id))})
        try:
            for task in self.storage.list_tasks(project_id):
                states = {t["key"]: t["status"] for t in self.storage.list_tasks(project_id)}
                if task["status"] == "done":
                    continue
                blocked = [d for d in task["depends_on"] if states.get(d) != "done"]
                if blocked:
                    self.storage.update_task(task["id"], status="skipped", error=f"depends on unfinished {', '.join(blocked)}")
                    self._write_md(project_id)
                    yield Evt("task_skipped", {"key": task["key"], "reason": f"depends on {', '.join(blocked)}"})
                    continue
                yield from self._run_task(project, task, team, ws)
            tasks = self.storage.list_tasks(project_id)
            status = "done" if all(t["status"] == "done" for t in tasks) else "failed"
            self.storage.update_project(project_id, status=status)
        except _Cancelled:
            self._reset_running(project_id)
            self.storage.update_project(project_id, status="cancelled")
            status = "cancelled"
        except OllamaUnavailable as e:
            self._reset_running(project_id)
            self.storage.update_project(project_id, status="paused", error=str(e))
            self._write_md(project_id)
            yield Evt("error", {"message": str(e)})
            return
        except GeneratorExit:
            self._reset_running(project_id)
            self.storage.update_project(project_id, status="paused")
            raise
        self._write_md(project_id)
        counts = {}
        for t in self.storage.list_tasks(project_id):
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        yield Evt("project_done", {"id": project_id, "status": status, "tasks": counts})

    def _reset_running(self, project_id: int) -> None:
        for t in self.storage.list_tasks(project_id):
            if t["status"] == "running":
                self.storage.update_task(t["id"], status="pending")

    def _write_md(self, project_id: int) -> None:
        project = self.storage.get_project(project_id)
        ws = self.workspace(project)
        (ws.root / "PROJECT.md").write_text(render_project_md(project, self.storage.list_tasks(project_id)), encoding="utf-8")

    def _registry(self, ws: Workspace, persona: Persona) -> ToolRegistry:
        tools = file_tools(ws, FilePolicy.from_persona(persona.files))
        if self.extra_tools:
            tools += self.extra_tools(ws, persona)
        return ToolRegistry(tools)

    def _task_prompt(self, project: dict, task: dict, ws: Workspace) -> str:
        done = {t["key"]: t for t in self.storage.list_tasks(project["id"])}
        lines = [f"Project goal: {project['goal']}", "", f"YOUR TASK ({task['key']}): {task['title']}", task["description"]]
        if task["files"]:
            lines += ["", "Files you must create: " + ", ".join(task["files"])]
        for dep in task["depends_on"]:
            if dep in done and done[dep]["summary"]:
                lines += ["", f"Result of {dep} ({done[dep]['agent']}): {done[dep]['summary'][:700]}"]
        lines += ["", "Project files so far:", "\n".join(list_files(ws)) or "(none yet)", "",
                  "Work ONLY on this task. Create the files with write_file, then reply with a short summary."]
        return "\n".join(lines)

    def _run_task(self, project: dict, task: dict, team: dict, ws: Workspace) -> Iterator[Evt]:
        pid, persona = project["id"], team[task["agent"]]
        chat = ChatService(self.storage, {**self.cfg, "tools": True}, self.stream_fn, self._registry(ws, persona))
        conv_id = self.storage.create_conversation(f"[{project['title'][:30]}] {task['title']}"[:100],
                                                   persona=persona.id, model=persona.model or self.cfg["model"])
        self.storage.update_task(task["id"], status="running", conversation_id=conv_id, started_at=_now(), error=None)
        self._write_md(pid)
        yield Evt("task_start", {"key": task["key"], "title": task["title"], "agent": persona.id, "conversation_id": conv_id})

        prompt, retries_left, written, summary, error = self._task_prompt(project, task, ws), int(self.cfg.get("task_retries", 1)), [], "", None
        try:
            while True:
                events = chat.send(conv_id, prompt, think=bool(self.cfg.get("agent_think", False)))
                while events is not None:
                    current, events = events, None
                    for ev in current:
                        if self.storage.get_project(pid)["status"] == "cancelled":
                            current.close()
                            raise _Cancelled()
                        if isinstance(ev, Token):
                            yield Evt("agent_token", {"key": task["key"], "text": ev.text})
                        elif isinstance(ev, ToolStart):
                            yield Evt("agent_tool_call", {"key": task["key"], "name": ev.name, "arguments": ev.arguments})
                        elif isinstance(ev, ToolResult):
                            yield Evt("agent_tool_result", {"key": task["key"], "name": ev.name,
                                                            "result": ev.result[:300], "status": ev.status})
                        elif isinstance(ev, Interaction):         # agents work unattended: decline questions
                            events = chat.resume(ev.id, {"cancelled": True})
                        elif isinstance(ev, Finished):
                            reply = self.storage.get_message(ev.assistant_message_id)
                            summary = reply["content"].strip()
                            for step in reply.get("tool_trace") or []:
                                written += [c["arguments"].get("path") for c in step["calls"]
                                            if c["name"] == "write_file" and c["status"] == "done"]
                missing = [f for f in task["files"] if not (ws.root / f).is_file()]
                if not missing or retries_left <= 0:
                    break
                retries_left -= 1
                prompt = f"You have not created these files yet: {', '.join(missing)}. Create them now with write_file, then reply with a one-paragraph summary."
            if missing:
                error = f"expected files were not created: {', '.join(missing)}"
        except OllamaUnavailable:
            raise
        except OllamaError as e:
            error = str(e)
        written = list(dict.fromkeys(p for p in written if p))
        status = "failed" if error else "done"
        self.storage.update_task(task["id"], status=status, summary=summary[:2000] or None, error=error,
                                 written=written, finished_at=_now())
        self._write_md(pid)
        yield Evt("task_done" if status == "done" else "task_failed",
                  {"key": task["key"], "agent": persona.id, "summary": summary[:2000], "written": written,
                   "error": error, "conversation_id": conv_id})
