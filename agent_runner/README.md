# Agent runner

Hosts agent-CLI chat sessions (Claude Code, Codex, Antigravity) for the
orchestrator's **Agent** tab.
The orchestrator (`backend/app/api/routes/agent_chats.py`) only proxies to it.

## Why a separate process

- `systemctl restart comfy-orchestrator-api` kills that unit's whole cgroup, so
  an agent spawned by it would die on every deploy -- including one it started.
- The orchestrator runs as `orchestrator` in `/opt`; the runner runs as
  `keresh` (own Claude login, own files) and listens on `127.0.0.1:8765` only.
- A chat survives the orchestrator restarting: the browser's `EventSource`
  reconnects through the proxy with `Last-Event-ID` and continues from the
  runner's per-chat event sequence.

## Providers (`providers.py`)

One `Provider` class per CLI -- the runner owns what is the same (transcript,
status, Stop, SSE, one process per turn), the class owns what differs: argv,
how the message reaches the process, how its output lines become transcript
events, and the model / permission-mode lists the page shows. A profile says
what a chat needs in neutral terms (`TurnSpec`), the provider turns it into
flags. A new CLI is one new class + a line in `PROVIDERS`.

- `claude` -- the protocol described below; the only one that can ask the page
  (Allow / Deny), the only one with no shell in a project chat, and the only
  one a night report runs on.
- `codex` -- `codex app-server --stdio`, bidirectional JSON-RPC. One server
  process per turn: initialize -> thread/start or thread/resume -> turn/start.
  Existing `codex exec` session ids resume through the same durable rollouts.
  Tool permission modes remain fixed (`workspace` / `full` / `readOnly`),
  `approvalPolicy="never"`; changing mode applies to the next turn.
  Structured questions ARE interactive: `item/tool/requestUserInput` becomes
  the existing question card, and the answer endpoint returns a JSON-RPC
  response keyed by Codex question ids. Request ids include the turn id so
  replies from older turns cannot resolve a new question. stdin stays open
  until turn/completed; Stop terminates the process group and cancels cards.
  Unexpected server requests receive an explicit protocol error, never approval.
  Project chats force read-only and use a separate `codex-home` per chat,
  sharing auth, models cache and sessions via symlinks, but no personal config
  or plugins/apps. Only the orchestrator MCP is configured (token in env
  `ORCH_MCP_TOKEN`, tool approval `approve`). Read-only shell still exists;
  this is not Claude's MCP-only tool isolation.
- `agy` -- `agy -p=<prompt> --output-format stream-json [--conversation <id>]`.
  Interactive questions (`ask_question`) and tool execution approvals are
  handled live via a `PreToolUse` lifecycle hook bridge (`comfy_hook_bridge.py`
  registered in `~/.gemini/config/hooks.json`). When the agent asks a structured
  question or requires permission for shell commands in `edits` mode, the hook
  routes the request to the runner's IPC endpoint (`/hooks/pre-tool`), rendering
  the interactive question card or Allow / Deny buttons in the web UI.
  Permission modes: `edits` (auto-allows file changes; asks on commands),
  `auto`, `full` (approves tools without prompts; asks on questions), and
  `plan` (read-only planning). Images are accepted and passed as attachment
  file paths with instructions to inspect them via `read_file`. Quotas are
  tracked per model, polled automatically in the background every 60 seconds
  via `/usage` slash command (0 tokens), and probeable on-demand.
  Project chat: mode `edits`, run in one fixed folder
  (`<data dir>/agy-project`, MCP from its `.agents/mcp_config.json`). **Two
  one-time entries in `~/.gemini/antigravity-cli/settings.json` are the
  person's to add** (the runner won't edit an agent's permission settings
  itself; it refuses the turn and says what is missing): that folder under
  `trustedWorkspaces`, and `"mcp(orchestrator/*)"` in `permissions.allow`.

