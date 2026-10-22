# Roadmap

Captures a design brainstorm (2026-07-27, recovered from a lost claude.ai session) about
where this project goes next. Not a spec to implement literally — a record of decisions
and open questions so we pick up where we left off. Priority order below is deliberate.

**Shipped:** MCP server (2026-07-28/29), Idea board with stickers/ink/frames/connectors/comments, reference picker bridge, prompt macros with `{tag}`, 9 board MCP tools (2026-07-29), agent handoff between project chats (2026-10-22). See CLAUDE.md for implementation details.

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

## 2. Video board — MiniMax H3 ref2va

Design agreed 2026-08-22 (conversation, Ukrainian). **Not started**, and deliberately
gated on two cheap experiments (see "Blocking experiments" below) — the answers change
the shape of the feature, not just its priority.

### What it is (and what it deliberately is not)

A **separate board view** per project, alongside Grid and Board — precedent is the idea
board (§1): a third view with its own truth about its own relation, not a re-use of the
grid. Backed by one ComfyUI workflow, `MiniMaxH3ReferenceToVideo` (ref2va): up to 9
reference images, 3 reference videos (each optionally carrying its own audio), 3
standalone audio references, native stereo audio generated jointly with the picture.

It is a **video generator, not an editor** — the user's own framing, and it is what keeps
the data model small:

- you may only **append at the end**;
- you may only **edit length-preserving** (a layer replaces a time range with a clip of
  the same length);
- therefore **no trimming exists at all** — no in/out points, no ripple, no
  renumbering. A bad tail is fixed by a layer, never by shortening (shortening changes
  time). This killed an entire subsystem that an earlier draft of this design had.

Inserting new time in the middle is out of scope "at least for v1" — hence `Segment`
ordering uses `core/track_order.py`'s existing linked list rather than a dense integer,
so lifting that restriction later is not a migration.

### Model

```
VideoBoard  settings{aspect, megapixels, ref_image_size, scheduler, crossfade_ms}
            + global_refs[]  (2-3 images auto-attached to every new segment)
Segment     prev/next · length_frames · prompt(tokens) · refs[]
            continuation{seconds} · asset_id · history[]
Layer       z · start_frames · length_frames · prompt(tokens) · refs[]
            source_asset_id · asset_id · history[]
```

- **Layers, not clip-splicing.** A layer sits over a time range; render is base layer
  then layers bottom-up. Non-destructive by construction (toggle a layer off, the
  original is back) and it resolves the causality-vs-time conflict cleanly: layer N is
  made from the render of layers 0..N-1 over its range — a monotone stack, not a graph.
  The grid could not have carried this: its axis is causality (what was made from what),
  a repair clip's causal position is "after both neighbours" while its temporal position
  is "between them".
- **Global reference images** are the main defence against identity drift over a long
  chain of continuations — with ~700 segments nobody re-attaches the character sheet by
  hand. Slot budget is visible in the UI: e.g. 3 global + 6 per-shot.
- **History, not candidates.** At 6 min - 2 h per generation there is no room for "make 3
  takes and pick one". But the same number is why a regeneration must never silently
  destroy the previous result: keep prior takes, restorable, manually deletable.

### Time is quantized — this leaks into the UI

Valid lengths are `frames = 5 + 17k` at 24fps, i.e. a **0.708 s lattice**
(…, 3.75, 4.46, 5.17, 5.88, …, 8.00, …). Consequences:

- stored in **frames**, displayed in **seconds** (one decimal);
- **no free-text duration input anywhere** — a typed "5.0" would silently become 5.17
  and the timeline would drift from what the user typed. Slider/list that snaps, with
  ruler ticks so the snapping is visible;
- range selection: dragging either handle snaps the *length*, dragging the middle moves
  the window freely;
- a layer's output length == its reference length == the range it replaces, so
  **repairs never ripple**;
- feeding an already-valid length back through the graph's own math node is idempotent
  (`107 mod 17 == 5`), which also removes any dependence on that node's negative-modulo
  semantics. Better still: write `136.length` as a literal and let the orphan GC drop
  nodes `131`/`132`.

### Prompts

- The prompt is a **chip editor**, not a textarea: the user picks a reference (thumbnail
  / audio name) and the system emits the tag the model wants.
- The canonical stored form is the **role-token** one. `<Video 1>` exists only at build
  time. Never store the expanded text — it outlives the ref set that produced it and
  then points at the wrong reference, silently and plausibly.
- **`ref_video_0` is reserved** for the continuation/edit reference, always, even when
  empty — so "continue" always means `<Video 1>` as a program-level invariant rather
  than a convention. Visible consequence: 3 user video refs on a first segment, 2 on a
  continuation.
