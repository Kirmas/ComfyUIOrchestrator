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
