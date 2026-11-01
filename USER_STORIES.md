# User stories

Every feature is covered by at least two stories (a story may cover several closely related features).
Acceptance criteria are observable behaviours, and each one is checked by an automated test or a documented manual step.
The criteria for X4 and A3 are written for the terminal and the HTTP API.

| Feature | Stories |
|---|---|
| B1 CLI chatbot | US-01, US-02 |
| M1 Sessions & profiles | US-03, US-04 |
| M2 Token budgeting | US-05, US-06, US-07 |
| X1 Web UI | US-07, US-08 |
| X2 Branching | US-09, US-10 |
| X3 Export | US-11, US-12 |
| T1 MCP / tool calling | US-13, US-14, US-15, US-29, US-30 |
| X4 Questions & forms | US-15, US-16 |
| A3 Human-in-the-loop | US-17, US-18, US-21 |
| A5 Persona | US-19, US-20, US-23 |
| A2 Multi-agent | US-21, US-22 |
| T4 File access within permissions | US-23, US-24 |
| T2 Sandboxed code execution | US-25, US-26 |
| T5 Structured output | US-27, US-28 |

---

**US-01 — Chat with a local model from the terminal** `[B1]`
As a developer without a GPU server, I want to chat with a local model in my terminal and see the answer appear
progressively, so that I get feedback while a slow CPU model is still generating.
Acceptance criteria:
- The reply is printed token by token, not as one block.
- After each reply the prompt tokens, generated tokens and duration are shown.
- A follow-up that depends on the previous answer works (the history is sent every turn); empty input is ignored.

**US-02 — Switch models and survive errors** `[B1]`
As a user, I want to pick the model without editing code and get clear errors when something is wrong, so that I can
fix the problem and keep my session.
Acceptance criteria:
- `--model X` overrides `model:` of `config.yaml`.
- If Ollama is not running, `[error] Cannot reach Ollama at … Is it running?` is shown and I can retry in the same session.
- If the model is not pulled, the message tells me the exact `ollama pull X` to run; Ctrl+C interrupts a reply without crashing.

**US-03 — Resume yesterday's conversation** `[M1]`
As a student, I want to reopen an earlier conversation, so that I do not have to re-explain the context.
Acceptance criteria:
- `--last` or `--resume ID` reprints the history and continues from it; `/list` shows conversations, most recent first.
- In the web UI the sidebar lists conversations and opening one restores its messages.
- Conversations that never received a message are not listed.

**US-04 — A profile the assistant always knows** `[M1]`
As a regular user, I want my name, preferences and custom instructions to be applied to every conversation, so that I do
not repeat them.
Acceptance criteria:
- `profiles/user.yaml` (or the profile dialog / `PUT /api/profile`) is injected into the system prompt of every conversation.
- Editing it applies to the next message, without restarting anything.
- The web UI shows my name in the sidebar and on my messages.

**US-05 — Never hit the context window in a long conversation** `[M2]`
As a user in a long working session, I want old turns to be condensed automatically, so that the model keeps the key facts
and the conversation does not break at the window limit.
Acceptance criteria:
- When the context passes the token budget, older messages are replaced in the prompt by a summary and a "condensed N older messages" notice appears.
- The newest `keep_recent` messages stay verbatim and no message is deleted (the full history is still exported).
- If the summarization fails, the turn still completes normally.

**US-06 — Tune the token budget at any time** `[M2]`
As a developer demoing and stress-testing, I want to change the cap whenever I want, so that I can trigger compression in
a demo and switch it off for big tests.
Acceptance criteria:
- `/budget 1500`, `/budget off` and `/ctx 16384` (CLI) or the meter popover (web) change the settings, which apply to the next message.
- The values survive a restart; invalid values (for example a budget under 256) are rejected with a message.
- The effective cap never exceeds the window minus 512 tokens reserved for the reply.

**US-07 — See how full the context is** `[X1, M2]`
As a user of the web UI, I want a live view of the token usage, so that I know when the model will start forgetting.
Acceptance criteria:
- The top bar shows `used / cap` and a bar that changes colour at 75% and 90%.
- Each reply shows its generated tokens and duration.
- Replies stream token by token, and a Stop button is shown while streaming.

**US-08 — Manage my chats and prompts in the browser** `[X1]`
As a user, I want to organise conversations and reuse my favourite prompts, so that the web UI is a comfortable daily tool.
Acceptance criteria:
- A "···" menu on each conversation offers Rename, Fork, Export and Delete; a model selector lists the models pulled in Ollama.
- Saved prompts can be created, edited and deleted, and are inserted from a picker (button or `/` in the message box).
- If Ollama is unreachable the status shows "Ollama unavailable" instead of failing silently.

