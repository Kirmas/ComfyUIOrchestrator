# ComfyUI Orchestrator — context for Claude Code

Product overview & architecture: [README.md](README.md) — the source of truth now that the original `SPEC.md` design doc has been retired. That doc described the initial plan; the shipped code diverged from it in several places (in-process queue instead of Redis/Arq, local disk instead of MinIO, single systemd service instead of docker-compose), so this file plus the code — not the old spec — are authoritative.

## Grid/node domain model (read this before asking what a "chart"/"grid"/etc. is)

- **The Grid** (`frontend/src/components/Grid.tsx`) is a spreadsheet-like canvas: rows are **tracks** (`Track.row_index`), columns are **steps** (`Node.step_index`). Every column strictly alternates `asset`/`workflow` kind project-wide, starting from `Project.start_kind` (`kindForStep` in Grid.tsx mirrors `_kind_for_step` in `backend/app/api/routes/nodes.py`) — there's no per-cell choice of kind, it's dictated by column parity.
- **Node kinds**: `asset` (holds/references an image/file/mesh) and `workflow` (a ComfyUI job definition; spans multiple rows to reach its image/file input slots — see row-span below).
- **Node.node_type discriminator** (namespaced string):
  - `asset.single` — a settled asset with exactly one resolved output.
  - `asset.select` — an undecided candidates picker (several generated variants, none chosen yet); draggable/swappable as a whole unit (all its candidates move together, same as any other asset node — see Grid.tsx's `isDraggableAsset`), but never a compare source/target or a single-asset "pick cell" target (`isPickable`), since there's no single "the" picture to single out for those.
  - `asset.refasset` — a lightweight *pointer* to another node's asset (made via "+ ref elsewhere"), not a real owned `Asset` row — see `RefAssetNodeView` in `NodeCell.tsx` and `resolveSlotAsset` in `slotResolution.ts`. Its whole point is placing the same underlying asset in more than one grid cell without duplicating the file.
  - `asset.subgraph` — a *smart pointer* opening its own sub-dashboard; its picture is that dashboard's chosen result. See the sub-dashboards section below.
  - `template.<slug>` — a workflow node backed by a DB `NodeTemplate` row, created via the node-type wizard from an uploaded ComfyUI `workflow.json`.
  - `native.<slug>` — a workflow node backed by a hardcoded Python class in `backend/app/core/node_types.py`'s `NATIVE_NODE_TYPES` registry (code-only, no DB row, a closed developer-authored set). E.g. `native.character_chart` (`CharacterChartBackend`) composes 4 head + 4 body reference images into one character sheet; its param_schema declares 8 image slots, so it always wants an 8-row span. `native.crop` and `native.mask` (`CropBackend`/`MaskBackend`) are single-image-in/single-image-out editing nodes — `native.mask` paints a bilevel mask (`MaskPreview.tsx`, base64 PNG in `node.params.mask_png`, no separate Asset row) and bakes it into the source image's alpha channel matching ComfyUI's own clipspace mask-editor convention (painted = transparent hole). `native.transplant` (`TransplantBackend`) is the two-slot member of that family: target on top, source underneath, and a `layer_mask` param (same params-stored bilevel PNG) saying where the source shows through — for recovering detail an otherwise-good regeneration lost. Its editor (`TransplantPreview.tsx`) stacks the two as layers and paints holes in the top one; the brush/undo/export engine both mask editors share lives in `components/paintMask.tsx`, so a third one shouldn't re-implement it. `GET /api/node-templates` merges native + DB templates into one list so the frontend doesn't need to know which is which.
- **Asset kinds are polymorphic, not `if`-chains.** Behavior for the four `asset.*` kinds lives in one class each — `backend/app/core/asset_types.py` (`AssetNodeBackend` + `ASSET_NODE_TYPES`, the asset-side counterpart of `node_types.py`'s `NATIVE_NODE_TYPES`) and its frontend mirror `frontend/src/assetNodes.ts` (`AssetNodeKind` + `assetNodeKind()`). Only two things actually differ per kind, and that *is* the interface: **`face`** — the one Asset the cell stands for (own selected/latest output for `single`/`select`; the explicit-ref target for `refasset`; `Dashboard.result_asset_id` for `subgraph`) — and **`owns_assets`** — whether `Asset` rows hang off the node at all, which deletion, storage GC and `GET /nodes/{id}/outputs` key on. Everything else (pickability for compare/"pick cell", the shared `AssetFaceView` thumbnail with zoom + compare, `FullSizeModal`) is one implementation in the base class or one shared component, deliberately: the old per-call-site `if node_type == "asset.refasset"` chains had already drifted — `list_node_outputs` and `isPickable` grew a refasset branch but never a subgraph one, so a smart pointer rendered a picture that nothing else in the app would accept as one. Adding a fifth asset kind should mean one new class on each side, not a new branch anywhere.
- **Row-span paradigm**: there is no display-only/cosmetic position override anywhere — a node's rendered position is always exactly its `track_id` + `step_index`. A workflow node's desired row-span is the number of image/file fields its template declares (`slotFields()` in `templateUtils.ts`); "moving"/"resizing" it means actually reassigning `track_id`/`step_index` on it and its dependents (`applyRowMove`/`applyColumnMove`/`applyDiagonalMove` in Grid.tsx), never a visual-only change.
- **A workflow's materialized output is rigidly bound to its creator** (`Node.created_by_node_id`): it can only sit at `creator.step_index + 1`, in a row within the creator's own span or a track spawned from it — enforced both in Grid.tsx's `isPositionAllowedFor` (UI-facing fast path) and, authoritatively, the backend's `_ensure_output_binding` in `api/routes/nodes.py`. When moving a workflow node together with its dependents, the workflow's own PATCH must be sent before its output's PATCH, or the backend still sees the old creator position and 409s "can only move among its own creator's positions" (2026-07-18 incident, fixed by putting the workflow node first in all three move functions' ordering).
- **Compare** (`CompareModal.tsx`, overlay slider between two resolved images) and **reference/refasset** ("↗ Reference") are two unrelated features that happen to both involve picking another node — don't conflate them.
- **Copying vs referencing is split by kind, on purpose.** An asset is *referenced* into a second cell (`asset.refasset`, one picture in two places — never duplicated). A workflow node is *copied* ("⧉" on its card → click an empty workflow cell → `POST /api/nodes/{id}/duplicate`), producing a second independent node with the same template and every local setting (params/slot refs/variants/backend/use_api) but none of the original's results — two cells can share one picture, but not one set of parameters. Don't add a "reference a workflow" or a "duplicate an asset" path.
- **Nothing creates a grid cell the user didn't ask for.** Settling a candidate used to also create the empty workflow cell after it (`_ensure_next_workflow_step`, removed 2026-07-29 — it was deleted by hand ~90% of the time). Every asset cell with a free next column already offers its own "+ step" (`assetNextStepCells`), so continuation is one click when it's actually wanted; don't reintroduce speculative auto-creation.
- **Any modal opened from `NodeCell.tsx` must render via `createPortal(..., document.body)`**, never inline in the cell's own JSX. A cell sits inside Grid's pan/zoom CSS transform (`Grid.tsx`); per the CSS transform spec, any ancestor transform (even a no-op `scale(1)`) establishes a new containing block for `position: fixed` descendants, so an inline (non-portaled) `.image-modal-backdrop` silently stops being viewport-relative and can open hundreds of pixels off-screen — only reproduces once the modal's content is tall enough to need `.params-modal-content`'s own scroll area, which is why it went unnoticed until `native.mask`'s paint canvas hit it (2026-07-21 incident; the params-modal `paramsOpen` block was the one `.image-modal-backdrop` usage in the file that wasn't portaled, unlike the other three).
- **Portaling a modal to `document.body` does *not* remove it from Grid's drag-to-pan event handling.** `onBackgroundPointerDown` (Grid.tsx) arms a `window`-level pan drag on any pointerdown that bubbles up to the grid container without matching its exclusion selector — and React's *synthetic* event bubbling follows the React tree, not the real DOM tree, so a portaled child (still a React descendant of Grid) bubbles a pointerdown up to it regardless of where it actually sits in the DOM. Interactive content inside any modal (e.g. `MaskPreview.tsx`'s paint canvas) must therefore be covered by the `.image-modal-backdrop` entry already in `onBackgroundPointerDown`'s `closest()` exclusion list, or a pointerdown inside the modal visibly pans/drags the grid underneath it (2026-07-21 incident, found right after the createPortal fix above while testing `native.mask`'s paint canvas).

## Sub-dashboards & smart pointers (`backend/app/api/routes/dashboards.py`)

A project is not one grid. A character is a dozen-plus separate pieces (the
character chart, an armour chart, a weapon chart…), so a grid can contain a
**smart pointer** — an `asset.subgraph` node — that opens its own separate
grid. This is *organisational decomposition*, not a performance trick.

- **A scope is (project, dashboard).** `Track.dashboard_id IS NULL` means the
  project's main grid, which is why this shipped with **no data migration** —
  every pre-existing track reads as "main" for free. `core/track_order.py`'s
  linked list is unchanged; only the key it filters by moved (`scope_of(track)`
  is how callers should derive it). Each scope has its own list head, its own
  `start_kind` origin (`core/grid_scope.py`), and its own layout.
- **A pointer is a reference, not containment.** Several pointers may open one
  dashboard, and a loop (A→B→A) is legal — diving in is one click and you come
  back through **navigation history** (`navStack`), not through structure. There
  is deliberately no menu listing every dashboard.
- **Reachability is structural, via ownership.** `Dashboard.owner_node_id` is
  the *main* pointer. A subgraph node can never leave the dashboard it was
  created in (`_ensure_same_scope`), and the main pointer can't be deleted while
  its dashboard holds anything — so main pointers form a spanning tree rooted at
  the main grid. Extra pointers are non-tree edges, hence always safe to delete.
  Deleting the main pointer of an *empty* dashboard auto-promotes another
  pointer, or deletes the dashboard if it was the last one.
- **Ownership can be transferred**, guarded by `_owner_chain_reaches_root`:
  walking the *ownership* chain (one FK per hop, not the pointer graph, which
  may loop). It rejects only the genuine hazard — handing a dashboard's
  ownership to a pointer reachable solely *through* that dashboard, which would
  close the chain into a ring cut off from the main grid. Legitimate cross-links
  are unaffected.
- **Cross-scope single-node moves are rejected outright.** Positions only mean
  something inside one grid, so relocating one node across scopes would strand
  its creator/output binding. Moving finished work between dashboards is meant
  to be a deliberate whole-track operation (not built yet).
- **An MCP agent reads a sub-dashboard the same way the frontend does: pass
  its id, don't look for a separate tool.** `get_project_recipe`/`list_tracks`/
  `create_track` (mcp/tools.py) take an optional `dashboard_id`, mirrored
  straight through to `GET /api/projects/{id}/recipe|tracks` and
  `POST /api/tracks`, which already had the parameter for the frontend's own
  navigation. A `recipe` read also now echoes each node's
  `subgraph_dashboard_id` (previously only on `GET /api/nodes/{id}`), so an
  agent can spot every nested pointer in one call instead of probing each
  asset node individually, and get the value to pass back in. Board tools
  (`get_board`, `list_board_items`) are unrelated -- a dashboard is a grid
  scope, not a `Board` row, and never accepts a dashboard id (2026-08-09
  gap report; fixed same day).

## Idea board (`backend/app/api/routes/boards.py`, `frontend/src/components/Board.tsx`)

Pre-production — the idea, the references, the divergence — lives on a separate
**Board** view per project, not in the grid (`roadmap.md` §1 explains why this
reversed the earlier "everything in the grid" decision). The grid is convergent
by construction; a **sticker's `x`/`y` is its only truth**, nothing derives or
validates it, which is the exact opposite of the grid's rule that a node's
position is always `track_id` + `step_index`. Don't "fix" one to match the other.

- A `BoardItem` holds **exactly one content type** (`kind`): `text` (markdown),
  `image`/`audio`/`video` (→ a project asset), `frame` (the lasso, rect or
  ellipse), `ink` (freehand, no semantics, erased per-stroke), `connector`
  (anchored to two items so it follows them), `comment` (anchored to one).
  Connectors/comments are `board_items` rows with self-FKs, not their own table,
  so one request loads a whole board and both cascade when their anchor dies.
- **`Asset.project_id` vs `Asset.node_id`**: alternatives, never both.
  `node_id` is `ondelete=CASCADE`, so an asset owned by a grid cell would be
  destroyed when that cell is deleted. The board owns library assets; **the grid
  only ever references them** (`asset.refasset` with
  `inputs=[{type:"explicit", output_id}]` and no `node_id` — both
  `_explicit_ref_asset` and `resolveSlotAsset` already resolve by asset id
  alone). There is deliberately **no "send a generated output to the board"**
  direction; that was the only thing that made this hazardous.
- **Prompt bridge**: `core/idea_macros.py` strips markdown and expands `{tag}`
  macros against the project's tagged text stickers. It runs at **run time in
  `resolve_node_inputs`**, on the node instance's own text params — never in a
  capability's baked `workflow_json`, which is global and would leak one
  project's character description into every other project using it. Tags are
  unique project-wide (enforced in `boards.py`, on top of a per-board DB
  constraint) because a macro resolves against the project. An unknown `{tag}`
  is left **literal**, never expanded to empty — a quietly emptied prompt
  generates the wrong thing unnoticed. `POST /api/projects/{id}/resolve-macros`
  serves the node-config preview from that same function so preview and run
  can't drift.
- `frontend/src/markdown.ts` is a deliberately tiny escape-then-format renderer,
  not `marked`: a real renderer passes raw HTML through and would need a
  sanitizer too — two dependencies to un-bold a sticker, on a box where
  `vite build` has been OOM-killed.
- A sticker's root carries `board-kind-<kind>`, **not** `board-sticker-<kind>`:
  the latter collides with `.board-sticker-text`, the class of a note's own body
  field, so the root of every text note matched every rule and selector meant for
  the field — including the "don't start a drag here" list, which made text notes
  the only kind that could not be dragged or selected at all.
- Empty canvas is `.board-plane` (or `.board-svg`), never the container element:
  the plane covers the container edge to edge, so `e.target === containerRef.current`
  is never true and any check written that way silently disables itself.
- Board connectors are drawn from stored coordinates, **not** via
  `ArrowsOverlay.tsx` (it measures the DOM and re-polls every 500 ms — fine for
  the grid, visibly laggy while dragging). `usePinchPan` is shared, generalized
  with `{minZoom, maxZoom, panAtMinZoom}`; the board is the one caller that pans
  at min zoom.

## MCP server (`backend/app/mcp/`)

An agent-facing MCP server runs **inside the same FastAPI process**, mounted at
`/mcp` (streamable HTTP), behind the same bearer `API_TOKEN` — `core/auth.py`'s
prefix check covers `/mcp` explicitly, which matters because the unit binds
`0.0.0.0`. Its tools live in `app/mcp/tools.py` and almost all of them call this
app's **own REST API in-process** via `app/mcp/client.py` (`httpx.ASGITransport`),
so route validation stays the single source of truth instead of being mirrored.

Two mounting gotchas, both already fixed — don't reintroduce them:
- `FastMCP(..., streamable_http_path="/")`. The default is `/mcp`, which lands at
  `/mcp/mcp` once the sub-app is itself mounted at `/mcp`.
- A Starlette `Mount` only matches paths *below* its prefix, so exact `/mcp` never
  reaches it and falls through to the frontend's catch-all `StaticFiles` mount,
  which answers POST with **405**. `main.py` adds an explicit 307 redirect for the
  no-trailing-slash form. This only reproduces when `FRONTEND_DIST_DIR` is set —
  i.e. on prod but not on a bare dev run.

`mcp==1.28.1` is pinned deliberately: it's the first release with all published
transport advisories closed (unverified-session + DNS-rebinding in 1.27.2,
Host/Origin validation in 1.28.1). It requires `pydantic>=2.11`; pulling it in
also moved `fastapi` to 0.140.7 / `starlette` to 1.3.1, since starlette 0.41.x
carried seven unfixed advisories (including an SSRF in `StaticFiles`, which this
app uses to serve the frontend). `Pillow`/`python-multipart` were bumped for the
same reason. Note fastapi 0.140 represents included routers as `_IncludedRouter`
objects rather than flattening them into `app.routes` — route *counts* look
wildly different, but routing is unchanged.

## Dev environment (`backend/.venv` + `backend/.env`, both gitignored)

The dev copy is runnable now: a venv plus a `.env` pointing at
`sqlite+aiosqlite:///./dev.db` (the app supports SQLite as a documented
fallback). Build the schema with `Base.metadata.create_all` rather than
`alembic upgrade head` — the migration chain isn't SQLite-clean (0010 does an
ALTER of constraints, unsupported there). Set `FRONTEND_DIST_DIR` in dev too, or
prod-only routing bugs like the `/mcp` 405 above stay invisible.

Migrations are still validated against the real Postgres with the
`BEGIN … ROLLBACK` technique (`alembic upgrade X:Y --sql`, swap COMMIT for
ROLLBACK, probe the new tables inside the transaction) before any deploy.

`scripts/mcp_smoke_test.py` drives a scratch project end-to-end through the MCP
tools and deletes it afterwards:
`backend/.venv/bin/python scripts/mcp_smoke_test.py http://127.0.0.1:8011 dev-local-token`

## Where this code runs

This working copy (`~/comfy-orchestrator`) is the **development copy**, owned by `keresh`, editable directly (no sudo). It is separate from the **live production copy** at `/opt/comfy-orchestrator`, owned by the `orchestrator` system user, run as systemd unit `comfy-orchestrator-api`. The two are not symlinked or synced automatically — deploying means copying dev → prod on purpose (see below).

This is a git repo (`main` branch, remote on GitHub) — use `git diff`/`git log` freely to see what changed. Commits happen only when the user asks; pushes likewise.

## Deploying a change (dev copy → live service)

`deploy/deploy.sh` scripts this (build frontend, sync backend + frontend
into `/opt/comfy-orchestrator`, `pip install`, `alembic upgrade head`,
restart the unit).

**`deploy/deploy.sh backup` takes a snapshot** (pg_dump `-Fc` + media dir +
prod `.env`) into `~/media/comfy-orchestrator-backups/<timestamp>/`, keeping
the last `BACKUP_KEEP` (10), and writes a `RESTORE.txt` next to each. Run it
before anything structural — a migration touching tracks/nodes, or a refactor
of the track ordering: the finished charts in the DB and the generated media
are not reproducible. It self-verifies (`pg_restore --list` must parse the
archive; the media file count is compared against `assets` rows) and fails
loudly rather than leaving a dump nobody checked. `~/media` is a separate
466 GB disk (`/dev/sdb`) — deliberately *not* `/var/backups`, both because
`/var` is its own ~8 GB LV that also holds `/var/lib/postgresql` (growing
snapshots would fill it and take the database down) and because sdb is a
different physical disk from the sda holding the live media and DB.

`root-deploy.sh` syncs `backend/app/`, `backend/alembic/`
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
- Mid-run job recovery: there's no durable queue (see `recover_orphaned_jobs()` in `backend/app/worker/tasks.py`) — a restart mid-generation leaves jobs needing a manual re-roll. Expected, not a bug to fix reflexively.
