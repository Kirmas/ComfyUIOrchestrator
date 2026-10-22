# Agent handoff — stage 2 brief (limits)

Read first: `roadmap.md` §3 (bullets on limits/pace/scarce), `docs/agent-handoff/stage1-brief.md`
(what already exists), `agent_runner/handoff.py`, and `agent_runner/README.md`.

## Goal

`list_handoff_targets` stops offering models that are out of limits or burning their
limits too fast, and marks moderately-fast ones **scarce**, which always need the
person's Yes/No. All of it is computed by the runner from data it already stores —
no new probes, no token cost, no judgement left to the agent.

Out of scope: learned capabilities (stage 3), any UI for editing thresholds (JSON
config only), changing how limits are polled.

## The data you work from (real, from `~/.local/share/comfy-agent-runner/limits.json`)

`providers.limits.data` (`LimitsStore`) holds, per provider id:

```json
"claude": {"status": "allowed", "updated_at": 1792658074.0,
  "windows": [
    {"id": "five_hour",     "utilization": 0.09, "resets_at": 1792675200},
    {"id": "seven_day",     "utilization": 0.66, "resets_at": 1792746000},
    {"id": "cloud_credits", "label": "Cloud credits: $0 of $100 used", "utilization": 0.0, "resets_at": 1793865540}]},
"codex": {"windows": [
    {"id": "five_hour", "utilization": 0.0,  "resets_at": 1792676021},
    {"id": "seven_day", "utilization": 0.38, "resets_at": 1793095844}]},
"agy": {"windows": [
    {"id": "gemini-weekly", "group": "Gemini Models", "utilization": 0.2575, "resets_at": 1792884365, "disabled": false},
    {"id": "gemini-5h",     "group": "Gemini Models", "utilization": 0.0178, "resets_at": 1792671820, "disabled": false},
    {"id": "3p-weekly",     "group": "Claude and GPT models", "utilization": 1.0, "resets_at": 1792700717, "disabled": false},
    {"id": "3p-5h",         "group": "Claude and GPT models", "utilization": 0.0, "resets_at": null, "disabled": true},
    {"id": "gemini-argon-wtu-weekly", "group": "Gemini 4 Argon", "utilization": 1.0, "resets_at": 1792864443, "disabled": false}]}
```

Facts to respect:
- `utilization` is 0..1, may be `null`; `resets_at` is epoch seconds, may be `null`.
- A window with `disabled: true` doesn't apply. Ignore it.
- `cloud_credits` (Claude) is a dollar allowance, not a rate window. Ignore it (config
  `ignore_windows`).
- Claude and Codex: **one pool per provider** — every model of that provider eats all
  of its windows.
- Antigravity: windows carry a `group`; **which model eats which group is NOT in the
  data**. It comes from config `pools` (below). In addition, `AgyProvider.record_quota_error`
  may append a window whose `id` is a **model id** (e.g. `"gemini-3.8-flash"`),
  utilization 1.0, no `group`: that window applies to that model only.
- A window whose `resets_at` is in the past has reset: treat its utilization as 0.
- No data for a provider at all → nothing is known → the target is `ok`.

## Pool and pace

For a target, its **windows** are:
- claude/codex: all the provider's windows, minus disabled and `ignore_windows`;
- agy: windows whose `group == config.pools[target]`, plus any window whose
  `id == <model id>`; minus disabled. A target missing from `pools` gets only the
  model-id windows (so it is never hidden by a group it may not belong to).