**US-09 — Explore an alternative by editing a message** `[X2]`
As a writer, I want to edit an earlier message and see a new answer without losing the original, so that I can compare directions.
Acceptance criteria:
- "Edit & resend" creates a new branch from that point; the original branch stays untouched.
- A `2 / 2` indicator with arrows switches between branches, showing each branch's own continuation.
- Forking a conversation (whole, or from one message) creates an independent copy.

**US-10 — Stop a generation and redirect it** `[X2]`
As a user, I want to stop a reply that goes in the wrong direction and give new instructions, so that I do not wait for it to finish.
Acceptance criteria:
- Stop ends the stream and the partial reply is saved, marked as stopped.
- My next message is a normal continuation: the model sees the partial reply in the history.
- If I stop before any token arrived, my message is removed so the history stays consistent.

**US-11 — Hand a conversation to someone else** `[X3]`
As a team member, I want to export a conversation as Markdown, so that I can paste it into a report.
Acceptance criteria:
- "Export as Markdown" downloads `<title>.md` with the branch on screen, my name and Sensai as speakers, and token info.
- Only the active branch is included; the thinking blocks can be added with `include_thinking=true` (API).
- Tool calls made during an answer appear as 🔧 lines.
(PDF and a public link are not implemented.)

**US-12 — Archive the full data** `[X3]`
As a researcher, I want a complete JSON export, so that I can analyse or restore everything.
Acceptance criteria:
- `format=json` contains every message of every branch with `parent_id`, the ids of the active path, token metrics and M2 summaries.
- Tool traces are included in the assistant messages.
- An unknown format or conversation returns a clear error (422 / 404).

**US-13 — Let the model use a calculator** `[T1]`
As a user, I want the model to call a tool for exact arithmetic, so that I get a correct number, not a guess.
Acceptance criteria:
- With `tools: true`, "what is (12+3)*4" shows a `[tool] calculate(...)` line and the answer 60.
- The tool never uses `eval` (only numbers and arithmetic operators are accepted).
- The tool calls are stored with the reply and replayed to the model on later turns.

**US-14 — Tools that fail must not break the chat** `[T1]`
As a user, I want a broken or unknown tool call to be reported to the model, so that the conversation continues.
Acceptance criteria:
- An unknown tool, a tool error or an unsafe expression produces `Error: …` as the tool result and the turn still ends with an answer.
- A model looping on tools is stopped after `max_tool_rounds` with an explicit message.
- A model that does not support tools still answers (the request is retried without tools).

**US-15 — The model asks me instead of guessing** `[X4, T1]`
As a user, I want the model to ask me a clarifying question with options, so that it does not invent missing information.
Acceptance criteria:
- A question from `ask_user` is shown with numbered options; I can answer with a number or free text.
- The answer is given back to the model, which then continues the same turn.
- Nothing is saved as a final reply until I answer.

**US-16 — A question can wait** `[X4]`
As a user who gets interrupted, I want a pending question to stay pending, so that I can come back later.
Acceptance criteria:
- A pending question is stored: the conversation endpoint returns it as `pending`, also after a server restart.
- Answering it resumes the turn exactly once (a second answer returns 404).
- Sending a new message instead abandons it: the old turn is saved as interrupted and the new one proceeds.

**US-17 — Approve before anything is written** `[A3]`
As a cautious user, I want to approve actions with side effects, so that the assistant never changes my files without my consent.
Acceptance criteria:
- `save_note` and `delete_file` show the tool name and the exact arguments and wait for "y" / approval.
- Nothing is written before the approval; after it, the note is appended to `notes_path`.
- Answering an approval request without an explicit `approved` true/false is refused (422).

**US-18 — Say no and let the model adapt** `[A3]`
As a user, I want to reject an action and have the model take it into account, so that I stay in control without ending the session.
Acceptance criteria:
- A rejection writes nothing and the model receives "The user rejected this action."
- The model then produces a normal answer in the same turn.
- Pressing Stop while waiting saves the turn as interrupted.

**US-19 — Change the assistant's role mid-conversation** `[A5]`
As a user, I want to switch persona without losing the conversation, so that one chat can go from planning to coding.
Acceptance criteria:
- `PATCH /api/conversations/{id}` with `{"persona": "coder"}` changes the system prompt for the next message and keeps the whole history.
- The persona's tool list restricts what the model may call, even if it requests another tool.
- An unknown persona is refused (422); `""` returns to the default assistant.

**US-20 — Add an agent with a text file** `[A5]`
As a developer, I want to define a new agent in YAML only, so that I can specialise the system without touching the code.
Acceptance criteria:
- A new `personas/<id>.yaml` (name, role, system prompt, tools, file permissions, optional model) appears in `GET /api/personas` without a restart.
- A file with a missing prompt or invalid fields is ignored instead of breaking the others.
- An id with path tricks (`../x`) is never loaded.

