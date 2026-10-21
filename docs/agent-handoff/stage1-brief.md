# Agent handoff — stage 1 brief (core)

Read first: `roadmap.md` §3 (the why and the agreed rules), `CLAUDE.md` (Agent chats
section), `agent_runner/README.md`. This brief is the *what* for stage 1 only.

## Goal

A project-chat agent can hand its task to another provider+model, one way. The runner
enforces the policy; the person approves via a card in the chat; the source chat freezes;
the new chat starts immediately with a system-assembled first message.

**Out of scope for stage 1** (do not build, do not stub elaborately): limits/pace/scarce
(stage 2), learned capabilities (stage 3), dev chats, night mode. Leave a single obvious
hook point where stage 2 will filter targets, nothing more.

## Terms

- **target** = `"<provider>:<model>"`, e.g. `"claude:opus"`, `"agy:gemini-3.8-flash"`,
  `"codex:gpt-6-astra"`. Valid only if the provider is available, `supports_project`, and
  the model is in its `model_ids` (`agent_runner/providers.py`).
- **identity** = the underlying model regardless of CLI. `claude:opus` and
  `agy:claude-opus-5-5` are the same identity. The mapping target → identity lives in the
  config (below); a target missing from the map is its own identity.
- **chain** = list of identities the task has passed through, oldest first, including the
  current chat's.

**Identity is used for the chain ban only.** Rules (`allow`/`deny`, the popup, the
settings list) are always per *target*: denying `claude:opus` must never deny
`agy:claude-opus-5-5` — the person may refuse a target because of that provider's
limits, not because of the model. An empty `identities` map makes the chain ban
per-target too.

## Config (runner data dir, e.g. `DATA_DIR / "handoff.json"`)

```json
{
  "timeout_minutes": 5,
  "user_messages_max": 20,
  "identities": {"claude:opus": "claude-opus", "agy:claude-opus-5-5": "claude-opus",
                 "claude:sonnet": "claude-sonnet", "agy:claude-sonnet-5-5": "claude-sonnet"},
  "default_effort": {"*": "low"},
  "rules": {"agy:gemini-3.8-flash -> codex:gpt-6-astra": "allow"}
}
```

`rules` values: `"allow"` | `"deny"`; absent = ask. Write atomically (tmp + replace, like
`LimitsStore.update`). Effort: `default_effort[target]`, else `["*"]`; if the model doesn't
support that effort (`provider.efforts_for`), use the provider default.

## Runner (`agent_runner/runner.py`)

1. **Refactor `create_chat`** so its body (validation + meta + dir creation) is a plain
   function reused by the HTTP route and by handoff. Do not copy it.
2. `GET /chats/{id}/handoff-targets` → only targets this chat may request now:
   excludes the chat's own target, every target whose identity is in the chain, targets
   with a `deny` rule from this chat's target, targets this chat already got a no for
   (`meta["handoff_refused"]`), unavailable/non-project providers. Each item:
   `{"target", "label", "mode": "auto"|"ask"}`. Nothing else (no reasons for hidden ones).
   Put the filter in one function; stage 2 will add to it.
3. `POST /chats/{id}/handoff` with `{target, task, reason, done_so_far, focus}`:
   - Refuse (400, short message) if: chat kind ≠ project; chat already frozen; a handoff
     request is already pending; target not in the list from (2); field caps exceeded —
     `task` ≤ 4000 chars, `reason` ≤ 300, `done_so_far` ≤ 2000, `focus` ≤ 20 ids.
   - `mode == "auto"` → perform the handoff now, return `{"status": "started", "chat_id"}`.
   - `mode == "ask"` → emit a `handoff_request` event (request_id, target label, reason,
     task preview) in the source chat, store the pending request in `meta` (so it is
     visible after reload), return `{"status": "awaiting_person"}` **immediately**, and
     then **stop the source chat's turn** (`stop_chat`) so the agent does not keep working
     while the person decides. Never block the MCP call while waiting: MCP clients have
     tool-call timeouts, and the handoff is one-way, so the source agent doesn't need the
     answer inside that call.
