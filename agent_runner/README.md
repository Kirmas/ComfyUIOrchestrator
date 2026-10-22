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

- `claude` -- the protocol described below; tool approvals use Allow / Deny.
  The only one with no shell in a project chat, and the only one a night
  report runs on.
  Context meter and `/compact` (`supports_compact=True`): the context in use is
  the last assistant call's whole input (`input_tokens + cache_creation +
  cache_read`; a subagent's calls, `parent_tool_use_id` set, don't count) and
  the window is the live `modelUsage[<model>].contextWindow` of the turn's
  `result` (1,000,000 for opus/sonnet/haiku on CLI 2.1.304 -- not 200k; the
  200k base default only shows before a chat's first turn). `/compact` is a CLI
  command that works headless as an ordinary message; its own `result` has zero
  usage, so the meter is reset from `system/compact_boundary.compact_metadata
  .post_tokens`. Verified end to end against the real CLI.
  Efforts are listed per model (`models[].efforts`; all three families take
  `low..max` + `ultracode`, probed -- the CLI itself downgrades an unsupported
  `xhigh`/`max` to `high`). Limits: `probe_limits` reads
  `GET https://api.anthropic.com/api/oauth/usage` (what `/usage` and the desktop
  app read) with the CLI's own OAuth token from `~/.claude/.credentials.json`:
  no model turn, so the shared scheduler polls it every minute like Codex's.
  `utilization` there is a percent; the promotional cloud-session credit is the
  block with a `limit_dollars` (its key is an internal codename, matched by
  shape) and shows as a `cloud_credits` window. The token is only read, never
  refreshed by this code (rotating it could log the CLI out); when it has
  expired (~8 h idle) the provider runs `claude auth status`, which makes the
  CLI renew its own login with no model call (verified on 2.1.304), and retries. Each turn's
  `rate_limit_event` still refreshes the plan windows too.
- `codex` -- `codex app-server --stdio`, bidirectional JSON-RPC. One server
  process per turn: initialize -> thread/start or thread/resume -> turn/start.
  Existing `codex exec` session ids resume through the same durable rollouts.
  Dev permission modes use both sandbox and approval settings:
  `ask` = workspace-write + on-request + user reviewer;
  `auto` = workspace-write + on-request + auto_review reviewer;
  `full` = danger-full-access + never. Retired Dev modes `workspace` and
  `readOnly`, missing modes and unknown values resolve to `ask`; additional
  rights therefore require a person's decision. Only ask/auto/full appear
  in the Dev picker. Mode changes apply to the next turn.
  Command/file/permission approval requests become Allow / Deny cards.
  Decisions are checked against the server's available choices. Approvals
  are one-off (permission grants last for the turn); no persistent rules are
  written and no session-wide approval is promised across process restarts.
  Automatic review is performed by Codex, not by the runner; any requests
  the server routes to the user are shown on the page.
  Structured questions ARE interactive: `item/tool/requestUserInput` becomes
  the existing question card, and the answer endpoint returns a JSON-RPC
  response keyed by Codex question ids. The separate `request_user_input_async`
  tool arrives as `item/completed` with `agentMessage.questions` (title and
  string options); these also become question cards instead of plain text.
  Async answers are sent as user input through `turn/start`, which can steer
  an active turn or start continuation after generation has ended. The server
  stays open while a question is unanswered or its answer RPC is in flight;
  a successful turn/completed alone must not cancel the card in those cases.
  Request ids include the turn and item ids so replies from older turns cannot
  resolve a new question. Stop terminates the process group and cancels cards.
  Unexpected server requests receive an explicit protocol error, never approval.
  Project chats force read-only + never + user reviewer regardless of saved
  dev mode; unexpected permission requests are denied without showing an
  escalation card. They use a separate `codex-home` per chat,
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
  Project chat: mode `project` -- like `edits`, except the hook bridge refuses
  `run_command`/`notebook_execution` and file writes outright instead of
  asking (they run as the runner's user, anywhere on disk; questions,
  `generate_image`, reads and MCP are unaffected). Hooks are Antigravity's documented mechanism for
  this (PreToolUse `decision: "deny"`), and they do fire under
  `--dangerously-skip-permissions` on this version. Runs in one fixed folder
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
  Each model advertises its own `efforts`: Sol/Astra offer `low`, `medium`,
  `high`, `xhigh`, `max`, `ultra`; Luna ends at `max`. These are the exact
  levels in the installed model catalog. The HTTP create/update paths and
  launch validation use the selected model's list too. Auto leaves the CLI
  default/config in effect.