**US-21 — A team builds a project from a goal** `[A2, T5, A3]`
As a developer, I want to describe a goal and have a project manager plan it for a team of agents, so that the work is split and executed for me.
Acceptance criteria:
- `projects_cli "goal" --team pm,backend,frontend` shows the PM's plan (tasks, agents, dependencies, files) and waits for my approval; declining keeps the plan for `--resume`.
- After approval the tasks run in dependency order, each by its own persona in its own conversation, and a task sees the summaries of the tasks it depends on.
- The workspace contains the produced files and a `PROJECT.md` with the status of every task.

**US-22 — Recover from interruptions and failures** `[A2]`
As a user with a slow machine, I want to stop and resume a project, so that a long run is not lost.
Acceptance criteria:
- Closing the stream or a server restart pauses the project; `--resume ID` continues with the unfinished tasks only.
- An agent that forgets its expected files gets one follow-up; if it still fails, the task fails and its dependents are skipped.
- If Ollama is down the project is paused (not failed) and can be resumed; `cancel` stops a running project.

**US-23 — Each agent only touches its own folders** `[T4, A5]`
As a project owner, I want every agent restricted to its area, so that the frontend agent cannot rewrite the backend.
Acceptance criteria:
- The backend persona can write only `backend/**` and `docs/**`; writing elsewhere is refused before touching the disk.
- The refusal states where the agent may write, so the agent can correct itself.
- Entries an agent may not read are hidden from its directory listings.

**US-24 — Agents cannot escape the workspace or read secrets** `[T4]`
As a user, I want strong guarantees about files, so that a model mistake cannot damage my machine.
Acceptance criteria:
- Absolute paths, `..`, `~`, Windows drives and symlinks pointing outside the workspace are refused.
- `.env`, `.git` and key files are never readable, listable or writable, even by an agent allowed to read everything.
- `delete_file` always requires my approval; the project-files API applies the same rules.

**US-25 — The tester checks the code by running it** `[T2]`
As a developer, I want the tester agent to run the tests it writes, so that errors are caught and fixed automatically.
Acceptance criteria:
- `run_tests` runs pytest in the sandbox and returns the exit code, the output and the failing assertions.
- The tester sees the traceback, fixes its own test and runs again, or reports a bug in the code under test.
- Every run starts from a fresh copy of the project: only files written with `write_file` persist.

**US-26 — Runaway code cannot hurt the machine** `[T2]`
As a user, I want generated code to run in a strict sandbox, so that an infinite loop or a destructive script is harmless.
Acceptance criteria:
- An infinite loop is killed at `sandbox_timeout_s`; a huge allocation or huge output is stopped or truncated.
- `fork`, subprocesses, signals, sockets and `ctypes` are refused; the host's secrets and files outside the project copy cannot be read.
- Writing to the real project by absolute path is refused and the real files stay unchanged.

**US-27 — The plan is machine-readable and checked** `[T5, A2]`
As a developer, I want the PM to answer with a validated JSON plan, so that the orchestrator can execute it reliably.
Acceptance criteria:
- The planning call sends a JSON schema in which `agent` can only be a member of the team.
- The plan is validated: unknown agents, dependencies on later tasks, duplicate ids, too many tasks, invalid paths, and files outside the assigned agent's writable folders are all detected.
- The validated tasks are stored and can be edited by the user before approval (the edit is validated again).

**US-28 — A wrong plan is corrected, not executed** `[T5]`
As a user, I want an invalid plan to be sent back to the PM with precise feedback, so that I get a usable plan or a clear failure.
Acceptance criteria:
- Each invalid attempt is returned to the PM with the exact list of problems, up to `plan_retries` times.
- JSON wrapped in a code fence is accepted; non-JSON output counts as an invalid attempt.
- After the last retry the project is marked failed with a readable error and no task is executed.

**US-29 — Plug in an existing MCP server** `[T1]`
As a developer, I want to add a ready-made MCP server by editing `config.yaml`, so that my assistant gains its tools without writing code.
Acceptance criteria:
- A server listed under `mcp_servers` is started with the app (with `tools: true`) and its tools reach the model as `<server>__<tool>`, with the description and JSON schema the server provides.
- With the official `mcp-server-time`, asking the time in Paris makes the model call `time__get_current_time` and answer from the result.
- A server that cannot start or that crashes is reported (`[mcp] …` in the CLI, `GET /api/mcp`), a call to it fails with a readable error, and the app and the other servers keep working.

**US-30 — Write my own MCP tool** `[T1]`
As a developer, I want to expose my own functions as an MCP server, so that Sensai (or any MCP client) can use them.
Acceptance criteria:
- `src/mcp_servers/notes_server.py`, built on the small `base.serve` helper (no SDK), offers `count_words`, `list_notes` and `search_notes`; the model finds a saved note through `notes__search_notes`.
- A tool that raises an error returns an error result the model can read, and the server keeps running; a call that exceeds the timeout fails without breaking later calls.
- A tool not marked read-only waits for my approval before it runs (A3); `confirm: always|never` overrides this per server.
