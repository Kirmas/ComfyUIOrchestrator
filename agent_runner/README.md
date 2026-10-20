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
- `codex` -- `codex exec [resume <thread>] --json`, prompt on stdin. Headless:
  the permission mode *is* the sandbox (`workspace` / `full` / `readOnly`, as
  `-c sandbox_mode=...` + `approval_policy="never"`). Project chat: read-only
  sandbox, `--ignore-user-config`, the orchestrator's MCP via `-c
  mcp_servers.orchestrator.*` (token in env `ORCH_MCP_TOKEN`, tool approval
  `approve`). The shell exists but can only read.
- `agy` -- `agy -p=<prompt> --output-format stream-json [--conversation <id>]`.
  Headless, a tool that needs permission is auto-denied (the final `result`
  lists it as `denied_actions`; the transcript says so), so modes are
  `edits` / `full` (`--dangerously-skip-permissions`) / `plan`. Quotas are per
  model; a quota error ends the turn as `error` with the CLI's message.
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
`probe_limits` is a 1-token Haiku turn. Codex and Antigravity don't implement
any of the three yet; the page shows each control only when `describe()` says
the provider has it.

The CLIs have no system-prompt flag, so for codex/agy the dev prompt (plus
"read CLAUDE.md") rides in the conversation's first message. Their session id
is reported by the CLI on the first turn (`thread.started` / `init`) and
stored as `session_id`; a turn that fails before that starts a fresh one.
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
