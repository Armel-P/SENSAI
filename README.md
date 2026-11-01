# Sensai

A local AI assistant built **around** a model instead of inside it: a CLI chatbot and a web UI on top of
[Ollama](https://ollama.com), with persistent memory, tool calling, human-in-the-loop approvals, and a
**multi-agent software team** (project manager + specialised agents) that writes and tests code in a sandbox.

- **Language:** Python 3.10+. **No LLM framework** (no LangChain / LlamaIndex): every model call goes to the
  Ollama HTTP API directly (`src/core/ollama_client.py`, plain `requests`).
- **Everything runs locally.** The web server has no authentication and listens on `127.0.0.1` only.

## Setup

```bash
# 1. Ollama + a model (the weights live in ~/.ollama, not in this repo)
ollama pull qwen3:8b            # ~5 GB; supports tools and a thinking mode

# 2. Python environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Check everything
python -m pytest                # the tests need no Ollama (the model is scripted)
```

The model is chosen in `config.yaml` (`model:`) or per run with `--model`, never in the code.

## Usage

### Chat in the terminal
```bash
python -m src.cli                       # new conversation
python -m src.cli --model llama3.2      # another model, no code change
python -m src.cli --last                # resume the most recent conversation
python -m src.cli --resume 4 --think    # resume #4 and show the model's reasoning
```
Commands inside the chat: `/list` `/new` `/resume <id>` `/history` `/title <text>` `/budget [n|off]` `/ctx [n]` `/help` `/quit`.

### Web UI
```bash
python -m src.server                    # http://127.0.0.1:8000
```
Streaming chat, conversation list (rename, fork, export, delete), edit-and-branch, Stop button, context meter
with a live token-budget popover, model selector, thinking toggle, profile dialog and a saved-prompt library
(type `/` in the message box).

### Tools, questions and approvals
Set `tools: true` in `config.yaml`. The model can then call `calculate`, `get_current_time`, `ask_user`
(multiple-choice or free-text question to you) and `save_note` (**asks your approval first**). In the terminal:
```
you> what is (12+3)*4 and then save the result as a note
[tool] calculate({"expression": "(12+3)*4"}) -> 60 (done)
? Allow the assistant to run 'save_note'?
  save_note({"title": "Result", "text": "60"})
  Approve? [y/N] y
```
Over HTTP the stream emits an `interaction` event; answer it with `POST /api/interactions/{id}/answer`.

### MCP servers (T1)
Servers listed under `mcp_servers` in `config.yaml` are started with the app and their tools are offered to the model
as `<server>__<tool>`:
```yaml
mcp_servers:
  - name: notes                                   # Sensai's own server: search / list your saved notes
    command: [python, -m, src.mcp_servers.notes_server]
  - name: time                                    # a pre-existing server:  pip install mcp-server-time
    command: [python, -m, mcp_server_time]
    confirm: auto                                 # auto (default) | always | never
```
`confirm: auto` asks your approval (A3) for every tool the server does **not** mark as read-only. A server that fails to
start is reported (`[mcp] ...` in the CLI, `GET /api/mcp`) and never stops the app or the other servers.
Write your own with `src/mcp_servers/base.py` (about 40 lines, see `notes_server.py`).

### Multi-agent projects
A PM agent turns a goal into a task plan, **you approve it**, then specialised agents execute the tasks
in dependency order, each inside the project folder `workspaces/<name>/` with its own file permissions.

```bash
mkdir -p workspaces/todo                                    # optional: put an existing project here to build upon it
python -m src.projects_cli "A tiny static todo page" --team pm,frontend --workspace todo
python -m src.projects_cli --list
python -m src.projects_cli --resume 1                       # approve later / resume after a stop / retry after a failure
python -m src.projects_cli --rename-workspace 1 landing-page
```
- **Team:** `team.yaml` lists the default agents; `--team` overrides it per project.
- **Agents are personas:** one YAML file per agent in `personas/` (name, system prompt, allowed tools, and
  `files: {read: [...], write: [...]}` globs). Add or edit a file, no code or restart needed.
- **Existing backend, agents build the front:** put the backend in `workspaces/<name>/backend/`, use
  `--team pm,frontend,tester`; the PM sees the file list and previews of the existing files.
- **Result:** the files in the workspace, plus `PROJECT.md` (status table and task log), plus one conversation per
  task in the web sidebar.

### HTTP API (overview)
| Area | Endpoints |
|---|---|
| Conversations | `GET/POST /api/conversations`, `GET/PATCH/DELETE /api/conversations/{id}`, `POST .../{id}/fork`, `GET .../{id}/export?format=json\|markdown` |
| Chat (SSE) | `POST /api/conversations/{id}/messages` → events `started`, `compressed`, `thinking`, `token`, `tool_call`, `tool_result`, `interaction`, `finished`, `error` |
| Branches | `POST /api/messages/{id}/switch` |
| Questions / approvals | `POST /api/interactions/{id}/answer`, `.../abort` |
| MCP | `GET /api/mcp` (servers, start errors, tools) |
| Settings / profile / prompts | `GET/PUT /api/settings`, `GET/PUT /api/profile`, `/api/prompts` CRUD, `GET /api/personas`, `GET /api/models` |
| Projects (SSE) | `POST /api/projects`, `POST /api/projects/{id}/approve`, `/cancel`, `GET /api/projects[/{id}[/files[/{path}]]]` |

```bash
curl -N -X POST localhost:8000/api/conversations/1/messages -H 'Content-Type: application/json' -d '{"content":"hello"}'
```
Interactive API docs: `http://127.0.0.1:8000/docs`.

## Features

Catalog ids from the project's feature catalog. **Done** means implemented and covered by automated tests.

| Id | Feature | Status | Notes |
|---|---|---|---|
| B1 | Functional CLI chatbot | Done | streaming, history, clean errors, model via `--model` / `config.yaml` |
| M1 | Session persistence & profiles | Done | SQLite conversations; `profiles/user.yaml` injected in every system prompt, re-read on each message |
| M2 | Token budgeting & semantic compression | Done | summary of older turns near a cap you can change live; nothing is deleted |
| X1 | Web UI | Done | decoupled static front (ES modules) over the REST + SSE API |
| X2 | Branching: resume & interrupt | Done | edit a past message → new branch; Stop saves the partial reply |
| X3 | Export & publication | **Partial** | JSON (whole tree) and Markdown export. **Not done:** PDF, public share link |
| T1 | MCP / tool calling | Done | tool loop (Ollama function calling) + an **MCP client** (stdio, written from the spec, no SDK). Tested with Sensai's own MCP server and the official `mcp-server-time` |
| X4 | Questions & forms | Done | `ask_user` tool → options / free text → turn resumes (CLI and API) |
| A3 | Human-in-the-loop | Done | per-tool approval (`save_note`, `delete_file`) and approval of the PM's plan before any agent runs |
| A5 | Persona | Done | YAML personas, switch mid-conversation keeping history, per-persona tools and file permissions |
| A2 | Multi-agent orchestration | Done | PM → plan → specialised agents in dependency order, resumable |
| T4 | File access within permissions | Done | workspace sandboxing + per-agent read/write globs, checked before every operation |
| T2 | Sandboxed code execution | Done | `run_python` / `run_tests` in an isolated subprocess (see below) |
| T5 | Structured output | Done | the PM's plan uses Ollama's `format` JSON schema, then validated with retries |

Extras (not catalog items): saved-prompt library, conversation fork, workspace rename.

**Not implemented:** M3 (the `facts` table is reserved, no logic), M4 (`PROJECT.md` is written by code, not by
model-emitted edits), T3, R1/R2, A1, A4, A6, A7, EV1-EV4, X5.

## Architecture

```
 web/ (static ES modules) ─┐
 src/cli.py ───────────────┼─► src/core/chat.py  ChatService: one turn = model → (tool calls → results → model)* → reply
 src/server.py (FastAPI) ──┤        │  M2 summaries · personas (A5) · suspend/resume for questions & approvals
 src/projects_cli.py ──────┘        ▼
                          src/core/orchestrator.py   plan (T5) → approval (A3) → agents (A2)
                                    │
        ollama_client.py   storage.py (SQLite)   tools.py   workspace.py (T4)   sandbox.py + sandbox_runner.py (T2)
```
- **One core, three thin clients.** CLI, web server and project CLI all call the same services.
- **Conversations are trees** (`messages.parent_id`), which makes branching and forking cheap and non-destructive.
- **Summaries hang on a message id**, so they apply only to the branches that contain that message.
- **Questions and approvals suspend the turn**: its state is saved in SQLite and the stream ends; the answer is a new
  request that resumes it. Nothing blocks a thread, and a pending question survives a restart.
- **`sandbox_runner.py` is never imported.** `sandbox.py` starts it as a separate `python -I -B` process.

### Sandbox (T2)
Threat model: a small model making mistakes (infinite loop, fork bomb, huge allocation, `rm -rf`, network call, writing
to the real project by absolute path), not a determined attacker. Layers, each covered by a test: a fresh temporary
copy of the project per run (no `.env`, `.git` or symlinks; nothing persists), scrubbed environment, wall-clock timeout
with process-group kill, memory / CPU / file-size limits, an audit hook that refuses subprocess, fork, signals, sockets,
`ctypes` and any read or write outside the copy, and a network namespace (`unshare -Urn`) when the OS allows it.
**A subprocess is not a security boundary**; Docker would be stronger. Linux is the target platform.

## Configuration (`config.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `model`, `ollama_url`, `num_ctx` | `qwen3:8b`, `http://localhost:11434`, `8192` | model and context window |
| `token_budget`, `keep_recent` | `6000`, `4` | M2: condense above this many tokens (0 = off); newest messages kept verbatim. Both editable live in the UI / `/budget` |
| `tools`, `max_tool_rounds`, `notes_path` | `false`, `6`, `data/notes.md` | tool calling |
| `mcp_servers` | the notes server | T1: MCP servers to start (`name`, `command`, optional `env`, `cwd`, `confirm`, `timeout_s`) |
| `personas_dir`, `team_path`, `workspaces_dir` | `personas`, `team.yaml`, `workspaces` | multi-agent layout |
| `max_plan_tasks`, `plan_retries`, `task_retries` | `8`, `2`, `1` | plan size, PM corrections, follow-ups for missing files |
| `agent_think`, `agent_tool_rounds` | `false`, `12` | agent behaviour |
| `sandbox_timeout_s`, `sandbox_memory_mb`, `sandbox_output_chars` | `20`, `768`, `1800` | T2 limits |
| `host`, `port`, `db_path`, `profile_path` | `127.0.0.1`, `8000`, `sensai.db`, `profiles/user.yaml` | server and storage |

## Repository layout
```
config.yaml  team.yaml  requirements.txt  USER_STORIES.md
personas/    one YAML per agent (assistant, pm, backend, frontend, tester, reviewer)
profiles/    user.yaml (persistent user profile)
workspaces/  one folder per multi-agent project (git-ignored)
src/         cli.py  server.py  projects_cli.py  core/ (chat, storage, orchestrator, tools, mcp_client, workspace, sandbox, ...)
             mcp_servers/ (base.py, notes_server.py: Sensai's own MCP server)
web/         index.html  extras.css  js/ (state, api, ui, markdown, messages, chat, conversations, meter, prompts, ...)
tests/       pytest suite
```

## Known limits (honest assessment)
- The automated tests use a **scripted model**. Real behaviour (plan quality, tool choices, persona obedience) depends
  on the model and needs persona tuning; small models on CPU are slow (minutes per generated file).
- Multi-agent tasks run **sequentially**, and the agents only get file and sandbox tools (MCP tools are for the chat).
- MCP support covers **tools over stdio** only (no resources, prompts, sampling or HTTP transport).
- M2 summary quality depends on the model; a small model can lose details.
- The token estimate for not-yet-sent text is rough (about 3 characters per token); Ollama's real counts are used
  whenever available.
- No authentication: do not expose the server on a network.
