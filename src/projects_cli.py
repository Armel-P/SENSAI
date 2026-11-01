"""Run a multi-agent project from the terminal (no UI needed).

  python -m src.projects_cli "Build a todo API" --team pm,backend,tester --workspace todo
  python -m src.projects_cli --list
  python -m src.projects_cli --resume 3          # approve / resume / retry a project
"""
import argparse
import sys

from src.core.config import load_config
from src.core.orchestrator import Orchestrator, ProjectError
from src.core.sandbox import make_extra_tools
from src.core.storage import Storage


def show_plan(data: dict, out) -> None:
    out(f"\nPlan: {data['summary']}")
    for t in data["tasks"]:
        deps = f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        out(f"  {t['id']}  [{t['agent']}] {t['title']}{deps}")
        if t["files"]:
            out(f"        files: {', '.join(t['files'])}")


def show(ev, out) -> None:
    d = ev.data
    if ev.name == "planning":
        out(f"Planning (attempt {d['attempt']})…")
    elif ev.name == "plan":
        show_plan(d, out)
    elif ev.name == "task_start":
        out(f"\n▶ {d['key']} [{d['agent']}] {d['title']}")
    elif ev.name == "agent_tool_call":
        target = d["arguments"].get("path") or d["arguments"].get("code", "")[:50].replace("\n", " ")
        out(f"   · {d['name']} {target}")
    elif ev.name == "agent_tool_result" and d["status"] == "error":
        out(f"     ✗ {d['result'][:160]}")
    elif ev.name == "task_done":
        out(f"✔ {d['key']} done: {(d['summary'].splitlines() or [''])[0][:140]}")
    elif ev.name == "task_failed":
        out(f"✘ {d['key']} failed: {d['error']}")
    elif ev.name == "task_skipped":
        out(f"⏭ {d['key']} skipped ({d['reason']})")
    elif ev.name == "error":
        out(f"ERROR: {d['message']}")


def execute(orch: Orchestrator, pid: int, out) -> int:
    status = None
    for ev in orch.run(pid):
        show(ev, out)
        if ev.name == "project_done":
            status = ev.data["status"]
    project = orch.storage.get_project(pid)
    out(f"\nProject {pid}: {status or project['status']}. Files: {orch.workspaces_dir / project['workspace']}")
    return 0 if status == "done" else 1


def main(argv=None, *, orch=None, input_fn=input, out=print) -> int:
    ap = argparse.ArgumentParser(description="Sensai multi-agent projects")
    ap.add_argument("goal", nargs="?", help="what the team should build")
    ap.add_argument("--team", help="comma-separated persona ids (default: team.yaml)")
    ap.add_argument("--workspace", help="existing folder under workspaces/ to build upon")
    ap.add_argument("--title")
    ap.add_argument("--yes", action="store_true", help="approve the plan without asking")
    ap.add_argument("--resume", type=int, metavar="ID", help="approve / resume / retry a project")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)

    if orch is None:
        cfg = load_config(args.config)
        orch = Orchestrator(Storage(cfg["db_path"]), cfg, extra_tools=make_extra_tools(cfg))
    if args.list:
        for p in orch.storage.list_projects():
            tasks = orch.storage.list_tasks(p["id"])
            out(f"{p['id']:>3}  {p['status']:<18} {sum(t['status'] == 'done' for t in tasks)}/{len(tasks)}  {p['title']}  ({p['workspace']})")
        return 0
    try:
        if args.resume:
            orch.check_runnable(args.resume)
            return execute(orch, args.resume, out)
        if not args.goal:
            ap.error("give a goal, or --resume ID, or --list")
        pid = orch.create_project(args.goal, args.team.split(",") if args.team else None, args.workspace, args.title)
    except ProjectError as e:
        out(f"error: {e}")
        return 2

    failed = False
    for ev in orch.plan(pid):
        show(ev, out)
        failed = failed or ev.name == "error"
    if failed:
        return 1
    if not args.yes and input_fn("\nRun this plan? [y/N] ").strip().lower() not in ("y", "yes"):
        out(f"Not started. Approve it later with: python -m src.projects_cli --resume {pid}")
        return 0
    return execute(orch, pid, out)


if __name__ == "__main__":
    sys.exit(main())
