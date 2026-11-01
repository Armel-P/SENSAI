import json

import pytest
from fastapi.testclient import TestClient

from src import server
from src.core.ollama_client import Done, OllamaUnavailable, Token, ToolCall
from src.core.orchestrator import Orchestrator, ProjectError, validate_plan
from src.core.personas import PersonaStore
from src.core.storage import Storage

GOOD = {"summary": "Todo API, then a page for it", "tasks": [
    {"id": "t1", "title": "API", "agent": "backend", "description": "Write the FastAPI app",
     "depends_on": [], "files": ["backend/app.py"]},
    {"id": "t2", "title": "UI", "agent": "frontend", "description": "Write the page",
     "depends_on": ["t1"], "files": ["frontend/index.html"]}]}


def plan_round(plan):
    return [Token(plan if isinstance(plan, str) else json.dumps(plan)), Done(5, 5, 0.1)]


def agent_round(*events):
    return [*events, Done(5, 5, 0.1)]


def write(path, content="x"):
    return ToolCall("write_file", {"path": path, "content": content})


class Script:
    """Fake model: planning calls (format given) and agent calls (tools given) use separate queues."""
    def __init__(self, plans=(), agents=()):
        self.plans, self.agents, self.calls = list(plans), list(agents), []

    def stream(self, url, model, messages, num_ctx=8192, think=None, tools=None, format_schema=None):
        self.calls.append({"kind": "plan" if format_schema else "agent", "messages": [dict(m) for m in messages],
                           "format": format_schema, "think": think,
                           "tools": [t["function"]["name"] for t in tools] if tools else None})
        item = (self.plans if format_schema else self.agents).pop(0)
        if isinstance(item, Exception):
            raise item
        yield from item

    def of(self, kind):
        return [c for c in self.calls if c["kind"] == kind]


def make(tmp_path, script, **cfg_extra):
    cfg = {"ollama_url": "x", "model": "m", "num_ctx": 8192, "system_prompt": "BASE", "personas_dir": "personas",
           "profile_path": str(tmp_path / "none.yaml"), "db_path": str(tmp_path / "t.db"),
           "workspaces_dir": str(tmp_path / "ws"), "team_path": str(tmp_path / "team.yaml"), **cfg_extra}
    st = Storage(cfg["db_path"])
    return Orchestrator(st, cfg, script.stream), st, cfg


def names(events):
    return [e.name for e in events]


TEAM = ["pm", "backend", "frontend"]
BACKEND_OK = agent_round(write("backend/app.py", "print('api')\n")), agent_round(Token("Wrote the API."))
FRONTEND_OK = agent_round(write("frontend/index.html", "<h1>todo</h1>")), agent_round(Token("UI done."))


def test_plan_is_requested_as_structured_json_and_stored(tmp_path):
    script = Script(plans=[plan_round(GOOD)])
    orch, st, _ = make(tmp_path, script)
    pid = orch.create_project("Build a todo app", TEAM)
    events = list(orch.plan(pid))
    assert names(events) == ["project", "planning", "plan"]
    call = script.of("plan")[0]
    assert call["format"]["properties"]["tasks"]["items"]["properties"]["agent"]["enum"] == ["backend", "frontend"]
    assert call["format"]["properties"]["tasks"]["maxItems"] == 8 and call["think"] is False and call["tools"] is None
    system = call["messages"][0]["content"]
    assert "You are the project manager" in system and "- backend:" in system and "backend/**" in system
    project = st.get_project(pid)
    assert project["status"] == "awaiting_approval" and project["summary"] == GOOD["summary"]
    assert [t["key"] for t in st.list_tasks(pid)] == ["t1", "t2"] and st.list_tasks(pid)[1]["depends_on"] == ["t1"]
    md = (tmp_path / "ws" / project["workspace"] / "PROJECT.md").read_text()
    assert "Todo API" in md and "| t1 |" in md


def test_an_invalid_plan_is_sent_back_with_the_exact_problems(tmp_path):
    bad = {"summary": "x", "tasks": [
        {"id": "t1", "title": "A", "agent": "ghost", "description": "d", "depends_on": ["t2"], "files": []},
        {"id": "t2", "title": "B", "agent": "frontend", "description": "d", "depends_on": [], "files": ["backend/x.py"]}]}
    script = Script(plans=[plan_round(bad), plan_round(GOOD)])
    orch, st, _ = make(tmp_path, script)
    pid = orch.create_project("goal here", TEAM)
    events = list(orch.plan(pid))
    assert names(events) == ["project", "planning", "planning", "plan"]
    feedback = script.of("plan")[1]["messages"][-1]["content"]
    assert "unknown agent 'ghost'" in feedback and "not defined EARLIER" in feedback
    assert "agent 'frontend' cannot create 'backend/x.py'" in feedback and "Allowed to write: frontend/**" in feedback
    assert st.get_project(pid)["status"] == "awaiting_approval"