For each window compute a state, then the target's state is the **worst** of them
(`hidden` > `scarce` > `ok`):
- `used = utilization` (0 if reset or null-with-past-reset; skip the window if
  utilization is null and it hasn't reset).
- `used >= 1.0` → `hidden`.
- `duration` = window length in seconds (see below). If unknown or `resets_at` is null →
  only the `used >= 1.0` rule applies.
- `elapsed = clamp(1 - (resets_at - now) / duration, 0, 1)`; `ahead = used - elapsed`.
- `ahead >= hide_margin` → `hidden`; `ahead >= scarce_margin` → `scarce`; else `ok`.

Worked example from the person: 5-hour window, 80% used, 3 h left → elapsed 0.4,
ahead 0.4 → hidden with the default `hide_margin` 0.35.

Window duration: config `durations` by window id wins; otherwise infer from the id —
contains `five_hour` or `5h` → 18000; contains `seven_day` or `weekly` → 604800;
anything else → unknown.

Return, along with the state, the numbers of the window that decided it
(`window_id`, `label` if any, `used`, `resets_at`) — the scarce question shows them.

## Config additions (`handoff.json`, all optional, with these defaults)

```json
"limits": {
  "hide_margin": 0.35,
  "scarce_margin": 0.15,
  "ignore_windows": ["cloud_credits"],
  "durations": {},
  "pools": {
    "agy:gemini-3.8-flash": "Gemini Models",
    "agy:gemini-4-argon": "Gemini 4 Argon",
    "agy:claude-sonnet-5-5": "Claude and GPT models",
    "agy:claude-opus-5-5": "Claude and GPT models",
    "agy:gpt-oss-120b": "Claude and GPT models"
  }
}
```

Load/save it the same way as the existing fields of `HandoffConfig` (unknown keys
tolerated, defaults when absent).

## Where it plugs in

1. A pure function in `handoff.py`, e.g.
   `limit_state(target, limits_data, config, now) -> (state, detail)`.
   Pure = no I/O, `now` passed in, so tests don't depend on the clock.
2. `get_handoff_targets` — at the existing "Stage 2 hook point": `hidden` → skip the
   target entirely (no reason, no "back at": the agent sees nothing). `scarce` → keep it,
   force `mode = "ask"` even if the rule says allow, and add `"scarce": true` and
   `"note": "limits are being spent fast; prefer another target"`. `ok` → unchanged.
3. `request_handoff_route` — already refuses targets not in the list, so hidden ones
   are refused for free. For a scarce target store `scarce: true` and the window detail
   in `pending_handoff`, and put both in the `handoff_request` event.
4. `decide_handoff_route` — for a scarce pending request only `allow_once` / `deny_once`
   are valid; `*_always` → 400 **before** claiming the request (so the card stays usable).
   On allow, **re-check** the target's state: if it is now `hidden`, don't hand off —
   treat it like the existing failure path (error event in the chat, request not left
   pending), with a message that the limit ran out meanwhile.
5. MCP tool docstring of `list_handoff_targets`: one sentence that a target marked
   scarce needs the person's approval every time and should be avoided when another fits.

## Frontend (`AgentChat.tsx`)

For a `handoff_request` with `scarce: true`: show a line with the numbers, e.g.
"Gemini Models: 65% of the 5-hour window used, resets 15:20 — OK?", and only two
buttons (Yes = `allow_once`, No = `deny_once`). Reuse the existing card; add the
strings to `en.ts` and `uk.ts`.

## Tests (`agent_runner/tests/test_handoff.py`)

Pure-function tests with a fixed `now`:
- exhausted window → hidden; disabled exhausted window → ignored;
- the person's example (80% used, 3 h of 5 h left) → hidden; a moderate case → scarce;
  an on-pace case → ok;
- reset window (resets_at in the past) → ok;
- `cloud_credits` ignored;
- agy: Flash unaffected by an exhausted "Claude and GPT models" group; Sonnet-in-agy hidden
  by it; a model-id quota window hides only that model; a target missing from `pools`
  isn't hidden by any group;
- no provider data → ok; unknown window id (no duration) → only the 100% rule.
Route tests:
- scarce target is listed with `mode: "ask"` even with an allow rule;
- `allow_always` on a scarce request → 400 and the request is still pending;
- target becomes hidden between request and approval → no handoff, error event,
  nothing pending.

## Rules for you

- Do not commit, push, create branches, deploy, or restart any service.
- Do not change how limits are polled or stored, and don't touch other providers' code.
- No duplication: one function computes the state; the list, the request and the
  re-check all call it.
- Run `agent_runner/.venv/bin/python -m unittest discover -s agent_runner/tests` and
  `cd frontend && npm run build` (check the exit code; don't pipe it).
- If something here contradicts the code or the data, stop and ask one short question.
- Finish with a short report in Ukrainian: what you built, files touched, test results,
  anything skipped or uncertain.