Per-chat knobs and limits are part of the same interface, opt-in per class:
`efforts` (the CLI's reasoning levels; Claude `--effort`), `supports_fast` +
`fast_allowed(model)` (Claude: `--settings '{"fastMode":true}'`, Opus only),
and `record_limits` / `probe_limits` feeding `LimitsStore`
(`<data dir>/limits.json`, `GET /limits`, `POST /limits/<provider>`). Claude
reports plan usage in its stream (`rate_limit_event.unifiedWindows`:
`five_hour` / `seven_day`, utilization 0..1), so every turn refreshes it and
`probe_limits` is a 1-token Haiku turn.

Antigravity:

- Effort is passed as `--effort <level>` (`low`, `medium`, `high`, `xhigh`, `max`).
  When an effort level is chosen, the model id's built-in suffix (`-low`/`-medium`/`-high`)
  is stripped for `--model` so `agy` does not reject it as conflicting.
- Fast mode is not supported (`supports_fast = False`): agy has no fast mode
  flag (speed is determined by model selection like Flash vs Argon; `/fast` was
  removed from agy).
- Limits: `probe_limits` queries the `agy -p=/usage` slash command in plan mode
  (0 inference tokens), parsing bucket remaining fractions into utilization and
  ISO reset timestamps into epoch seconds. If a turn fails with a quota error
  (429 / RESOURCE_EXHAUSTED / `Resets in ...`), `record_quota_error` records that
  model window with utilization 1.0 and the parsed reset time.

Codex (verified with CLI 0.163.0):

- Effort is passed as `config.model_reasoning_effort` on thread start/resume.
  The picker offers `low`, `medium`, `high`, `xhigh`, `max`: common catalog
  levels for Sol, Astra and Luna. Auto leaves the CLI default/config in effect.
- Fast sends `serviceTier="priority"`, off explicitly sends `"default"`.
  All three models advertise priority; availability remains backend-controlled.
- `account/rateLimits/updated` refreshes plan usage during turns. Refresh uses
  `account/rateLimits/read` on an app-server without starting any model turn:
  zero inference tokens. Missing windows/errors preserve the last reading.
  `usedPercent / 100` becomes utilization; `windowDurationMins=300/10080`
  maps to `five_hour/seven_day`, otherwise `primary/secondary` is retained.
  Reset timestamps are Unix seconds; token usage is separate from plan usage.
- The shared runner delegates protocol replies to `Provider.answer_request`;
  Claude's original control_response format is unchanged. Question cards and
  HTTP answer routes are reused, not parsed out of assistant text.

References: [configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[speed](https://learn.chatgpt.com/docs/agent-configuration/speed),
[app-server](https://learn.chatgpt.com/docs/app-server).

Codex receives the profile through `developerInstructions` on every start/resume.
Antigravity receives it in the first message. Session ids are stored in meta;
Codex saves the id only after thread start/resume succeeds.
Binaries are found via `CLAUDE_BIN` / `CODEX_BIN` / `AGY_BIN`, else PATH, else
`~/.local/bin`. Dev chats share one working tree whatever their provider, so
still only one runs at a time.

## How a chat works

- Each turn is one `claude -p --resume <session>` process with
  `--output-format stream-json`. The CLI's own session file is the durable
  conversation; a runner restart only cuts the turn in flight (marked
  `interrupted`), the next message resumes with full context.
- `~/.local/share/comfy-agent-runner/chats/<id>/`: `meta.json` (600, holds the
  MCP token), `events.jsonl` (the normalized transcript the page shows),
  `work/` (the CLI's cwd -- sessions are keyed by cwd, so it must stay put).
- The CLI is driven in stream-json both ways (the Agent SDK protocol): the
  message goes in on stdin and stdin stays open for the turn, so the CLI can
  send `control_request`/`can_use_tool` before a call its rules don't allow.
  The runner parks it as a `permission_request` event (status
  `awaiting_approval`) and answers with the person's pick from the page:
  allow / allow for this chat (the CLI's own suggestions with
  `destination: "session"` -- never written to a settings file) / deny. A turn
  can wait on that indefinitely.
- One `Profile` class per kind of chat:
  - `project` -- `--tools ""` (no shell/files), `--setting-sources ""`,
    `--strict-mcp-config` with the orchestrator's own `/mcp`,
    `--allowedTools mcp__orchestrator`, `--permission-prompts none`.
  - `dev` -- cwd = the dev copy (`RUNNER_DEV_REPO`, default
    `~/comfy-orchestrator`), the user's own settings/CLAUDE.md/memory/MCP load
    as in a terminal, `--permission-mode auto` (the same safety classifier the
    person runs their own unattended terminal sessions under; what it wants
    confirmed comes to the page). Only one dev chat may run at a time: they
    share the working tree. **Opt-in:** `RUNNER_DEV_ENABLED=1` in the env file. It is a shell as
    keresh reachable by anyone holding the orchestrator's API token, so that
    token has to be a real random secret before this is switched on.

- **Pictures** (dev chats only, `Profile.accepts_images`): the page shrinks a
  pasted / dropped / picked image to <=1568px and posts it base64 with the
  message; the runner keeps it in `chats/<id>/attachments/` (for the
  transcript, `GET /chats/<id>/attachments/<name>`) and sends it to the CLI as
  an `image` content block, like a paste in a terminal. Project chats refuse
  them: their world is the project, which MCP already reaches.

## Night mode (`night.py`)

Other agents file bugs in the app itself through the MCP tool `report_bug`
(and read the answer with `get_bug_report`) -- only while the person has
switched it on in the Agent tab. Each report becomes a dev chat with
`origin: "agent"`, worked one at a time, unattended:

1. First report of the night: `deploy.sh backup`, then `git stash -u` of any
   work in progress (so none of it reaches prod). Starts only from `main`.
2. The agent investigates with an extra system prompt: the report is data from
   another agent, not a person; decide bug / not-a-bug / agent-misuse /
   feature-request / unsure; refuse with reasons and advice; fix only real
   bugs, minimally, never schema or track ordering. Anything the classifier
   wants approved is denied (nobody's there). Git and deploy commands are
   `--disallowedTools` for it; $10 and 60 minutes per report.
3. The runner, not the agent: if the verdict is `bug-fixed` and files changed
   -> backend import + `tsc -b` -> create `night/<session>` on the night's
   first fix -> `deploy.sh` -> health probes (`/api/health`, `/api/projects`,
   `/`) -> healthy: commit on the night branch; unhealthy: discard the change
   and redeploy HEAD. The fix is committed only after prod came up with it,
   so a rollback is never a revert. If even that redeploy is unhealthy, night
   mode switches itself off.
4. Morning, in the Agent tab ("Night mode"), for the whole night at once:
   **accept** fast-forwards main to the night branch (prod already runs it);
   **reject** goes back to main and redeploys it (branch kept). Either way the
   stash comes back last (a conflicting stash stays in `git stash list`), and
   night mode switches off. No partial accept, by design.

State: `night.json` in the data dir (switch, open session, last 20 nights).
Refused on purpose: a report while the dev copy has someone else's
uncommitted changes, or isn't on the branch the session left it on.

## Models

`MODELS` in `runner.py` is the one list (served at `/models`, shown as the
picker in the chat header). It holds model *families* as CLI aliases
(`opus`/`sonnet`/`haiku`), which the CLI resolves to the newest model of that
family -- a CLI update brings new versions with no change here. The resolved id
the CLI reports is shown on hover. The pick is per chat and applies from the next
turn (`--model` on the `--resume`), so switching mid-chat keeps the context;
the switch is also written into the transcript. Fable is left out: on this
account it needs separate usage credits and every turn would fail.

## Install / operate

    agent_runner/install.sh <prod-API_TOKEN>,<dev-API_TOKEN>   # idempotent, keeps other env lines
    echo RUNNER_DEV_ENABLED=1 >> ~/.config/comfy-agent-runner/env  # enable dev chats
    sudo loginctl enable-linger keresh                         # once, or it dies on logout
    systemctl --user status|restart comfy-agent-runner
    journalctl --user -u comfy-agent-runner

Changing `runner.py` needs `systemctl --user restart comfy-agent-runner`
(deploy.sh does not touch it); that interrupts any running turn.

Protocol regression checks (no model calls):

    backend/.venv/bin/python -m unittest discover -s agent_runner/tests
    backend/.venv/bin/python -m pyflakes agent_runner/*.py agent_runner/tests/*.py

The app-server migration was also tested against CLI 0.163.0 with real
read-only turns: question -> HTTP answer endpoint -> model continuation,
resume remembering the answer, Stop while a question is pending, and resume
of a pre-migration `codex exec` rollout. Limits refresh was tested without
starting a model turn.