- Fast sends `serviceTier="priority"`, off explicitly sends `"default"`.
  All three models advertise priority; availability remains backend-controlled.
- `account/rateLimits/updated` refreshes plan usage during turns. Refresh uses
  `account/rateLimits/read` on an app-server without starting any model turn:
  zero inference tokens. The shared scheduler polls this and Antigravity's
  free usage endpoint concurrently, then waits 60 seconds. Each provider's
  error is isolated so it cannot skip the other. Claude is not polled.
  Missing windows/errors preserve the last reading.
  `usedPercent / 100` becomes utilization; `windowDurationMins=300/10080`
  maps to `five_hour/seven_day`, otherwise `primary/secondary` is retained.
  Reset timestamps are Unix seconds; token usage is separate from plan usage.
- `supports_compact=True`: the existing `/compact` button sends a native
  `thread/compact/start` request after thread start/resume, never a model
  prompt saying `/compact`. The transport stays open after the immediate RPC
  acknowledgement and drains progress through `turn/completed`. It keeps
  the same thread id and reports normal failure/Stop events.
- `thread/tokenUsage/updated.tokenUsage.last.totalTokens` is the current
  context reading, including the post-compaction estimate (whose input/output
  fields can be zero). Lifetime `total.totalTokens` is not context occupancy.
  Both `context_tokens` and live `modelContextWindow` are persisted in meta
  and emitted on the result event. Windows are tied to the model that ran the
  turn, avoiding an old model's window after a model switch. Catalog defaults
  are 475,000 effective tokens for Sol and 258,400 for Astra/Luna; a server's
  live value takes precedence over those defaults. Older chats can backfill
  their latest token_count reading from a bounded tail of their own rollout.
- Generated images (`imageGeneration`, or the underlying
  `Extension/image_gen.generation`) are saved as chat attachments and emitted
  as `image` transcript events. The page renders an authenticated preview;
  opening it shows the original image. Base64 is never written to the SSE
  transcript. PNG/JPEG/GIF/WebP output is recognized from its bytes, with a
  separate 30 MB generated-output limit. Duplicate content is emitted once.
  `savedPath` from an event is not read as an arbitrary filesystem path.
- At runner startup, `recover_outputs` imports existing images only from the
  chat's own `generated_images/<session UUID>` folder, including the isolated
  project's Codex home. Symlinks and unrelated thread folders are skipped.
  Thus generations made before image support appear after restarting the
  runner without another generation request. Existing attachment routes and
  their token authentication serve the files.
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

- **Pictures** (`Profile.accepts_images`): the page shrinks a
  pasted / dropped / picked image to <=1568px and posts it base64 with the
  message; the runner keeps it in `chats/<id>/attachments/` (for the
  transcript, `GET /chats/<id>/attachments/<name>`) and sends it to the CLI as
  an `image` content block (or read_file instruction in Antigravity), like a paste in a terminal.
  Both dev and project chats accept pictures.

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

Approval transport validation: CLI 0.163.0 accepted both `user` and
`auto_review` reviewers in read-only thread/start probes without model calls.
A real read-only `untrusted` test produced a command approval, which was
routed through the runner's HTTP decision endpoint and intentionally denied;
the command was not executed. Allow responses, permission scopes, fixed-mode
rejection, legacy defaults and restricted choices are covered by unit tests.

