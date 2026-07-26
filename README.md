# ComfyUI Orchestrator

A self-hosted web app that sits on top of one or more [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instances (and, optionally, paid image-generation APIs) and turns them into a proper **iterative creative pipeline** — instead of a single canvas where every run overwrites the last one.

## The problem this solves

ComfyUI is great at *running* a workflow, but it has no real concept of a project: no history of variants, no easy way to branch "take this result and try three different next steps from it," no way to spread work across several GPU machines and just see whichever one is free. If you're iterating — generate a batch, pick the best one, feed it into the next step, maybe branch off two different directions from there, regenerate a step you didn't like — you end up doing all of that bookkeeping yourself, by hand, across folders of PNGs.

This app is that bookkeeping layer.

## What it actually does

- **A grid, not a graph.** Each project is a set of horizontal *tracks* (swimlanes). Each cell in a track is either a generation step or an asset (an image/model), alternating left to right. This is deliberately not a general node-graph editor — it matches how a linear creative pipeline actually gets used in practice, branching only when you explicitly want a fork.
- **Pick-the-best, then branch.** A step can generate N variants at once. You keep the one you like; the rest automatically spawn their own track below so you can pursue any of them further without losing the others.
- **Name your cells.** Two steps of the same type look alike on the grid, so any node can be given an optional short label that replaces its type name on its cell — enough to tell "head close-up" from "full-body back" at a glance.
- **Multiple backends, load-balanced automatically.** Point it at more than one ComfyUI instance; the dispatcher sends each job to whichever is free, and jobs wait in a queue if none are. A job that's genuinely slow (not stuck) can safely run for a long time — the system watches for stalled progress, not a fixed timeout.
- **Any workflow, no code to add one.** Upload a ComfyUI workflow (API-format JSON) and the app inspects it, detects seed/prompt/size/etc. fields, and turns it into a reusable template — no code changes needed to add a new kind of step.
- **A few built-in nodes for the fiddly bits.** Alongside uploaded workflows, a small fixed set of steps are baked into the app itself and run in-process (no ComfyUI round-trip, no GPU backend occupied, instant):
  - **Crop** — trim an image to a region you drag out.
  - **Paint Mask** — draw a mask straight onto the image; it bakes into the alpha channel the way ComfyUI's own clipspace mask editor does (painted = transparent hole).
  - **Character Chart** — composes four head + four body reference images into a single character sheet.
- **Workflow-aware prompt helpers.** It doesn't just pass prompts through blindly — it can recognize *what a workflow is* and surface a tailored editor for it. For instance, any workflow that loads the Qwen-Image-Edit **Multiple-Angles LoRA** automatically gets a camera-angle builder in its prompt field: pick azimuth / elevation / distance from dropdowns and it composes the exact `<sks> …` tokens that LoRA expects, so you don't have to memorise them. Detection is automatic and content-based — the moment a workflow references that LoRA, the builder appears wherever that prompt is edited (both the per-node params and the workflow's baked-in prompt editor); no flag to set.
- **Paid image APIs as just another backend.** Point it at a provider such as **Google Gemini** ("nano banana") image models and it slots in beside your ComfyUI instances — the dispatcher treats it as a backend with effectively unlimited capacity and routes a job to it only when you've enabled that provider for the node type (and picked it, or left the node on "Auto"). You supply the API key; an optional **daily request limit** per provider keeps a paid backend from running away with your quota.
- **Runs anywhere on your LAN.** Built for a home-lab setup: one small Debian box runs the orchestrator, your GPU machines run ComfyUI and can be turned on only when needed — the orchestrator just waits and retries.

## How it's built

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI, async SQLAlchemy |
| Database | PostgreSQL |
| Job dispatch | In-process async queue (no Redis/Celery — one box is enough for one user) |
| Realtime updates | WebSocket, proxied from ComfyUI's own progress events |
| Asset storage | Local disk, served back out by the API |
| Frontend | React + TypeScript, a hand-rolled CSS-grid canvas (not a graph-editor library) |
| Deploy | Single systemd service serving the API, the WebSocket feed, and the built frontend together |

See [`CLAUDE.md`](CLAUDE.md) for day-to-day operational notes — deploy process, server specifics, and the known gotchas the code has settled into. Worth knowing: the shipped system deliberately diverged from its original plan in a few places, always toward "one box, one user is enough" — an **in-process async queue** instead of Redis/Celery, **local disk** instead of an S3/MinIO object store, and a **single systemd service** (API + WebSocket + built frontend together) instead of a multi-container compose stack.

## Status

This is a personal home-lab tool, built and run for one user. It's not packaged for turnkey self-hosting by strangers yet (no Docker Compose, no installer) — `deploy/` documents how it's actually deployed today, which assumes a single Debian box you already control.

## License

Apache License 2.0, with the [Commons Clause](https://commonsclause.com/) restriction on top.

In plain terms: **run it, modify it, use it as a tool to help produce work for commercial projects — all free, no strings attached.** What you can't do is sell *the software itself* — as-is or modified, under this name or a different one — e.g. standing up your own hosted version of this orchestrator and charging people for access to it, or repackaging your fork as a paid product. If that's what you want to do, reach out about a separate license instead.

See [`LICENSE`](LICENSE) for the full text.
