# ComfyUI Orchestrator — context for Claude Code

Full product spec: [SPEC.md](SPEC.md) — read it for architecture, data model, and what's explicitly out of MVP scope.

## Grid/node domain model (read this before asking what a "chart"/"grid"/etc. is)

- **The Grid** (`frontend/src/components/Grid.tsx`) is a spreadsheet-like canvas: rows are **tracks** (`Track.row_index`), columns are **steps** (`Node.step_index`). Every column strictly alternates `asset`/`workflow` kind project-wide, starting from `Project.start_kind` (`kindForStep` in Grid.tsx mirrors `_kind_for_step` in `backend/app/api/routes/nodes.py`) — there's no per-cell choice of kind, it's dictated by column parity.
- **Node kinds**: `asset` (holds/references an image/file/mesh) and `workflow` (a ComfyUI job definition; spans multiple rows to reach its image/file input slots — see row-span below).
- **Node.node_type discriminator** (namespaced string):
  - `asset.single` — a settled asset with exactly one resolved output.
  - `asset.select` — an undecided candidates picker (several generated variants, none chosen yet); never draggable, never a compare source/target, no single "the" picture yet.
  - `asset.refasset` — a lightweight *pointer* to another node's asset (made via "+ ref elsewhere"), not a real owned `Asset` row — see `RefAssetNodeView` in `NodeCell.tsx` and `resolveSlotAsset` in `slotResolution.ts`. Its whole point is placing the same underlying asset in more than one grid cell without duplicating the file.
  - `template.<slug>` — a workflow node backed by a DB `NodeTemplate` row, created via the node-type wizard from an uploaded ComfyUI `workflow.json`.
  - `native.<slug>` — a workflow node backed by a hardcoded Python class in `backend/app/core/node_types.py`'s `NATIVE_NODE_TYPES` registry (code-only, no DB row, a closed developer-authored set). E.g. `native.character_chart` (`CharacterChartBackend`) composes 4 head + 4 body reference images into one character sheet; its param_schema declares 8 image slots, so it always wants an 8-row span. `GET /api/node-templates` merges native + DB templates into one list so the frontend doesn't need to know which is which.