Native compaction was checked with a scratch read-only Codex thread through
`runner.run_turn`: 10,009 -> 2,022 context tokens, same thread id, and the
following turn remembered the chosen color. No API key or paid polling turn
is involved in limits refresh. Compaction itself is a native model operation,
not a claim of zero inference usage.

Async-question regression: the exact `agentMessage.questions` format emitted
by `request_user_input_async` is tested independently from the JSON-RPC
`item/tool/requestUserInput` format. Real read-only runner tests confirmed
both immediate HTTP answers during generation and delayed answers after the
model finished; the server kept the card alive and the model received Blue.

Image validation includes replaying both image event shapes, invalid outputs,
deduplication and isolated startup recovery. The actual previously generated
robot PNG was recovered through a scratch chat and served byte-for-byte by
the attachment endpoint (2,415,221 bytes), with no model call.

## Show media in the chat (all providers)

### Import chat pictures into the graph

`list_chat_attachments(chat_id)` returns original attachment names, sizes,
MIME types and `source` (`user` or `generated`). It also discovers generated
images in the current provider session. `import_chat_attachment(chat_id,
name, node_id)` copies one into an existing asset node through the ordinary
node upload implementation, retaining its validation and original bytes.
For a sub-dashboard, use an asset node inside it, then set its result.

The API fetches bytes from the authenticated runner attachment route; it
never opens a private agent path. Names are opaque 32-hex filenames, paths
and symlinks are rejected. No shell, base64 in model context, extra filesystem
permissions or provider-specific upload flow is needed. User messages currently
accept image attachments; this does not add a general file-upload UI.

`show_in_chat(chat_id, asset_id, caption="")` is an Orchestrator MCP tool for
Claude, Codex and Antigravity, in both project and Dev chats. The current chat
id is injected into each profile; resumed prompt-based sessions also receive
it. Use `get_candidates`, recipes or board reads to find the asset id, then
call `show_in_chat` when the person asks to see it. `get_candidates` itself
shows images to the model; this new tool publishes to the human transcript.

The tool calls `POST /api/agent-chats/<chat>/media`. The API validates the
asset and its file, then sends metadata to `POST /chats/<chat>/media` on the
runner. The runner emits a provider-neutral `media` event containing only the
asset UUID, MIME type, name and caption: no base64, local path or token.
There is no provider-specific tool-result parsing and no duplicate Asset row.
The browser uses authenticated asset URLs; images are previews, video/audio
have players with the original asset endpoint's byte-range support, and all
formats have a download link. Unknown formats (including meshes) are files.

Previews never replace originals: asset previews use `/fit`, while opening
or downloading uses `/file`. `/file?download=true` sets Content-Disposition
and streams the exact original payload after its storage prefix. Generated
chat images similarly use `attachments/<name>?preview=true` for a 768px
rendition, and the normal attachment route for opening or `?download=true`
for saving. Even if both preview/download flags are passed, download wins.

Dev sessions also receive `ORCH_CHAT_ID`, `ORCH_API_URL`, `ORCH_API_TOKEN` in
the CLI environment. If their personal CLI has no orchestrator MCP connection,
they can use the same media API with those credentials (never print the
API token). Locally generated files can first be uploaded to the existing
`/api/assets/upload` multipart route and then published by their asset id.
Project chats use MCP and do not need shell/filesystem permission expansion.

Codex project chats additionally allow the built-in `image_gen` tool for
requested image generation/editing (`features.image_generation=true`). This
does not enable apps/plugins or change the read-only sandbox and fixed
approval policy. Generated output uses the same automatic chat attachments
as Dev chats; no external image API or API key is required by this path.
Existing project sessions receive changed developer instructions through
`thread/inject_items` before their next turn, once per instruction digest.
This CLI retains its original developer message when `thread/resume` is
given new `developerInstructions`; a runner restart alone cannot update it.

Integration checks (temporary files and in-process HTTP, no production DB):

    backend/.venv/bin/python scripts/test_chat_media.py

Covers MCP publishing for all three provider ids, missing files/invalid ids,
full-size downloads byte-for-byte, generated previews versus originals, and
range requests for audio/video/other assets.
