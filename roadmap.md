# Roadmap

Captures a design brainstorm (2026-07-27, recovered from a lost claude.ai session) about
where this project goes next. Not a spec to implement literally — a record of decisions
and open questions so we pick up where we left off. Priority order below is deliberate.

## 1. MCP server (highest priority)

### Vision

The agent is a **bootstrapper / night shift**, not a hands-off "eyes only" advisor and not
purely an "operator that clicks buttons for you." Three usage modes:

- **Shared skeleton**: "do a basic pass of this character, use project X as reference,
  4 variants/step, pick the best" → you get a half-finished project and take over by hand.
- **Overnight batch**: "make 5 characters while I sleep."
- **Fan-out**: "give me 10 variants of this table/building" — the orchestrator already
  parallelizes across 2 GPU backends out of the box (verified: two manually-run nodes
  already share the queue cleanly), so this is free throughput the agent just triggers.

Core value: the orchestrator's move-not-mutate model (candidate pickers) makes the agent's
choices *provisional and safe* — nothing it picks destroys other candidates, you just
re-click in the morning. MCP exists to keep both GPUs fed unattended, not to make the agent
a better artist.

### Tool contract — node-centric, dispatch/await split

Granularity is **the node**, never the grid and never a raw ComfyUI job — the pipeline is
step-sequential by construction (column N+1 can't start before column N is done and a
candidate is chosen), so a "whole grid state" tool has no use case, and the agent doesn't
care what ComfyUI itself is doing, only what a node produces.

- `get_project_recipe(project_id)` — ordered steps → node_type + key params + prompt
  roles (leader/follower). A recipe, not raw node JSON — the agent walks it step by step
  and creates/configures nodes as it goes (no bulk "clone whole project structure" — that
  was an earlier wrong framing, dropped).
- `create_node(project_id, step, track, node_type, params)`, `set_reference_asset(node_id, image)`,
  `set_prompt(node_id, text, role)` — build one step at a time, substituting the new
  subject/reference in.