- **Row-span paradigm**: there is no display-only/cosmetic position override anywhere — a node's rendered position is always exactly its `track_id` + `step_index`. A workflow node's desired row-span is the number of image/file fields its template declares (`slotFields()` in `templateUtils.ts`); "moving"/"resizing" it means actually reassigning `track_id`/`step_index` on it and its dependents (`applyRowMove`/`applyColumnMove`/`applyDiagonalMove` in Grid.tsx), never a visual-only change.
- **A workflow's materialized output is rigidly bound to its creator** (`Node.created_by_node_id`): it can only sit at `creator.step_index + 1`, in a row within the creator's own span or a track spawned from it — enforced both in Grid.tsx's `isPositionAllowedFor` (UI-facing fast path) and, authoritatively, the backend's `_ensure_output_binding` in `api/routes/nodes.py`. When moving a workflow node together with its dependents, the workflow's own PATCH must be sent before its output's PATCH, or the backend still sees the old creator position and 409s "can only move among its own creator's positions" (2026-07-18 incident, fixed by putting the workflow node first in all three move functions' ordering).
- **Compare** (`CompareModal.tsx`, overlay slider between two resolved images) and **reference/refasset** ("↗ Reference") are two unrelated features that happen to both involve picking another node — don't conflate them.

## Where this code runs

This working copy (`~/comfy-orchestrator`) is the **development copy**, owned by `keresh`, editable directly (no sudo). It is separate from the **live production copy** at `/opt/comfy-orchestrator`, owned by the `orchestrator` system user, run as systemd unit `comfy-orchestrator-api`. The two are not symlinked or synced automatically — deploying means copying dev → prod on purpose (see below).

This is a git repo (`main` branch, remote on GitHub) — use `git diff`/`git log` freely to see what changed. Commits happen only when the user asks; pushes likewise.

## Deploying a change (dev copy → live service)

`deploy/deploy.sh` scripts this (build frontend, sync backend + frontend
into `/opt/comfy-orchestrator`, `pip install`, `alembic upgrade head`,
restart the unit). `root-deploy.sh` syncs `backend/app/`, `backend/alembic/`
(the whole directory, not just `versions/`), and `requirements.txt` — it used
to skip `alembic/` entirely, so a new migration file only ever existed in the
dev copy and `alembic upgrade head` on prod silently had nothing new to
apply; the app then started against a schema missing a column its own
models.py declared (2026-07-17 incident, `nodes.node_type`). If you ever
rewrite this script from scratch, make sure alembic/ is in the sync list. It
shells out to `deploy/root-deploy.sh` via `sudo` for
the privileged half. See `deploy/README.md` for the one-time sudoers setup
that makes that `sudo` call passwordless for `keresh` — once that's in
place, Claude Code can run `deploy/deploy.sh` directly without the user
typing a password each time. Until that setup is done (or on a box where it
hasn't been applied), `sudo` there prompts interactively and Claude Code
(no TTY) can't answer it — hand the script, or the manual steps below, to
the user to run themselves.

Manual equivalent, if you ever need to do it by hand instead of via the script:

```bash
sudo cp -r ~/comfy-orchestrator/backend/app /opt/comfy-orchestrator/backend/
sudo cp -r ~/comfy-orchestrator/backend/requirements.txt /opt/comfy-orchestrator/backend/
sudo chown -R orchestrator:orchestrator /opt/comfy-orchestrator/backend/app /opt/comfy-orchestrator/backend/requirements.txt

# only if requirements.txt changed:
sudo -u orchestrator /opt/comfy-orchestrator/backend/.venv/bin/pip install -r /opt/comfy-orchestrator/backend/requirements.txt

# only if DB models/migrations changed:
cd /opt/comfy-orchestrator/backend && sudo -u orchestrator ./.venv/bin/alembic upgrade head

# only if frontend changed:
cd ~/comfy-orchestrator/frontend && npm ci && npm run build
sudo rm -rf /opt/comfy-orchestrator/frontend/dist
sudo cp -r ~/comfy-orchestrator/frontend/dist /opt/comfy-orchestrator/frontend/
sudo chown -R orchestrator:orchestrator /opt/comfy-orchestrator/frontend/dist

sudo systemctl restart comfy-orchestrator-api
sudo systemctl status comfy-orchestrator-api
```

## Code style

- Avoid unjustified code duplication. Before writing similar logic in a second place, check whether an existing helper already does it (or could, with a small generalization) and reuse it instead of re-deriving it inline.
- When the same piece of logic is genuinely needed in more than one spot, extract it into a shared function/class (e.g. `workflowMatching.ts`, `cropUtils.ts` on the frontend) rather than copy-pasting — even across a backend/frontend split, mirror the same computation rather than let two implementations drift.
- This applies to logic/computation, not to UI/JSX that merely looks structurally similar but serves a different purpose (e.g. a "define a new field" checkbox+label row vs a "match an existing field" dropdown row) — don't force those into one shared component just because they share a shape; only extract when the *duplicated part itself* (e.g. the actual `<select>` options/footer text) would otherwise be copy-pasted verbatim.

## Server specifics (don't rediscover these)

- Debian 13 (trixie), single-box deploy: FastAPI/uvicorn serves the API, the WebSocket progress feed, *and* the built frontend (`frontend/dist`) from one process — no nginx/redis/minio, see `deploy/debian-setup.md` for why.
- `backend/.env` (not in git, lives only at `/opt/comfy-orchestrator/backend/.env`): Postgres DB `orchestrator`/user `orchestrator`, password `2505` (home LAN only, intentionally simple). `API_TOKEN` is a random bearer token, not a login — this is single-user, no auth flow.
- `deploy/comfy-orchestrator-api.service` binds `--host 0.0.0.0` on the live unit so it's reachable from other LAN devices at `http://192.168.0.3:8000/`. The checked-in repo copy of that file still says `127.0.0.1` (the doc's conservative default) — if you ever regenerate `/etc/systemd/system/comfy-orchestrator-api.service` from the repo file, reapply the `0.0.0.0` change or LAN access breaks silently.
- `backend/requirements.txt` includes `greenlet` — required internally by SQLAlchemy's async engine even with the `asyncpg` driver. It was missing originally and broke `alembic upgrade head` with `ValueError: the greenlet library is required`. Don't remove it.
- Mid-run job recovery: there's no durable queue (see `SPEC.md` and `recover_orphaned_jobs()` in `backend/app/worker/tasks.py`) — a restart mid-generation leaves jobs needing a manual re-roll. Expected, not a bug to fix reflexively.