def test_the_pm_gives_up_after_the_allowed_retries(tmp_path):
    script = Script(plans=[plan_round("not json at all")] * 3)
    orch, st, _ = make(tmp_path, script)
    pid = orch.create_project("goal here", TEAM)
    events = list(orch.plan(pid))
    assert events[-1].name == "error" and "not valid JSON" in events[-1].data["message"]
    assert len(script.of("plan")) == 3 and st.get_project(pid)["status"] == "failed"
    assert st.list_tasks(pid) == []


def test_fenced_json_is_accepted(tmp_path):
    script = Script(plans=[plan_round("```json\n" + json.dumps(GOOD) + "\n```")])
    orch, st, _ = make(tmp_path, script)
    pid = orch.create_project("goal here", TEAM)
    assert list(orch.plan(pid))[-1].name == "plan"


def test_validate_plan_rules():
    team = PersonaStore("personas")
    team = {i: team.get(i) for i in ("pm", "backend", "frontend")}
    assert validate_plan(GOOD, team, 8) == []
    t = lambda **k: {"id": "t1", "title": "T", "agent": "backend", "description": "d", "depends_on": [], "files": [], **k}
    assert any("project manager" in e for e in validate_plan({"tasks": [t(agent="pm")]}, team, 8))
    assert any("duplicate" in e for e in validate_plan({"tasks": [t(), t()]}, team, 8))
    assert any("too many" in e for e in validate_plan({"tasks": [t(id=f"t{i}") for i in range(3)]}, team, 2))
    assert any("invalid file path" in e for e in validate_plan({"tasks": [t(files=["../x"])]}, team, 8))
    assert any("invalid file path" in e for e in validate_plan({"tasks": [t(files=["/etc/x"])]}, team, 8))
    assert validate_plan({"tasks": []}, team, 8) and validate_plan("nope", team, 8)


def test_team_and_workspace_validation(tmp_path):
    orch, st, _ = make(tmp_path, Script())
    with pytest.raises(ProjectError, match="Unknown persona"):
        orch.create_project("goal here", ["pm", "wizard"])
    with pytest.raises(ProjectError, match="at least one agent"):
        orch.create_project("goal here", ["pm"])
    for bad in ("../evil", "a/b", "A B", ""):
        with pytest.raises(ProjectError, match="workspace"):
            orch.create_project("goal here", TEAM, workspace=bad)
    pid = orch.create_project("goal here", ["backend"])
    assert st.get_project(pid)["team"] == ["pm", "backend"]


def test_an_existing_project_is_shown_to_the_pm(tmp_path):
    script = Script(plans=[plan_round(GOOD)])
    orch, st, _ = make(tmp_path, script)
    (tmp_path / "ws" / "mine" / "backend").mkdir(parents=True)
    (tmp_path / "ws" / "mine" / "backend" / "app.py").write_text("@app.get('/todos')\ndef todos(): ...\n")
    (tmp_path / "ws" / "mine" / ".env").write_text("SECRET=1")
    pid = orch.create_project("Build the front for it", TEAM, workspace="mine")
    list(orch.plan(pid))
    system = script.of("plan")[0]["messages"][0]["content"]
    assert "backend/app.py" in system and "@app.get('/todos')" in system and ".env" not in system and "SECRET" not in system


def planned(tmp_path, script, **cfg):
    orch, st, c = make(tmp_path, script, **cfg)
    pid = orch.create_project("Build a todo app", TEAM)
    list(orch.plan(pid))
    return orch, st, pid


def test_the_agents_run_in_order_and_build_the_files(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[*BACKEND_OK, *FRONTEND_OK])
    orch, st, pid = planned(tmp_path, script)
    events = list(orch.run(pid))
    assert names(events)[0] == "project_start" and names(events)[-1] == "project_done"
    assert [e.name for e in events if e.name in ("task_start", "task_done")] == ["task_start", "task_done"] * 2
    assert events[-1].data == {"id": pid, "status": "done", "tasks": {"done": 2}}
    ws = tmp_path / "ws" / st.get_project(pid)["workspace"]
    assert (ws / "backend/app.py").read_text() == "print('api')\n" and (ws / "frontend/index.html").exists()
    t1, t2 = st.list_tasks(pid)
    assert (t1["status"], t1["summary"], t1["written"]) == ("done", "Wrote the API.", ["backend/app.py"])
    assert t2["status"] == "done" and st.get_project(pid)["status"] == "done"
    agents = script.of("agent")
    assert agents[0]["messages"][0]["content"].startswith("You are a backend developer")
    assert agents[2]["messages"][0]["content"].startswith("You are a frontend developer")
    assert agents[0]["tools"] == ["list_dir", "read_file", "write_file"] and agents[0]["think"] is False
    prompt = agents[2]["messages"][1]["content"]                       # t2 sees what t1 produced
    assert "Result of t1 (backend): Wrote the API." in prompt and "backend/app.py" in prompt
    convs = {c["persona"] for c in st.list_conversations()}
    assert convs == {"backend", "frontend"} and t1["conversation_id"] != t2["conversation_id"]
    md = (ws / "PROJECT.md").read_text()
    assert md.count("✅") >= 2 and "UI done." in md and "**Status:** done" in md