- `run_node(node_id, variants, backend=comfy|api)` — **non-blocking**, returns a handle
  immediately. The orchestrator's existing scheduler already spreads jobs across both GPU
  backends — the agent never needs backend/queue visibility (`get_backends` was proposed
  and dropped: that's the orchestrator's job, not the agent's concern).
- `await_node(node_id, timeout)` / `get_runs_status([node_ids])` (non-blocking poll) —
  always with a timeout, never an unbounded block (see "handle persistence" below).
- `rerun_node(node_id)` — reroll a failed/orphaned node; the night safety net.
- `get_candidates(node_id)` — feeds candidate images into the agent's multimodal context
  so it can actually *see* and judge, not just read JSON.
- `select_candidate(node_id, candidate_id)` — move-not-mutate selection.
- `flag_cell(node_id, note)` — "mark ambiguous and continue" instead of blocking to ask a
  question nobody's awake to answer. Default behavior overnight; you review flagged cells
  in the morning.

Explicitly **not** building: `get_backends`, `get_grid_state`, `notify_done` (that's
Claude Code's own push channel, not an orchestrator concern), `clone_project_structure`.

### Handle persistence — resolved, no new persistence work needed

Question was: does a `run_node` handle need to survive an MCP-process/site restart? No —
and pushing it into the DB wouldn't actually help, since ~90% of restarts mid-generation
already mean the generation itself failed (no durable ComfyUI-side recovery exists — see
`recover_orphaned_jobs()` in `CLAUDE.md`). So:

- **`node_id` *is* the handle.** Status is read live from the existing `jobs`/`nodes`
  tables (already persisted), not from an ephemeral in-process object. Nothing new to
  persist — it already is.
- `await_node` must have a **timeout**, never block forever. If the server dies mid-await,
  the connection just errors/drops like any other timeout — the agent (or its host loop)
  retries the poll, it doesn't hang silently forever.
- `recover_orphaned_jobs()` must reliably mark stale "running" jobs as `failed` on server
  boot, *before* the MCP server starts answering — otherwise a polling agent reads a
  perpetual lying "running" status and never knows to `rerun_node`.

### Node-type discovery (`list_node_types`)

Problem: can't rely on user-written descriptions (field is almost always left blank in
practice). Discovery must be built from **auto-derived facts**, not authorship.

**Level 1 — static fingerprint (deterministic, not an LLM call):**
extend the existing `analyze_workflow()` / `find_editable_text_fields()` in
`backend/app/core/workflow_analyzer.py` (do not write a second graph-walker — reuse per
the project's duplication rule) to also pull:
- model (`UNETLoader`/`CheckpointLoader*`)
- LoRA (`LoraLoader*`, name + strength)
- prompt(s) — already traced via the sampler-trace logic
- I/O slot counts (`slotFields`, already exists)
- sampler params (seed/steps/cfg — already exists)

**Two distinct prompt concepts, not one** (this took a few passes to untangle against real
code — see `Settings.tsx`'s `CapabilityTextFieldsModal` / `find_editable_text_fields`):
- **Baked prompt at the capability level** (e.g. `TextEncodeQwenImageEditPlus` text not
  exposed via `param_mapping`) — edited via the "prompts" button in Settings, feeds the
  auto-description, regenerates the description when changed.
- **Exposed param-mapped prompt at the instance level** (e.g. `createimage`'s
  `text_string_user_prompt`) — a free per-cell param, read via `get_node`, does **not**
  affect the type-level description.

**A node can have multiple backends/capabilities** (verified against real prod rows:
`createimage` = one `api_call` backend on Nano Banana/Gemini + two `comfyui_workflow`
backends on `asusi7`/`asusi9`). Fingerprint merges **per-attribute** across backends:
identical → `all:X`; differs → `backend1:Y backend2:Z`. Lets the agent see at a glance
whether "the same node" is configured identically everywhere or differently per machine.

**API-only backend (no graph) priority:**
1. If the API has a baked, non-parameter prompt → use it directly as the description, no
   graph needed, no guessing.
2. If the prompt is itself an exposed free parameter (e.g. Nano Banana) → only *then* fall
   back to a low-confidence guess from param names (`prompt`+`image_size`+`aspect_ratio` ⇒
   "probably a create-image node"). This guess is a last resort strictly for API-only nodes
   with no graph at all — if any comfy/graph backend exists, always analyze the graph
   instead of guessing.

**Description source priority chain (final):**
```
manual user edit  >  valid agent_description  >  auto-fingerprint (merged)  >  raw workflow JSON (Level 2)
```
- `description_source: auto|manual` dirty-flag: auto-regenerates on baked-prompt/config
  change until the user edits the text by hand, then freezes permanently. One escape
  hatch: a "reset to auto" button. No drift-warning indicator (considered, dropped — a
  stale manual description after a workflow re-upload is the user's own problem).
- Level 2 (raw workflow JSON) only when the fingerprint is "thin" (unrecognized node
  classes) — exceptional case, not the default path.

### `agent_description` cache — the token-economy hack

Separate DB field + MCP endpoint where an agent can write back its own compact
interpretation of a node, for later readers (itself, another agent, an agent in a
different project) to reuse instead of re-deriving it.

**Trigger — generalized, not gated to "only after falling back to Level 2":** write-back
is allowed any time the agent's candidate description is more compact than whatever it
actually read to produce it — whether that source was a verbose baked API prompt (Level 1
case — e.g. three paragraphs of studio-lighting/pose prompt-engineering boilerplate that
the agent distills to "T-pose node," 5 words), a merged fingerprint, or a raw workflow
graph (Level 2). The level of the source is irrelevant; the only condition is compression.

**Guard:** compare **character length**, not tokens or words. Word count under-counts
code/JSON-like text (few whitespace-delimited "words," many actual tokens from punctuation
and symbols) — `{"class_type": "KSampler"}` is one "word" but many tokens. A real tokenizer
(`tiktoken` etc.) would be more precise but adds a dependency (and typically needs a
network fetch for its BPE tables) for precision this comparison doesn't need — it only has
to be *directionally* right. `len(candidate) < len(source_agent_read)` is cheap,
dependency-free, and sufficient; reject the write if the candidate isn't strictly shorter.

**Invalidation:** hash of the node's full config (across all its capabilities), not an
event log — mismatched hash on read ⇒ ignore stale `agent_description`, fall back down the
priority chain. A manual user edit both outranks and invalidates it.

**Caveat to keep in mind:** the hash catches config drift, not the agent being *wrong*. A
bad `agent_description` can persist and mislead readers until the config changes. Treat it
as a hint a later agent is free to overwrite, never as ground truth.

### Cost/backend guard for autonomous mode

Not a property of the node-type (`cost_class` per type — considered, dropped). It's a
property of the **backend used per call**:

- `run_node(node_id, backend=comfy|api)`, defaults to `comfy` (free/local).
- In `auto` session mode, the **server** (never the agent/prompt) hard-blocks any
  `run_node(backend=api)` call → block + `flag_cell`. The paid variant of that node just
  gets skipped and flagged for you to do by hand in the morning.
- Optional: a pre-authorized budget (N paid calls / $X per overnight batch) loosens the
  block. Live per-call confirmation (`AskUserQuestion`-style) is `interactive`-mode only —
  meaningless overnight by definition.

### New node-type authoring (agent creates a node-type, not just uses one)

Scenario: agent asks a **separate, existing, not-ours** ComfyUI MCP server what models are
available on an instance, generates or finds a workflow it's missing, then registers it as
a new node-type on our orchestrator, bound to that backend.

**Final design — one tool, no sampling, no propose/create split:**

```
create_node_type(workflow_json, name, backend_id, param_mapping)
```

- `param_mapping` is **always mandatory** — the agent states up front what to rename and
  what to expose as an optional grid-editable param. No auto-fill-with-defaults fallback
  (considered and rejected — it silently produces a node-type nobody can correct afterward,
  since we deliberately don't want an `edit_mapping`-style tool).
- Server runs `analyze_workflow()` internally and diffs it against the supplied
  `param_mapping`. Any mismatch (agent referenced a field the analyzer doesn't see, or
  missed something required) → **hard error, nothing created.**
- **`seed` must be mapped, or the call errors.** Not a style preference — `run_node(node_id,
  variants=N)` fans out N candidates, and without seed exposed as a param the orchestrator
  has nothing to vary between them. This rule is MCP-specific; the existing human UI wizard
  is untouched (a human can still create a node-type with no seed param, same as today).
  Edge case (some workflows genuinely have no seed node, e.g. certain Trellis-style
  pipelines) is deferred until it's actually hit, not solved preemptively.
- **No `sampling`.** MCP's `sampling/createMessage` (server asks the *client's model* for a
  judgment mid-call) was seriously considered for the mismatch-repair path — it's real,
  SDK-supported, and not exotic-expensive (billed the same as any other client-side
  completion). Dropped because the caller here is always an agent that already has full
  project context: constructing enough of a digest for a context-blind sub-completion (a
  "brother with amnesia," not even in the working directory) to reconcile a mismatch is
  strictly more work than just retrying the call directly. A direct retry is cheap — it
  reuses this session's already-warm, cached context (a few hundred tokens for the error +
  corrected call), unlike spawning a cold subagent from scratch. Sampling support is
  negotiated once at MCP `initialize` (not discovered via a timeout), so even keeping it
  would degrade cleanly, not hang — but there's no case here where it beats a plain retry.
- Reuses the **existing wizard backend** (`analyze_workflow()` + param-mapping detection) —
  same code path the human UI wizard uses, agent-driven instead of click-driven. Slug
  collisions already caught by the DB unique constraint on `node_templates.node_type_slug`.
- **New node-types go live immediately**, no draft/pending-review gate — a bad one is just
  deleted in the morning, cheap and reversible, not worth a staging-state mechanism.

Side note on MCP transport, since it came up while designing this: MCP is a genuinely
persistent, bidirectional session (stdio pipes, or SSE/streamable HTTP for remote) — not
stateless request/response like REST — and capability negotiation (including whether the
client supports `sampling`) happens once at `initialize`. That's real and distinct from
REST. It just doesn't change the conclusion above for *this* tool, because what would make
`sampling` valuable — a caller with no LLM judgment of its own — doesn't apply: our caller
is always an agent with more context than a sampled sub-completion would have anyway.

### Cross-backend capability propagation

A second, related gap: a node-type can have a capability bound to one backend (e.g.
`http://192.168.0.4/`) but not another (`http://192.168.0.5/`) — the same two-GPU-machine
setup as the real prod example (`createimage` has separate capabilities per `asusi7` and
`asusi9`). An agent working against `.5` sees the node-type exists, tries to run it, and it
silently never progresses — there's no capability there to actually execute it, and today
an agent has no way to detect or fix that itself. Four tools cover it (one of them isn't
even new code, just newly exposed):

- **`list_backends()`** — thin MCP wrapper around the *already-existing* `GET /api/backends`
  (`backend/app/api/routes/backends.py`, already powers the human backend list in
  `Settings.tsx`). Returns `id` (the actual `backend_id` foreign key used everywhere else
  here) + `name` + `base_url` + `kind` + `is_active`. This is how the agent turns an IP it
  knows (`http://192.168.0.5/`) into the `backend_id` the other three tools need — without
  it, none of them are callable. Reuse the existing endpoint, don't write a second one.
- **`capability_exists(node_type_slug, backend_id) -> bool`** — a *type-level* check ("does
  this node-type have a capability for this backend at all"), distinct from anything about a
  specific grid node instance. Doesn't exist today — this is the missing piece that
  currently makes the failure silent instead of diagnosable.
- **`get_capability_workflow(node_type_slug, backend_id)`** — returns the `workflow_json` +
  `param_mapping` of an existing, *working* capability of this node-type (any backend that
  has one), so the agent has something concrete to adapt — "show me the recipe that already
  works elsewhere."
- **`add_capability(node_type_slug, backend_id, workflow_json, param_mapping, execution_type)`**
  — adds a *new* `Capability` row to an *existing* `NodeTemplate`, without creating a new
  node-type. The agent — using its own separate comfy-mcp connection to `.5` to know what
  models/LoRAs are actually installed there — adapts the borrowed workflow to that
  instance's models, then submits it here.

**Extra validation `add_capability` needs that `create_node_type` didn't:** it must also
check that the new workflow's detected I/O signature (image/file slot count) **matches the
existing `NodeTemplate.param_schema`'s slot count**, not just that its own `param_mapping`
is internally consistent. `param_schema` — and therefore row-span, see the Grid domain
model in `CLAUDE.md` — lives on the *template*, shared across all its capabilities; a newly
added capability with a different slot count than the template's existing one would
silently desync a node's rendered row-span from what the backend that happens to fulfill it
actually needs. Reuse the same `analyze_workflow()` + mandatory-`param_mapping` +
seed-required validation `create_node_type` already has — don't duplicate it, have
`add_capability` call through the same validator.

## 2. Planning / brainstorming capability inside the grid

Explicitly **not** a separate view/canvas (an Excalidraw-style whiteboard tab was
considered against `visualbruno/3DGenStudio`'s brainstorming boards and rejected — "ідея
живе там, де й робота," i.e. it should live in the grid, not off to the side).

- **`asset.note`** — a plain text/markdown `node_type` that behaves as a normal asset node:
  sits at an asset-position per column parity, drags/moves like any other asset node.
  Brainstorming becomes "text nodes and reference images laid out among the working
  chains," not a separate mode. Zero risk to row-span/output-binding invariants.
- **Annotation layer** (arrows between nodes, colored tags on a track, a comment pinned to
  a cell) — a lighter, purely cosmetic layer on top of the grid. Second priority, not
  blocking on `asset.note`.

Status: accepted in principle, low priority, not started.

## 3. Additional native node types

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

**Text**: `asset.note` (see §2), `native.prompt_compose` — generalization of the existing
Multiple-Angles LoRA prompt builder into a general prompt-assembled-from-blocks node.

Status: sketch-level only, not started; blocked on the corresponding asset type (3D,
audio) actually landing first, except for the image-domain nodes which have no such
blocker.
