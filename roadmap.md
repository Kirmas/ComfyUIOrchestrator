# Roadmap

Captures a design brainstorm (2026-07-27, recovered from a lost claude.ai session) about
where this project goes next. Not a spec to implement literally — a record of decisions
and open questions so we pick up where we left off. Priority order below is deliberate.

MCP server (formerly §1 here) shipped 2026-07-28/29 — see CLAUDE.md's "MCP server" section
and memory `mcp_server_shipped` for what was built and where it deliberately diverged from
the brainstorm below.

## 1. Idea board — the pre-production half of the pipeline

**Rewritten 2026-07-29.** The earlier version of this section ("`asset.note`, arrows
between cells, colored track tags — all inside the grid, explicitly *not* a separate
view") is superseded. It was decoration on a model that has nowhere to put an idea:

- **`Asset.node_id`** (`models.py`) means every image is *owned by a grid cell*. There is
  no project-level asset. Collecting 40 loose references therefore requires 40 grid cells
  under column-parity rules — i.e. a reference pool is structurally impossible today.
- The grid is **convergent by construction** (column parity, creator/output binding, one
  asset per cell). Brainstorming is divergent and unstructured. That's not a flaw in the
  grid; it's why the grid is good at production and wrong for ideation.

The goal this section now serves, in the user's words: *"беруся за нового персонажа і
проганяю його від брейншторму ідеї до 3D болванки."* Stages 3–5 of that chain already
exist (`select_candidate` + collapse chain, `native.character_chart`, the Multiple-Angles
LoRA builder); stage 6 needs `template.mesh_gen_*` (§2) but `AssetKind.mesh` and
`Model3DThumb` already work. **Stages 0–2 — the idea, the references, the divergence —
do not exist at all.** That hole is what §1 fills.

### The board is a separate view (reversing the earlier decision)

The original objection was to a free-XY canvas as a *reference dump*: 60 scraped images on
an unbounded plane need filtering and rating, not coordinates. That objection stands, and
is answered not by dropping the board but by **one storage, two presentations**: the board
(free XY, curated, mixed media) and a flat tag-filtered list (the picker used from the
grid). The board is a separate *view*, not a separate *world* — the two bridges below make
its contents addressable from the grid, which is what "ідея живе там, де й робота" was
actually protecting.

### Data model

- `Asset.project_id` — project-scoped assets that no cell owns. `Asset.node_id` is
  **already nullable**, so this is a small migration.
- `Board` (id, project_id, name) — one auto-created per project, but the table exists from
  day one because "a board per character" will be wanted immediately.
- `BoardItem` (board_id, kind, x, y, w, h, z, content JSON, color) — **a sticker**, in the
  Windows-Sticky-Notes sense: one card, exactly **one content type each**:
  - `text` — markdown, plus an optional unique-per-project `tag` (see bridge 2)
  - `image` / `audio` / `video` — `content.asset_id` → a project-scoped Asset
  - `frame` — the lasso: `shape: rect|ellipse` + color, one entity, two renderings
  - `ink` — freehand: SVG path + color + width. Deliberately carries **no semantics** —
    it's there for the feeling of freedom while sketching, and the eraser works
    **per-stroke, not per-pixel** (a pixel eraser drags in a raster layer and an undo stack
    for an outcome stroke-level deletion already gives).
  - `connector` — `from_item_id`/`to_item_id`. **Anchored to items, never to raw
    coordinates**: a free point-to-point line detaches the moment a sticker moves.
  - `comment` — `content.target_item_id`, no x/y of its own, rendered anchored to its
    target, ordered by `created_at`, carries `source: user|agent` like grid annotations.
    Comments are board items rather than their own table so one request loads a whole
    board and the agent creates them with the same tool it creates stickers with.
- No 3D on the board (a `model-viewer` per sticker would kill it). Audio/video stickers are
  cheap (`<audio>`/`<video>`) and notably do **not** require any pipeline support for those
  media types — the board can hold what the pipeline can't process yet.

### Bridge 1: grid asset cells can reference the board (one-way)

An asset cell's creation UI gains a third option beside the current upload: **"з
референсів"** → tag-filtered picker over the project's assets → creates an
`asset.refasset`. `refasset` must be extended to also point at a bare `asset_id` (today it
points at *another node's* asset, and a library asset has no owning node).

**The grid references, never owns.** The reverse direction ("send a generated output to the
board") was considered and **dropped** — it was the only thing that made this complicated,
because `Asset.node_id` is `ondelete=CASCADE`: a grid cell owning a board image means
deleting that cell silently destroys the picture on the board. One-way flow removes the
hazard entirely rather than working around it.

### Bridge 2: pulling note text into a prompt — manual by default

Scope: **the node instance's own config**, never the capability's baked `workflow_json`
(capabilities are global; a project's character description leaking across projects is the
failure mode that rules that out).

One UI — a **"взяти з ідей"** picker in the node's settings listing every text sticker,
multi-select — with two insert modes:

- **plain text (default)** — copies the text in and freezes it. Editing the sticker later
  changes nothing. This is the literal reading of "вручну, не автоматично".
- **macro `{tag}`** — resolved at run time. Worth having because a character description
  wanted by 12 nodes shouldn't be pasted 12 times, but it *is* the "automatic" the default
  avoids, so it only stays honest with three guardrails:
  1. the node config renders a **resolved preview** — a macro never hides what will run;
  2. `tag` is **unique per project**; selecting several stickers inserts several macros
     (`{head} {outfit}`), never one group macro with ambiguous resolution;
  3. a deleted sticker leaves its macro **visibly unresolved** in the preview, never
     silently expanding to empty — a quietly emptied prompt generates the wrong thing
     without anyone noticing.

  The resolved text goes into the job record, so "what description did this actually run
  with" stays answerable a week later.

Either way the markdown is **stripped to plain text on insertion** — `**bold**` and `- `
bullets are noise to a sampler.

### Deferred

- **Speech-to-text for dictating stickers.** Not a browser problem (Edge is Chromium and
  has `webkitSpeechRecognition`): the mic needs a **secure context**, and the frontend is
  served over plain `http://192.168.0.3:8000/`, so any LAN device other than the box itself
  is blocked outright. Unblocking it means HTTPS (Tailscale being the least painful route).
  Revisit then, browser API first, local `whisper.cpp` only if sending audio to Google is
  unacceptable.
- Multiple boards per project (the table supports it; the UI ships with one).

### Implementation traps this repo already knows about

- Modals inside the board's pan/zoom transform need `createPortal(..., document.body)` —
  the same trap as the grid's params modal (2026-07-21 incident).
- Reuse `usePinchPan.ts`; do not write a second pan/zoom.
- **Do not render connectors via `ArrowsOverlay`.** It measures the DOM with
  `getBoundingClientRect` and re-polls on a 500 ms interval — fine for the grid, visibly
  laggy on a board where items are dragged. Board connectors come straight from stored
  item coordinates.
- Markdown rendering needs a dependency (there is none today). Justified here, but keep it
  small — `vite build` has been OOM-killed on this box before.

### Status (2026-07-29)

Shipped in the dev copy, **not deployed yet** — migration `0014` has been checked
as offline Postgres SQL but not run against the real database:

- backend: `Board`/`BoardItem`/`Asset.project_id`+`tags`, `api/routes/boards.py`,
  `core/idea_macros.py`, macro expansion wired into `resolve_node_inputs`
- frontend: `Board.tsx` + `BoardSticker.tsx` (stickers, media, circles, ink,
  connectors, comments, pan/zoom), `ReferencePicker.tsx` (bridge 1),
  `IdeaTextPicker.tsx` + `MacroPreview` (bridge 2), `markdown.ts`
- MCP: `get_board`, `list_board_items`, `create_note`, `comment_on_board_item`,
  `connect_board_items`, `update_board_item`, `delete_board_item`,
  `list_reference_assets`, `place_reference_asset`

**Deployed to production 2026-07-29.** Migration `0014` went first, on its own,
validated with the `BEGIN … ROLLBACK` technique; the full `deploy/deploy.sh` ran
after, which re-synced `alembic/` (so prod's `versions/` now holds the `0014`
file that the standalone DB step had skipped) and found the schema already at
head. Verified on the live service: board creation, text and media stickers,
asset tagging, and macro resolution including markdown stripping and an unknown
`{tag}` left literal.

Library-asset tagging is done and lives **on the board**, not in the grid: a
media sticker's `⋯` panel edits the underlying `Asset.tags`, and the grid's
reference picker reads them to filter. Two different things wearing the word
"tag", kept apart deliberately — a text sticker's `tag` is a single unique
prompt-macro handle, a media sticker's `теги` are free labels on the asset.

**Multiple boards per project: dropped, not deferred.** One board per project is
the intended model. The `boards` table keeps its own primary key (the row is
created on first access and `board_items.board_id` points at it), but nothing
will ever offer a second one.

One decision made during implementation and worth keeping: the markdown renderer
is hand-written (`frontend/src/markdown.ts`) rather than a dependency, because
every real renderer passes raw HTML through and would need a sanitizer alongside
it — two dependencies to render bold text on a sticker.

## 2. Additional native node types

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