def test_an_agent_that_forgets_the_files_gets_one_follow_up(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[
        agent_round(Token("All done!")),
        agent_round(write("backend/app.py")), agent_round(Token("Now really done.")), *FRONTEND_OK])
    orch, st, pid = planned(tmp_path, script)
    events = list(orch.run(pid))
    assert events[-1].data["status"] == "done"
    follow_up = script.of("agent")[1]["messages"][-1]["content"]
    assert follow_up.startswith("You have not created these files yet: backend/app.py")
    assert st.list_tasks(pid)[0]["summary"] == "Now really done."


def test_a_failed_task_skips_its_dependents_and_a_rerun_only_redoes_what_is_left(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[agent_round(Token("done")), agent_round(Token("still done"))])
    orch, st, pid = planned(tmp_path, script)
    events = list(orch.run(pid))
    assert [e.name for e in events if e.name.startswith("task_")] == ["task_start", "task_failed", "task_skipped"]
    assert events[-1].data["status"] == "failed" and st.get_project(pid)["status"] == "failed"
    t1, t2 = st.list_tasks(pid)
    assert "expected files were not created" in t1["error"] and t2["status"] == "skipped"
    assert len(script.of("agent")) == 2
    script.agents += [*BACKEND_OK, *FRONTEND_OK]
    events = list(orch.run(pid))
    assert events[-1].data["status"] == "done" and st.get_project(pid)["status"] == "done"


def test_agents_cannot_write_outside_their_folders(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[
        agent_round(write("frontend/sneaky.html"), write("backend/app.py")), agent_round(Token("ok")), *FRONTEND_OK])
    orch, st, pid = planned(tmp_path, script)
    events = list(orch.run(pid))
    results = [e.data for e in events if e.name == "agent_tool_result"][:2]
    assert [r["status"] for r in results] == ["error", "done"] and "not permitted" in results[0]["result"]
    ws = tmp_path / "ws" / st.get_project(pid)["workspace"]
    assert not (ws / "frontend/sneaky.html").exists() and (ws / "backend/app.py").exists()


def test_closing_the_stream_pauses_and_resuming_skips_finished_tasks(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[*BACKEND_OK])
    orch, st, pid = planned(tmp_path, script)
    gen = orch.run(pid)
    for ev in gen:
        if ev.name == "task_done":
            break
    gen.close()
    assert st.get_project(pid)["status"] == "paused"
    assert [t["status"] for t in st.list_tasks(pid)] == ["done", "pending"]
    script.agents += [*FRONTEND_OK]
    events = list(orch.run(pid))
    assert [e.data["key"] for e in events if e.name == "task_start"] == ["t2"]
    assert st.get_project(pid)["status"] == "done" and len(script.of("agent")) == 4


def test_cancelling_stops_the_run_between_events(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[agent_round(Token("Working. "), write("backend/app.py")),
                                                      agent_round(Token("x"))])
    orch, st, pid = planned(tmp_path, script)
    events = []
    for ev in orch.run(pid):
        events.append(ev)
        if ev.name == "agent_token":
            st.update_project(pid, status="cancelled")
    assert events[-1].name == "project_done" and events[-1].data["status"] == "cancelled"
    assert st.get_project(pid)["status"] == "cancelled" and st.list_tasks(pid)[0]["status"] == "pending"


def test_ollama_going_down_pauses_the_project_instead_of_failing_it(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[OllamaUnavailable("Ollama is not running")])
    orch, st, pid = planned(tmp_path, script)
    events = list(orch.run(pid))
    assert events[-1].name == "error" and st.get_project(pid)["status"] == "paused"
    assert st.list_tasks(pid)[0]["status"] == "pending"
    script.agents += [*BACKEND_OK, *FRONTEND_OK]
    assert list(orch.run(pid))[-1].data["status"] == "done"


