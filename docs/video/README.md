# MiniMax H3 — reference material for the video board (roadmap.md §2)

`minimax_h3_ref2va.workflow.json` is the ComfyUI **API-format** export this whole design
was derived from (source: the official ref2va template, ComfyUI PR
[#15224](https://github.com/Comfy-Org/ComfyUI/pull/15224), weights at
[Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3)). Every node id
quoted in roadmap.md §2's role manifest refers to **this file** — keep them in sync, and
re-declare the manifest if the workflow is ever re-exported (ids change; binding is by id,
never by title).

## Prompt format — `prompt_guide_ref.md` / `prompt_guide_base.md`

Both official guides are saved here (from the same HF repo as the weights):
`prompt_guide_ref.md` covers **ref2va — the mode this workflow runs**;
`prompt_guide_base.md` covers T2VA/I2VA/FL2VA/L2VA. Read the ref one before writing
any prompt or building any prompt assembler. It is not style advice — it is a
**mandatory six-section structure**, and a free-form paragraph is simply not the input
type the model expects.

```
subject_definitions -> summary -> retention_analysis
  -> detailed_description -> overall_soundscape -> non_diegetic_music
```

The parts that carry the most weight, and that nothing else in this repo records:

- **`<Subject N>` is a fourth label type**, distinct from `<Picture N>`/`<Video N>`/`<Audio N>`
  and absent from the workflow export. A *character* is a Subject assembled from several
  assets ("`<Subject 1>` is the woman whose appearance comes from `<Picture 1>` and whose
  walking motion comes from `<Video 1>`"), not a picture reference. Role tokens in
  roadmap.md §2 should therefore expand to `<Subject N>`, not `<Picture N>`.
- **Speakers get stable ids `(S1)`, `(S2)`** in playback order, and dialogue goes inside
  `<d>[English] ... </d>`. These plus `<Subject N>` are the three official means of binding
  a line to a character; without them attribution is left to chance (2026-08-23: a
  free-form paragraph naming the characters in plain prose gave one character's line to
  the other one).
- **`summary` opens with a bracketed task-type prefix** — `[video continuation +
  reference generation + audio reference]`. `audio reuse` copies the signal;
  `audio reference` only borrows timbre/delivery, which is how a voice is bound to a
  speaker that has not spoken before.
- **`retention_analysis`** declares per-subject what survives: `fully_preserved`,
  `partially_preserved`, `attribute_transfer`, `weak_reference` (audio: `fully_copy`,
  `partially_copy`, `reference`, `weak_reference`).
- **Punctuation inside `<d>`** is restricted to `.` `?` `!` `,` — no ellipses.
- **`detailed_description` targets 350-500 words** for generation tasks. The visual-style
  sentence goes *before* `[Shot 1]`; first shot carries no timestamp, later ones are
  `[Shot N] At MM:SS.mmm`.

Note the ref guide's own title: "Full-Reference Mode **Rewrite** Output Format". This is
the output of a prompt-rewriting LLM inside MiniMax's own pipeline — a step ComfyUI
bypasses entirely. So either the video board assembles the structure itself (deterministic,
free, and it already knows the subjects/references/task type) or a local LLM does it from a
draft. Only `detailed_description` and the dialogue are genuinely creative; the other five
sections are derivable from what the board already stores.

## The model

MiniMax H3 is omni-modal: it understands text, image, video and audio, and generates video
with **native stereo audio** — voice, sound effects and music modelled jointly in one
forward pass, not layered afterwards. Up to 2K, 24 fps, ~15 s maximum.

This workflow runs the **reference-to-video (ref2va)** task via `MiniMaxH3ReferenceToVideo`.
The diffusion weights (`minimax_h3_ref2va_pruned_int8_convrot`) are a different set from the
`fl2va` model used by the t2v/i2v templates.

## Inputs that matter

- **9 reference images, 3 reference videos** (each may carry its own paired soundtrack),
  **3 standalone audio clips**.
- **prompt** references inputs *by tag, in the order they were connected*: `<Picture 1>`,
  `<Video 1>`, `<Audio 1>`. Numbering is per-modality and 1-based. Being explicit about
  *which reference drives which part of the shot* is what makes ref2va behave; the docs
  call its output "very sensitive to prompt wording".
  → This is why roadmap.md §2 stores prompts as role tokens and expands them only at build
  time, after pruning has fixed the final numbering.
- **`ref_image_size`**: `match` scales references down to the generation resolution
  (faster); `max` keeps up to a 2048 px short edge for stronger identity fidelity, at real
  cost — reference tokens ride along *every* sampling step.
- **width/height** from `ResolutionSelector` (115); **duration** in seconds through the
  math node (131) fed by the float (132).

## Sampling / decode

- Sampler `res_multistep`. The graph ships `scheduler: simple`, but the template docs say
  **`beta` or `normal` outperform `simple` for reference-heavy prompts** like this one.
- `SamplerCustomAdvanced` emits one packed audio+video LATENT; `VAEDecode` (video VAE) and
  `VAEDecodeAudio` (audio VAE) each pull their own half out of it, and `CreateVideo` muxes
  them into one MP4 with synced sound.

## Duration is quantized

Node 131's expression is:

```
max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17
```

i.e. valid lengths are **`frames = 5 + 17k`**, a 0.708 s lattice at 24 fps:

| frames | 5 | 22 | 39 | 56 | 73 | 90 | 107 | 124 | 141 | 158 | 175 | **192** | 209 | 226 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sec | 0.21 | 0.92 | 1.63 | 2.33 | 3.04 | 3.75 | 4.46 | 5.17 | 5.88 | 6.58 | 7.29 | **8.00** | 8.71 | 9.42 |

Feeding an already-valid length back through it is idempotent (`107 % 17 == 5`, so the
correction term is 0), which is also why it does not matter whether `ComfyMathExpression`
uses Python or C negative-modulo semantics — as long as nothing ever feeds it an
off-lattice value. roadmap.md §2 sidesteps the node entirely by writing `136.length` as a
literal in frames and letting the orphan GC drop nodes 131 and 132.

## Gotchas already found in the export above

- `SaveVideo` (92) is on `format: auto, codec: auto` — pin to h264 + aac, or the browser
  preview will one day get something it cannot play.
- Slots 1-8 of `ref_images` and all three video/audio refs are filled with the same
  placeholder file (`mecha_dragon_lightning.png`, `bedroom.mp4`). That is the evidence
  that the template expects unused references to be *removed*, not left dangling — hence
  the prune-by-input-key + reachability-GC design.
- One `LoadVideo` feeds **two** `MiniMaxH3ReferenceToVideo` inputs through
  `GetVideoComponents` (output 0 → picture, output 1 → audio). "Use this video without its
  audio" is therefore dropping one input key, not deleting a node.
