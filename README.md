# ComfyUI Orchestrator

A self-hosted web app that sits on top of one or more [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instances (and, optionally, paid image-generation APIs) and turns them into a proper **iterative creative pipeline** — instead of a single canvas where every run overwrites the last one.

## The problem this solves

ComfyUI is great at *running* a workflow, but it has no real concept of a project: no history of variants, no easy way to branch "take this result and try three different next steps from it," no way to spread work across several GPU machines and just see whichever one is free. If you're iterating — generate a batch, pick the best one, feed it into the next step, maybe branch off two different directions from there, regenerate a step you didn't like — you end up doing all of that bookkeeping yourself, by hand, across folders of PNGs.

This app is that bookkeeping layer.

## What it actually does

- **A grid, not a graph.** Each project is a set of horizontal *tracks* (swimlanes). Each cell in a track is either a generation step or an asset (an image/model), alternating left to right. This is deliberately not a general node-graph editor — it matches how a linear creative pipeline actually gets used in practice, branching only when you explicitly want a fork.
- **Pick-the-best, then branch.** A step can generate N variants at once. You keep the one you like; the rest automatically spawn their own track below so you can pursue any of them further without losing the others.
- **Multiple backends, load-balanced automatically.** Point it at more than one ComfyUI instance; the dispatcher sends each job to whichever is free, and jobs wait in a queue if none are. A job that's genuinely slow (not stuck) can safely run for a long time — the system watches for stalled progress, not a fixed timeout.
- **Any workflow, no hardcoded node types.** Upload a ComfyUI workflow (API-format JSON) and the app inspects it, detects seed/prompt/size/etc. fields, and turns it into a reusable template — no code changes needed to add a new kind of step.
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

See [`SPEC.md`](SPEC.md) for the full original design doc, and [`CLAUDE.md`](CLAUDE.md) for day-to-day operational notes (deploy process, server specifics, known gotchas).

## Status

This is a personal home-lab tool, built and run for one user. It's not packaged for turnkey self-hosting by strangers yet (no Docker Compose, no installer) — `deploy/` documents how it's actually deployed today, which assumes a single Debian box you already control.

## License

Apache License 2.0, with the [Commons Clause](https://commonsclause.com/) restriction on top.

In plain terms: **run it, modify it, use it as a tool to help produce work for commercial projects — all free, no strings attached.** What you can't do is sell *the software itself* — as-is or modified, under this name or a different one — e.g. standing up your own hosted version of this orchestrator and charging people for access to it, or repackaging your fork as a paid product. If that's what you want to do, reach out about a separate license instead.

See [`LICENSE`](LICENSE) for the full text.