- **Prompt templates store roles, not indices**, for the same reason.
- Continuation/edit refs are **always with audio**; the `with audio` checkbox exists only
  on user-added refs. "Grab the audio of this range as a reference" (for catching a
  voice) is the same range→asset primitive with a different output modality, and lands
  in a `LoadAudio` slot, so it costs no video slot.

### The one bridge between the two views

**"take a range of the timeline as an asset"** (ffmpeg). Both features collapse into it:

- *continue*: render the last N seconds → `ref_video_0`, "continue" preset;
- *repair a seam*: render a window **spanning the cut** → `ref_video_0`, "fix `<Video 1>`,
  keep the first and last frames, smooth the transition" preset.

One reference, not two: the model sees the bad cut itself in temporal continuity, which
makes it an in-painting-in-time task rather than "invent the span between A and B", and
the keep-the-edges instruction is what makes the result splice back in.

The rendered range is **materialized as a real asset**, never re-rendered at run time —
otherwise re-running the node a week later reads a changed assembly and silently
produces something else. Materialize at job start (so a chain can be queued before the
earlier results exist), then reuse on re-run.

### Preview and export

The user must be able to watch the whole thing as one video **in the browser without
saving**.

- **v1**: two `<video>` elements, A/B swap with the next segment preloaded. Zero server
  work, scrubbing is exact, only weakness is a micro-hitch at a boundary if the prefetch
  is late.
- **v2**: an **HLS playlist** generated from the timeline state, played via `hls.js` —
  one continuous stream with seeking across the whole project. The segments already *are*
  HLS-shaped; cost is a one-off **stream-copy remux** per segment (repackaging, no
  decode/encode — an order of magnitude lighter than the Plex transcoding this box
  already runs). Only segments a layer cuts mid-way need a short real re-encode; cached.
- ⚠️ The bottleneck is RAM, not CPU. Measured 2026-08-22: the VM has **1951 MB total,
  ~585 MB available, and is already 412 MB into a 975 MB swap** while merely idling —
  Jellyfin (280 MB), Plex, transmission, docker and uvicorn (170 MB) all share it. Note
  that measurement was taken with a Claude Code session resident, which is itself ~520 MB
  (agent 417 + `claude rc` 105) — and that is the normal condition on this box, since
  builds and deploys are driven from a session. So the `vite build` OOM is less "the VM
  is too small for vite" than "the agent running the build outweighs the build": ~520 MB
  agent + 515 MB tsc/vite on a 2 GB box that is already swapping. Plan
  is to **raise the VM to 6 GB** (host has ~8 GB spare, earmarked for an Ollama that
  never worked out), which retires this constraint along with the `vite build` OOM
  workarounds. Until then: **one ffmpeg at a time**, thread-limited (4 cores, shared with
  Plex/Jellyfin transcodes), never alongside a frontend build.
- **Preview and export must share one flattening function** (base + layers → list of
  `(file, from, to)`) with two serializers (playlist / concat). Two implementations would
  drift, and the export differing from what the user just watched is the worst possible
  bug in this feature.
- Pin `92` SaveVideo to h264+aac explicitly. `format: auto, codec: auto` will one day
  emit something Chrome won't play, for non-obvious reasons.

### Workflow manifest (the "video workflow" section)

The generic wizard *discovers* slots; a video workflow must *fill known roles*. So the
video section is a role manifest, bound **by node id** (title-based binding was already
proven to corrupt the wrong field when two nodes share a title):

| role | node today | handling |
|---|---|---|
| output | `92` SaveVideo | sole output; pin h264+aac |
| prompt | `138` | expanded chip text lands here |
| resolution | `115` | `aspect_ratio` + `megapixels` from board settings |
| length | `136.length` | literal in frames; GC drops `131`/`132` |
| ref_image 0-8 | `137,139,141-147` | prune by `ref_images.ref_image_N` |
| ref_video 0-2 | `150/151`, `152/153`, `155/154` | slot 0 reserved; audio checkbox on 1-2 only |
| ref_audio 0-2 | `156,157,158` | prune by `ref_audios.ref_audio_N` |
| scheduler | `124.scheduler` | board setting (`beta`/`normal` beat `simple` for ref-heavy) |
| ref_image_size | `136.ref_image_size` | board setting (`match` fast / `max` faithful) |
| seed | `129.noise_seed` | persisted, for the regenerate-at-higher-res flow |

Re-exporting the workflow from ComfyUI means re-declaring the manifest, same rule as the
existing wizard. Everything else (models, VAEs, sampler, guider) is fixed and hidden.

### Dynamic graph assembly

The graph must be pruned per generation to whatever references the user actually
supplied. Two rules, both general rather than MiniMax-specific:

1. **The unit of pruning is an input key, not a slot.** A ref video feeds two keys
   (`ref_videos.ref_video_N` from `GetVideoComponents` output 0, and
   `ref_video_audios.ref_video_audio_N` from output 1), so "video without audio" is just
   dropping one key. Exposed in the UI as a single boolean; audio-only was considered and
   rejected as meaningless (that is what the `LoadAudio` slots are for).
2. **Orphan removal is a reachability walk from the output**, never a hand-written "if
   slot N is empty delete nodes X and Y". Dropping both video keys makes `GetVideoComponents`
   unreachable, and then `LoadVideo` in turn — the cascade falls out for free, and the
   same machinery serves every future workflow.

Reference tag numbering is per-modality, 1-based, and follows post-prune order — which is
exactly why the chip editor and role-token storage above are load-bearing rather than
cosmetic.

### Prerequisites in existing code

- `comfyui_backend.py` `result()` filters outputs to `class_type == "SaveImage"` — a
  `SaveVideo` graph currently returns **zero assets** while reporting success.
- `AssetKind` has no `video`; `for_mime` maps everything non-`image/*` to `other`.
- `workflow_analyzer.py` `INPUT_IMAGE_CLASS_TYPES = {"LoadImage"}` — `LoadVideo`/`LoadAudio`
  are invisible to it.
- `_upload_image` needs correct extension/MIME for mp4/wav or `LoadVideo` won't find the file.
- **ffmpeg is not installed on this box** and is foundational here (range render, remux,
  crossfade, concat, poster frames, duration probing). Add it to `deploy/debian-setup.md`,
  not just to the box, or dev and prod diverge.
- `deploy/deploy.sh` restarts the unit; with 2-hour jobs and no durable queue, a deploy
  mid-generation destroys the run. A "there are active jobs — continue?" guard is minutes
  of work and covers the failure that will actually happen. (A real durable queue was
  considered and explicitly declined as out of scope for a single-user hobby deployment.)

### Blocking experiments

Both are cheaper than any of the code above and both can invalidate parts of it.

1. **Same seed at a higher resolution.** Generate at 0.2 MP, regenerate at 0.4 MP with
   the identical seed, compare. The intended workflow is "tune everything cheap, then
   press regenerate-all at full resolution and go to sleep" — but a different latent
   shape means a different noise tensor and therefore a different denoising trajectory,
   so composition and motion may not survive at all. If they don't, the button must
   instead re-run with the **approved low-res clip as an additional video reference**
   ("same shot, same motion, sharper") — H3 is a reference model, this is its native
   mechanism. Costs one video slot during the regen.

   Regeneration is a **different task from generation and takes a different prompt**
   (user's call, 2026-08-22): the model must not invent detail, so the regen prompt is a
   *systemic* one ("reproduce `<Video 1>` exactly — same composition, motion and timing;
   more detail, sharper"), never the original generation prompt, and the only references
   passed are the reference images plus the segment's own old output. That also settles
   the slot budget: user video references are dropped by design during regen, so nothing
   gets squeezed out.

   Sub-question for the same run, therefore nearly free to answer: whether the original
   prompt must still ride along (appended, "for reference only", as a role token in the
   regen preset) to preserve **spoken dialogue**. The lines are written in the original
   prompt; a purely systemic prompt leaves the model reconstructing speech from a blurry
   low-res reference, and style anchors ("bold comic-book ink style") may not survive
   being read off that reference either.

   Consequence worth noting: with no continuation reference in the regen, each segment is
   reproduced from its own old output alone, so **regenerate-all is an independent batch,
   not a chain** — restartable per segment, no cascade when one fails. Whether
   inter-segment continuity survives that is exactly what this experiment measures.
2. **Listen to a seam.** Two segments joined with a continuation reference carrying
   audio. Audio is generated jointly per segment, so ambience level, music phrase and
   reverb tail are independent across a cut; an audio reference makes the model *imitate*
   the previous audio, not continue it sample-accurately. The `crossfade_ms` board
   setting is the agreed fallback (~20-40 ms hides most of it). If seams are audible even
   with the reference, that changes which content this feature suits (dialogue vs action
   under music), which matters more than any ordering decision below.

### Order of work (after the experiments)

1. Make the workflow simply *work* as an ordinary template node: `SaveVideo` in the
   output allow-list, `AssetKind.video`, video/audio upload, `<video>` in a cell.
2. Prune-by-input-key + reachability GC + literal `length` + the audio checkbox.
3. `Segment`/`Layer` + the timeline view with lattice-snapped selection.
4. Range→asset primitive + prompt presets. "Continue" and "repair" are two presets of one
   action, not two features — this is the cheapest part of the plan and the whole point of it.