def test_the_user_can_edit_the_plan_before_approving(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[*BACKEND_OK])
    orch, st, pid = planned(tmp_path, script)
    only_backend = {"summary": "just the API", "tasks": [GOOD["tasks"][0]]}
    broken = {"summary": "x", "tasks": [{**GOOD["tasks"][0], "agent": "ghost"}]}
    with pytest.raises(ProjectError, match="unknown agent"):
        orch.check_runnable(pid, broken)
    events = list(orch.run(pid, only_backend))
    assert [t["key"] for t in st.list_tasks(pid)] == ["t1"] and events[-1].data["status"] == "done"
    with pytest.raises(ProjectError) as err:
        orch.check_runnable(pid)
    assert err.value.status == 409


def test_a_restart_pauses_whatever_was_running(tmp_path):
    script = Script(plans=[plan_round(GOOD)])
    orch, st, pid = planned(tmp_path, script)
    st.claim_project(pid, ("awaiting_approval",), "running")
    st.update_task(st.list_tasks(pid)[0]["id"], status="running")
    st.reset_running_projects()
    assert st.get_project(pid)["status"] == "paused" and st.list_tasks(pid)[0]["status"] == "pending"


def sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def test_api_plan_approve_files_and_errors(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[*BACKEND_OK, *FRONTEND_OK])
    orch, st, cfg = make(tmp_path, script)
    client = TestClient(server.create_app(cfg, st, script.stream))

    assert client.post("/api/projects", json={"goal": "x"}).status_code == 422
    assert client.post("/api/projects", json={"goal": "Build a todo app", "team": ["pm", "wizard"]}).status_code == 422
    assert client.post("/api/projects", json={"goal": "Build a todo app", "workspace": "../x"}).status_code == 422

    ev = sse(client.post("/api/projects", json={"goal": "Build a todo app", "team": TEAM}).text)
    assert [e for e, _ in ev] == ["project", "planning", "plan"]
    pid = ev[0][1]["id"]
    assert [t["agent"] for t in ev[-1][1]["tasks"]] == ["backend", "frontend"]
    assert client.get(f"/api/projects/{pid}").json()["project"]["status"] == "awaiting_approval"
    assert client.get("/api/projects").json()[0]["tasks_total"] == 2

    bad = {"summary": "x", "tasks": [{**GOOD["tasks"][0], "agent": "ghost"}]}
    assert client.post(f"/api/projects/{pid}/approve", json={"plan": bad}).status_code == 422
    assert client.post("/api/projects/999/approve").status_code == 404

    ev = sse(client.post(f"/api/projects/{pid}/approve").text)
    names_ = [e for e, _ in ev]
    assert names_[0] == "project_start" and names_[-1] == "project_done" and "agent_tool_call" in names_
    assert ev[-1][1]["status"] == "done"
    assert client.post(f"/api/projects/{pid}/approve").status_code == 409
    assert client.post(f"/api/projects/{pid}/cancel").status_code == 409

    files = client.get(f"/api/projects/{pid}/files").json()["files"]
    assert {"backend/app.py", "frontend/index.html", "PROJECT.md"} <= set(files)
    assert client.get(f"/api/projects/{pid}/files/backend/app.py").json()["content"] == "print('api')\n"
    assert client.get(f"/api/projects/{pid}/files/nope.txt").status_code == 404
    ws = tmp_path / "ws" / client.get(f"/api/projects/{pid}").json()["project"]["workspace"]
    (ws / ".env").write_text("SECRET=1")
    assert client.get(f"/api/projects/{pid}/files/.env").status_code == 403
    assert client.get(f"/api/projects/{pid}/files/%2e%2e/%2e%2e/%2e%2e/etc/passwd").status_code in (403, 404)

    assert client.delete(f"/api/projects/{pid}").json() == {"deleted": pid}
    assert client.get(f"/api/projects/{pid}").status_code == 404 and (ws / "backend/app.py").exists()


def test_api_cancel_a_project_that_is_waiting_for_approval(tmp_path):
    script = Script(plans=[plan_round(GOOD)], agents=[*BACKEND_OK, *FRONTEND_OK])
    orch, st, cfg = make(tmp_path, script)
    client = TestClient(server.create_app(cfg, st, script.stream))
    pid = sse(client.post("/api/projects", json={"goal": "Build a todo app", "team": TEAM}).text)[0][1]["id"]
    assert client.post(f"/api/projects/{pid}/cancel").json()["status"] == "cancelled"
    ev = sse(client.post(f"/api/projects/{pid}/approve").text)
    assert ev[-1][0] == "project_done" and ev[-1][1]["status"] == "done"