4. `POST /chats/{id}/handoffs/{request_id}` with
   `{"decision": "allow_once"|"allow_always"|"deny_once"|"deny_always"}`:
   - `*_always` writes the rule `"<from> -> <to>"` to the config.
   - allow → perform the handoff. deny → emit `handoff_declined`, add target to
     `meta["handoff_refused"]`.
   - Timeout (`timeout_minutes`, an asyncio task) → same as `deny_once` but marked
     `"timeout": true`; config untouched. On runner restart, a pending request is treated
     as timed out.
   - After any decline (including timeout), the runner **itself starts a new turn** in the
     source chat with a system message (a `user` event with `"origin": "system"`):
     `[System] The person did not approve handing this task to <label>` + ` (no answer in
     time)` for a timeout + `. Continue the task yourself. You cannot request <label>
     again in this chat.` Do not wait for the person's next message.
5. **Perform the handoff:**
   - If the source chat has a turn running, stop it (`stop_chat`).
   - Create the new chat via the shared function from (1): same `project_id`,
     `project_name`, `mcp_url`, `mcp_token`, `api_url`, `api_token`; target's provider and
     model; effort from config; title = source title + " →".
   - New meta `handoff_from = {chat_id, target, reason, chain}`; source meta
     `handed_off_to = {chat_id, target, reason, at}`. Emit `handoff_out` in the source
     (link data) — the page shows it frozen.
   - `post_message` on a chat with `handed_off_to` → 409 "This chat was handed off to …".
   - Build the first message (below), emit it as a `user` event with
     `"origin": "handoff"`, and start `run_turn` immediately.

## First message template (English, assembled by the runner)

```
[Handoff] This task was handed to you by <source label>. Chain: A → B → you.
You may not hand it back to: <labels of chain identities>.
Project: <name> (<id>). Previous chat: <source chat id>.
Reply to the person in the language of their messages below.

## Task
<task>

## Why you
<reason>

## Already done
<done_so_far or "nothing">

## Where to work
<focus ids, each validated against the API; unknown ids dropped with a note>

## The person's own messages (source of truth; if the task above contradicts them, follow these or ask)
<last N `user` events of the source chat with origin != handoff, verbatim, oldest first>

## Attachments in the previous chat
<names, or "none">. Import with import_chat_attachment(chat_id="<source chat id>", ...).

If you are not sure what the person wants, ask one short question instead of guessing.
```

Validate `focus` ids by GET against the orchestrator API (`api_url`, `api_token` from
meta) — the runner already talks to it for push notifications.

## Backend (`backend/app/api/routes/agent_chats.py`, `backend/app/mcp/tools.py`)

- Proxy routes: `GET /api/agent-chats/{id}/handoff-targets`,
  `POST /api/agent-chats/{id}/handoff`, `POST /api/agent-chats/{id}/handoffs/{req}`,
  plus `GET/PUT` for the rules (settings UI). Same `_call` pattern as the existing ones.
- MCP tools `list_handoff_targets(chat_id)` and
  `hand_off(chat_id, target, task, reason, done_so_far="", focus=[])`. Docstrings: use only
  when another model fits the task clearly better (cheaper for simple work, or can do
  what you can't); write `task`/`reason`/`done_so_far` in English, concise, self-contained;
  on `awaiting_person` end your turn with one short line saying you asked; you can't
  hand back to anyone in the chain. Validate `chat_id` like `show_in_chat` does.

## Frontend (`frontend/src/components/AgentChat.tsx`)

- `handoff_request`: card with the four buttons + countdown; reuse the existing
  permission-card styling, not a new component family.
- `handoff_declined`, `handoff_out` (banner "Handed off to X — open", input disabled),
  `user` with `origin: "handoff"` rendered as a collapsible "Handed off from X" card.
- Settings: list of rules with a dropdown allow/deny/remove. Minimal.

## Rules for you

- Do not commit, deploy, or restart `comfy-agent-runner` (the person does that).
- Do not touch Claude's `ask_permission` path or any agent's own permission settings.
- No duplication: reuse `create_chat`'s logic, the existing proxy `_call`, the existing
  card styles.
- Tests in `agent_runner/tests/` for: chain ban by identity across providers, deny rule,
  re-ask blocked after a no, timeout = soft no without config change, frozen chat 409,
  field caps, first-message assembly (verbatim user messages, last N).
- Run the runner tests, `backend/.venv/bin/python -m pytest` if relevant, and
  `cd frontend && npm run build` — check its exit code, never pipe it through `tail`.
- Finish with a short report: what was built, files touched, what you did not do or
  were unsure about.
