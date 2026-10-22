# Roadmap

Captures a design brainstorm (2026-07-27, recovered from a lost claude.ai session) about
where this project goes next. Not a spec to implement literally — a record of decisions
and open questions so we pick up where we left off. Priority order below is deliberate.

**Shipped:** MCP server (2026-07-28/29), Idea board with stickers/ink/frames/connectors/comments, reference picker bridge, prompt macros with `{tag}`, 9 board MCP tools (2026-07-29). See CLAUDE.md for implementation details.

## 1. Additional native node types

General rule locked in during the brainstorm: **one native node per editing *domain***,
never one node per tool/button — `native.crop`/`native.mask` are already this pattern
(operation = node with an embedded editor for configuring it), extend it rather than
inventing a new mechanism. Node = before/after lineage in the grid (creator/output
binding), never mutate-in-place.

**Images** (beyond existing crop/mask/gemini-watermark-remove):
- `native.color_correct` — brightness/contrast/saturation/curves.
- `native.transform` — rotate/flip/resize/perspective.
- `native.erase` — brush-to-transparency (crop/mask's sibling, direct result instead of a
  mask).
- `native.compose` — generalization of `native.character_chart`: N images composed into a
  grid/collage from a template.
- `native.annotate` — text/arrows/frames over an image (dovetails with the grid
  brainstorming layer above).

**3D** (once 3D asset support lands): scope pulled from `visualbruno/3DGenStudio`'s
mesh-editor feature set (Texturing, Modeling, Sculpting, Painting, Displace, Projection,
Auto UV/Retopo/Rig, Inpainting, Optimize) and re-mapped to our domain-per-node rule:
- `native.mesh_sculpt` — all sculpt brushes (Standard/Clay/Inflate/Smooth/Flatten/
  Pinch/Grab) in *one* embedded editor, not one node per brush.
- `native.mesh_paint`, `native.mesh_displace`, `native.mesh_model` (faces/verts, non-manifold
  fixes), `native.mesh_optimize` (decimation), `native.mesh_export` (GLB/FBX, terminal action).
- `native.auto_uv`, `native.auto_retopo`, `native.auto_rig` — heavyweight GPU ops. These
  three are the concrete case where "offload native processing to a separate worker
  service" (considered and explicitly deferred for images, since we can just give the VM
  more RAM) stops being optional and becomes necessary.
- `template.mesh_texture` / `template.mesh_project` — texturing/projection, stays on
  ComfyUI (template node), not native.
- `template.mesh_gen_*` — mesh generation (TripoSR/Wonder3D/Hunyuan) via ComfyUI.

**Audio** (planned): `native.audio_trim` (waveform trim/fade/volume — e.g. WaveSurfer-based),
`native.audio_slice` (segment → separate audio assets each), `template.audio_gen_*` via
ComfyUI.

**Video** (lower probability): `native.video_trim`, `native.frame_extract` (video → image
frames), `native.frame_assemble` (frames → video) — bridges between asset types.

**Text**: `asset.note` (see §1), `native.prompt_compose` — generalization of the existing
Multiple-Angles LoRA prompt builder into a general prompt-assembled-from-blocks node.

Status: sketch-level only, not started; blocked on the corresponding asset type (3D,
audio) actually landing first, except for the image-domain nodes which have no such
blocker.

## 3. Agent handoff between project chats

Agreed 2026-10-21. One project-chat agent passes its task on to another model (a
cheaper one, or one that can do something it can't, e.g. draw) **one way**: the source
chat is frozen for good, even for the person, and shows a link to the continuation and
the reason. This is a relay, not delegation — nothing comes back, so the source never has
to verify a result, and two agents never own one task. The motivating case: a cheap model
spent an hour unsure what was wanted, then did the work in seconds; an expensive model
that understands the person can rewrite the task so the cheap one gets it at once.

- **Project chats only.** They all work through `/mcp`, so a receiving agent can never
  gain rights the sender lacked. Dev chats are out of scope.
- **The system decides, never the agent.** The agent calls `hand_off`; the runner
  checks the policy at no token cost. A rule in a prompt is not a permission.
- **Chain ban.** Every handoff carries its chain (Opus → Flash → Astra). No model in it
  can receive the task again, whatever the table says — so chains always end. The ban is
  per *model identity*, not per provider: Claude Opus in Claude Code and in Antigravity
  are one identity (no "returning" through another CLI), but two targets with separate
  limit pools.
- **One table of rules for the whole app**, keyed `from target -> to target`
  (target = provider + model). Everything starts as *ask*. The popup offers Allow now /
  Allow always / Deny now / Deny always; the "always" answers fill the table, and the
  person can edit it in settings. No ranking formula: price is not the only axis (a
  dearer model that can draw is a legitimate target).
- **Agents only see targets they can actually use.** `list_handoff_targets` hides,
  without explanation, anything in the chain, denied always, out of limits ("back at …"
  is the only thing said), or spending too fast. Extra information is extra tokens and
  extra hesitation.
- **Limits are judged by pace, per pool, on the server.** A pool is what the provider
  actually meters (Antigravity meters groups: "Gemini models", "Claude and GPT models",
  Argon on its own). Any window at 100% → hidden. Used share far ahead of elapsed share
  (e.g. 80% used with 3 h of 5 h left) → hidden. Moderately ahead → **scarce**: shown
  with "better not", and **always asks** the person — even for an allowed-always pair —
  with the numbers and Yes/No only (no "always": tomorrow the pool is different).
  Thresholds live in config.
- **A timeout is a soft no** (minutes, config): no handoff, table untouched. After any
  no, that chat may not ask for the same target again.
- **While the person decides, the sender is stopped**; on a no (or timeout) the system
  itself resumes it with "not approved, continue yourself". Rules and denials are per
  *target*, never per identity — a person may refuse Opus-in-Antigravity for its limits
  while allowing Opus in Claude Code.
- **Model profiles are written by hand, never inferred** (2026-10-22, replacing a
  planned "learn capabilities from observation" stage). Each target carries
  `good_for`, `appetite` and `draws_natively`, drafted from public reviews and then
  corrected by the person's own experience, which wins (Opus spends fewer tokens per
  task than its price suggests; Claude inside Antigravity is a different harness, so
  nothing about Claude Code carries over). `draws_natively` means the model's *own*
  image generator — every agent can draw through the orchestrator's ComfyUI tools
  anyway — and the person deliberately leaves it off for Antigravity's Claude/GPT
  models: they would rather drawing never lands there than test whether it works.
  Editable in the Agent tab's handoff settings.
- **The first turn of the new chat is assembled by the system** from a fixed template,
  service text in English (cheaper), with an instruction to answer in the person's
  language: header with the chain and who may not receive it back; the rewritten task;
  why this model; what was already done; where to work (node/dashboard ids, validated);
  the person's own messages **verbatim** as the source of truth (last N); attachment
  names only (imported on demand from the old chat, never copied); "if unsure what the
  person wants, ask one question, don't guess". Agent-written fields have hard length
  caps. Not a copy of the chat: in project chats the state lives in the orchestrator
  and the new agent reads it through MCP.
- New chat starts **immediately**, at effort **low** by default (the hard part —
  understanding the person — was done by the sender; low is cheap and usually enough),
  configurable per model.

Shipped 2026-10-22: (1) core — tool, chain, table + popup, frozen chat, first-turn
template; (2) limits — pools, pace, hidden/scarce, always ok under 30% used;
(3) hand-written model profiles. Briefs: `docs/agent-handoff/stage1-brief.md`,
`stage2-brief.md`.
