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
- **Comments are threads, one per set of cells** (`Annotation` + `AnnotationMessage`; `routes/annotations.py`, `AnnotationThread.tsx`). A message's `source` (user/agent) is its author for good, and only the author may edit it. "Done" is `resolved_at`, never deletion, and any new message reopens the thread. An MCP agent writes in the same thread the person does (`flag_cell`/`reply_to_flag`/`resolve_flag`/`edit_flag_message`) and deliberately has no delete tool. Until 2026-09-27 a frame held a single text field, so a night of agent↔person back-and-forth in Nature Spirit ended up as the person's replies typed over agent comments (still labelled `agent`) plus a stack of "done" frames on each cell; migration 0022 merged those back into threads.
- **Nothing creates a grid cell the user didn't ask for.** Settling a candidate used to also create the empty workflow cell after it (`_ensure_next_workflow_step`, removed 2026-07-29 — it was deleted by hand ~90% of the time). Every asset cell with a free next column already offers its own "+ step" (`assetNextStepCells`), so continuation is one click when it's actually wanted; don't reintroduce speculative auto-creation.
- **Any modal opened from `NodeCell.tsx` must render via `createPortal(..., document.body)`**, never inline in the cell's own JSX. A cell sits inside Grid's pan/zoom CSS transform (`Grid.tsx`); per the CSS transform spec, any ancestor transform (even a no-op `scale(1)`) establishes a new containing block for `position: fixed` descendants, so an inline (non-portaled) `.image-modal-backdrop` silently stops being viewport-relative and can open hundreds of pixels off-screen — only reproduces once the modal's content is tall enough to need `.params-modal-content`'s own scroll area, which is why it went unnoticed until `native.mask`'s paint canvas hit it (2026-07-21 incident; the params-modal `paramsOpen` block was the one `.image-modal-backdrop` usage in the file that wasn't portaled, unlike the other three).
- **Portaling a modal to `document.body` does *not* remove it from Grid's drag-to-pan event handling.** `onBackgroundPointerDown` (Grid.tsx) arms a `window`-level pan drag on any pointerdown that bubbles up to the grid container without matching its exclusion selector — and React's *synthetic* event bubbling follows the React tree, not the real DOM tree, so a portaled child (still a React descendant of Grid) bubbles a pointerdown up to it regardless of where it actually sits in the DOM. Interactive content inside any modal (e.g. `MaskPreview.tsx`'s paint canvas) must therefore be covered by the `.image-modal-backdrop` entry already in `onBackgroundPointerDown`'s `closest()` exclusion list, or a pointerdown inside the modal visibly pans/drags the grid underneath it (2026-07-21 incident, found right after the createPortal fix above while testing `native.mask`'s paint canvas).
- **A dead end worth recording so it isn't re-chased:** 2026-09-14, dragging a node made the whole tab stop responding to everything (not just the app) -- DevTools' own pause button did nothing until an unrelated click delivered a pointerup, meaning no page JS was running at all during the "freeze". That rules out a render loop by definition; a real CPU profile from the same session did point at one genuine (separate, now-fixed) cost — `ArrowsOverlay`'s 500ms poll unconditionally calling `setSize`/`setPaths` even with zero edges to draw, forcing a reflow for nothing — but fixing that didn't end the freeze either. The leading theory was a transformed ancestor wedging native HTML5 drag-and-drop at the browser level (`grid-wrapper` always carries `transform: scale(zoomScale)`, even `scale(1)`, and Chromium has known quirks with DnD hit-testing under a transformed ancestor); an experimental build that omitted the transform at 1:1 zoom was deployed to test it, then reverted unconfirmed once a full **browser restart** (not just F5) cleared the freeze on its own -- meaning the wedged state lived in the browser process itself, not in this app's code, and there was nothing left here to fix. If this recurs, the transform-ancestor theory is still the best lead and the experimental patch is easy to redo, but don't assume it's the same bug without checking "does pausing the debugger do anything" first.

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
- **Copying a subgraph is split three ways, by what the thing is**
  (`POST /api/dashboards/{id}/copy` -> `core/subgraph_copy.py`). *Structure*
  (tracks, their order, step indices) is copied, and that IS the wiring --
  slot inputs are positional (`cell_index`), so nothing needs remapping.
  *Settings* (a workflow's template/params/slot refs/variants/backend/use_api)
  are genuinely copied, by the same `workflow_node_copy` the single-cell "⧉"
  gesture uses. *Content* is never copied: an asset cell that owns a picture
  comes across as a refasset on that same picture, a nested pointer as a second
  pointer at the same dashboard (no recursion), and each kind answers for
  itself via `AssetNodeBackend.copy_spec` -- the third member of that
  interface. And a workflow's own materialized output (`created_by_node_id`
  set, written in exactly one place, so it's precisely the "this is a generated
  result" flag) is **not reproduced at all**: it leaves a hole, deliberately,
  to be re-generated or re-wired left to right. A hole costs nothing because
  positional resolution tolerates an empty cell and picks up whatever is
  generated there later. The new dashboard inherits `start_kind` (column parity
  is per scope and step indices are copied verbatim) but not `result_asset_id`.
  Contents are copied *before* the target cell becomes a pointer, or a copy
  placed inside the dashboard being copied comes out holding a pointer at
  itself.
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
- **Until 2026-09-14, MCP had no tool that actually created a sub-dashboard.**
  `create_node(node_type="asset.subgraph")` made a node, but its
  `subgraph_dashboard_id` stayed permanently null -- a dead pointer, since the
  only thing that ever creates the nested `Dashboard` row is `POST
  /api/dashboards` (dashboards.py), which nothing in `mcp/tools.py` wrapped.
  Fixed by adding `create_dashboard`/`get_dashboard`/`rename_dashboard`/
  `copy_dashboard`/`add_pointer`/`set_dashboard_result`/`transfer_ownership` as
  thin wrappers over the existing routes, same pattern as every other tool
  here -- no new backend logic, just the missing surface. `create_dashboard`'s
  `name` also happens to be the only place in this app a grid *container* gets
  a name at all (a `Node`/`Track` has no name column); naming an individual
  node or track is a real gap, not an MCP one, and remains unsolved.
  `delete_node` is available for agent-created nodes (`is_agent_created=True`)
  so agents can clean up their own mistakes, but deletion is rejected if the
  user has verified/unmarked the node or created it manually. Still no
  `delete_dashboard`/`delete_track` tool. Found and fixed same day: `get_project_recipe` also used to
  inline `native.mask`/`native.transplant`'s `mask_png`/`transplant_png`
  base64 blobs (params of schema type `"mask"`/`"layer_mask"`) straight into
  every node's `params`, which is exactly the megabyte-blowup
  `list_node_types` already excludes baked defaults to avoid (see its
  docstring) -- a project with many painted cells could push one `recipe` read
  past several million characters. The route now reports only whether such a
  field is set, never its bytes.

## Live Blender scenes (`backend/app/core/blender_scene.py`, `api/routes/scenes.py`, MCP `scene_*`)

A **scene** is a workshop an agent opens over some assets, works in for as long
as it needs, takes a result out of, and closes. One background Blender per scene
(`blender_scene_server.py` runs *inside* it, listening on a unix socket and
`exec`ing code in one persistent namespace), so 100 small edits cost ~8 ms
total instead of a Blender start-up each. This replaced the earlier `native.blender_script` node (GLB + a Python
script in, GLB out, one stateless Blender per run -- removed 2026-11-20): a
10-second animation would have needed dozens of such nodes, with no way to
look at the result between them.

- **State is files, not DB rows.** `<cache_dir>/scenes/<id>/` (`meta.json`,
  `scene.blend` checkpoint, `in/`, `renders/`, `blender.log`). No migration,
  nothing in the media dir (backup/GC treat every file there as an asset). A
  scene is disposable by design; what is worth keeping leaves through
  `export`, which makes an ordinary library `Asset` (`node_id` None, like an
  upload) -- the grid then references it (`place_reference_asset`).
- **Lifecycle is enforced by the manager, never trusted to the agent**: a
  checkpoint `.blend` is saved after every successful call; idle close after
  `blender_scene_idle_s` (checkpoint kept, deleted after
  `blender_closed_keep_days`); any call on a closed scene reopens it; at most
  `blender_max_scenes` open at once. A deploy restarts the API unit and systemd
  takes the Blender children with it, so a scene is "closed, checkpoint intact"
  afterwards (`recover()` at startup).
- **Memory is the real constraint** (measured, 10 GB box): a 6M-triangle mesh
  costs 2.1 GB to import and 5.7 GB to decimate inside Blender, which **never
  gives memory back**. Hence: inputs over `blender_simplify_over_tris` (1M) are
  simplified to ~300k *before* import, in a subprocess
  (`core/mesh_simplify.py`, numpy GLB parser + `fast-simplification`; 6M->300k
  in 5 s; single plain mesh only -- anything rigged/animated/multi-part is left
  alone and the input's `note` says why); a scene above `blender_scene_soft_mb`
  is restarted from its checkpoint after a call (the reply says `recycled`;
  python variables are lost, the scene is not); above `blender_scene_hard_mb`
  it is killed *mid-call* by the 2 s sweeper; and below
  `blender_emergency_free_mb` of system memory the largest scene is killed to
  protect the API. Render defaults to **Workbench** (full 6M mesh in 0.8 s /
  1.2 GB; EEVEE needed 5 s and was what killed the old 5.8 GB VM).
- **The agent's code runs as the service user.** The child env is stripped
  (no `API_TOKEN`/DB URL) and `HOME` is the scene dir, but it can still read
  files the service can -- the person accepted this knowingly. Don't treat `scene_run` as sandboxed.
- **Hard-won correctness details** (each found by a test, don't undo):
  * Setting a camera's `location`/`rotation` does **not** update
    `matrix_world` until the depsgraph re-evaluates -- `set_view` calls
    `view_layer.update()`, otherwise `verts_under_mask` projects through the
    *previous* camera pose and selects the wrong vertices (looked like a
    working edit that moved a nose instead of an ear).
  * `verts_under_mask` only reaches the **camera-facing** surface (z-buffer
    visibility, vectorised -- a per-vertex ray cast is far too slow on
    millions of vertices). A mask is a "where" hint; the agent must `grow()` +
    `falloff()` before deforming or the back of a part is left behind.
  * Set render resolution *before* `set_view`/`render_to` (framing depends on
    aspect) and run `verts_under_mask` with the same camera/size the mask was
    painted on.
  * The checkpoint save happens *outside* the stdout capture so Blender's
    "Saved copy as ..." doesn't pollute what the agent reads; tracebacks drop
    the server's own frames and quote the agent's lines via `linecache`.
  * Never run two API processes over one `scenes` dir: each sweeper enforces
    its own limits on *every* scene it sees (a second dev server with a
    700 MB cap silently killed the first one's scenes -- 2026-11-20).
- **Hands** (2026-11-21): agents animate bodies fine but kept botching fingers --
  bone roll differs per rig, so a guessed local axis curls a finger sideways or
  backwards, and a hand is a few pixels on a full-body render. Hence, all in
  `blender_scene_server.py`: a per-hand frame (along / across-to-thumb / out of
  the palm) found from the skeleton, never from bone axes or names (names only
  help say which chain is which finger; unnamed rigs are classified by
  geometry; palm side from the rest pose, `palm_flip` overrides);
  `pose_hand`/`pose_finger` (curl 0..1, spread) built on it; `pose_report` /
  MCP `scene_pose` reads bends back signed (negative = backwards) with
  warnings and prop contacts; `scene_render(focus=<bone>)` closes up on a bone
  per frame with hand-relative views and a near clip so a thigh can't hide a
  palm. The hand view is `back_of_hand`, not `back` (that name is a world
  view). Pose bones come back as a **new Python wrapper on every access** --
  compare by `.name`, never `is` (an `is` check walked finger chains up to the
  root and the thumb preset rotated the whole arm).
- `scripts/blender_scene_smoke_test.py http://127.0.0.1:8011 dev-local-token`
  checks the whole thing end to end (builds its own 1.2M-triangle sphere and a
  painted mask); `--extras-only --idle --hard` against a server started with
  `BLENDER_SCENE_IDLE_S=6 BLENDER_SCENE_HARD_MB=700` checks idle close and the
  memory kill.
- **The Scenes page** (`frontend/src/components/Scenes.tsx`, tab "Scenes"): every
  scene with its picture, inputs (and the simplification note), RAM now/peak,
  disk size, last activity, and Details / Close / Delete (+ "delete all
  closed") -- so nothing ever needs an SSH session to clean up. The tab exists
  only when `GET /api/scenes` says `available` (Blender found), same rule as the
  Blender node type. Picture URLs carry `?token=` like assets (an `<img>` can't
  send a header) and are added by the route **only for the page, not for an MCP
  agent's calls** (`X-Actor: agent`) -- an agent has no use for a URL and
  shouldn't get the token in its context. "Details" is disabled for a closed
  scene because `GET /{id}/info` *reopens* it (it asks the live Blender).
  Verified without a browser (none can run on this box: no libnss3) by
  server-rendering the components with real API data in both languages.
## FBX meshes (`core/fbx_convert.py`, `core/blender_fbx_to_glb.py`, `core/mesh_view.py`)

An FBX is stored untouched (`kind=mesh`, mime `model/fbx`, found by signature) and shown through a GLB that a background Blender converts once and `mesh_view` caches (`/view`, `/view?in_place=true`, `/view-info`). Blender's importer applies the file's units/axes; the glTF exporter writes metres/Y-up -- no user choice, by design. Kept: mesh, skeleton, skinning, materials, every clip (each action stacked as an NLA track). A file with a skeleton but no mesh gets thin skinned "sticks" along the bones. `in_place` flattens the root's *horizontal* location curves (horizontal judged from the bone's own rest orientation, not assumed x/y). A clip of one frame is a pose, not an animation (the viewer ignores it). A file may hold several models (the "cat" had two overlapping meshes + armatures): `view-info.meshes` feeds a dropdown that only hides nodes. Vertex counts come from the converted GLB, built in a background thread by `warm()` when a list read finds none. Pitfalls found: a new `BMVert.index` is -1 until `index_update()`; bones of an imported skeleton-only file are non-deforming and the exporter then drops the skin. No FBX export, no ABC, no retargeting (decided).

## Alembic (`core/abc_convert.py`, `core/blender_abc_to_glb.py`, `core/glb_writer.py`)

`.abc` (modern Ogawa container only, by signature; mime `model/alembic`) is stored untouched and viewed through a GLB from a background Blender, same pattern as FBX (`core/blender_convert.py` is the shared runner). It is a per-frame cache with no rig/materials; V1 shows the FIRST frame only. Meshes -> triangles, loose verts -> points, **hair (Blender `CURVES`) -> one LINES primitive per groom, written by our own `glb_writer.py`** because Blender's glTF exporter drops curves. Grooms are thinned for display: strands resampled to the same point count, ordered so ANY PREFIX is an even spatial sample (Morton order of the roots, then bit-reversed -- measured better than random, and far better than "first N in file order", which bald one side), and the primitive's glTF `extras` carry `{strands, strand_points, total_strands}`: the viewer's hair slider just sets the draw range (`applyHairRange`, `Mesh3D.tsx`), no reload. Only 25% (max 60k) of a big groom is sent. Strands are drawn **translucent** (alpha 0.2, `alphaMode BLEND`) with one colour per groom: opaque 1-px lines overlapped into a solid silhouette even at 2%. The two real groom files tested (93k strands, 250 frames declared) turned out to be **static** (file size = one sample; Blender sees zero movement), so animation was not built. Hair width (`radius`, 0..0.0055 in scene units) is ignored: it is far below a pixel; ribbons would cost ~3.5x the line data and look wrong edge-on.

**Cache gotcha (2026-11-22):** `/view` is served `Cache-Control: immutable`, so a changed converter's output under the same URL is never fetched by a browser that already has the old one (the groom stayed one flat colour). `mesh_view.VIEW_VERSION` names the cache dir AND is the `?v=` of the view URL; **bump it whenever a converter's output changes**. The frontend's thumbnail-still key includes it too. Blender scenes open and export every shown format (glb, fbx, stl, abc; obj export-only): hair survives only an `abc` export.

## Agent task node (`native.agent_task`, `backend/app/core/agent_task.py`)

A node that hands its job to an agent instead of running a model -- "an
alternative to chat" in the grid. All six inputs (`input_1..6`, type `file`,
optional) take any asset; the person writes `instruction`; running it opens an
ordinary **project chat** (visible in the Agent tab, followable and stoppable
there), gives it the task and the input assets' **ids**, and waits. Whatever the
agent passes to the MCP tool `finish_task` becomes the node's output (several =
an `asset.select` picker). The node never touches Blender itself; the agent
uses the same MCP tools any project chat has (`scene_*`, `view_asset`).

- **How it plugs into the worker** (`worker/tasks.py run_variant_job`), via three
  opt-in attributes on a backend class rather than a special case: `agentic`
  (slots arrive as `AssetInput` ids, not bytes -- no 140 MB mesh loaded just to
  pass its id -- plus a `_task` dict with node/project/job), `job_timeout_s`
  (replaces the 10-minute network budget; the node's own `timeout_min` is what
  really bounds a run) and `retryable = False`. **Automatic retries are off
  on purpose**: a retry would start a second agent on the same job and spend
  tokens twice.
- `submit()` returns at once with the **chat id as the job's external id**
  (so the page can link a running node to its chat) and a background watcher
  polls the chat every 3 s: finished by the agent's `finish_task`; one
  **nudge** if the turn ends without it; then error. Also errors on: chat
  ended as error/stopped, `timeout_min` (chat is stopped), an approval nobody
  can give (120 s, chat stopped), runner unreachable 5x in a row.
- **The agent's reason lives on the Job, not the node**: the node's own message
  is the generic "all variants failed", so `NodeCell.tsx` shows
  `jobs[0].error` for agent tasks and loads jobs for a *finished* one too (they
  are otherwise read only while running) -- the chat link and the reason must
  survive a reload.
- **Result copy never deletes an input.** `result()` copies each handed-in
  asset into the node and deletes it afterwards only if it is the agent's own
  temporary export (`meta.source == "blender_scene"`, no node); any other
  asset -- above all an *input* the agent passed straight back ("no change
  needed") -- is only copied. The asset delete route destroys the file
  unconditionally, so this guard is the only thing standing between an agent
  and the person's source asset.
- State is in memory (`TASKS`). A deploy mid-task orphans the job (the usual
  "manual re-roll" rule) but not the chat -- the runner is a separate service;
  the agent's later `finish_task` answers "no running task" and says so.
- **Cancelling works for native nodes now**: `cancel_job` used to stop the
  worker's task and call `ComfyUIBackend.cancel` only when the job had a backend
  row, so a cancelled agent task kept running (and spending) -- found by the
  smoke test; it now calls the native backend's `cancel` too.
- Tests: `scripts/agent_task_unit_test.py` (watcher logic with a scripted chat,
  fast) and `scripts/agent_task_smoke_test.py` (starts its own dev server +
  `scripts/fake_agent_runner.py`, which *plays the agent over the real MCP
  protocol* against live Blender scenes: result, input-not-deleted, agent says
  "can't", agent goes silent -> exactly one nudge, cancel -> chat stopped).
  Stop other dev servers first (two API processes over one scenes dir fight).
- Not built yet: the studio (view/angle/mask on the page).

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

**One editor for a capability's text fields, not two.** `set_prompt` writes
the literal in one capability's ComfyUI `workflow_json` (redirected through
`prompt_leader_id` if this instance follows another's prompts) — genuinely
consulted at run time (`worker/tasks.py`'s `resolve_node_inputs`/
`core/template_engine.build_workflow`) for ANY untouched param, text or not,
mapped to a param_schema field or not: `build_workflow` only overwrites a
mapped input if `Node.params` already has that key, otherwise the graph's
own literal survives untouched. `set_node_params` only matters once an
instance's `Node.params` actually carries the key — before that, the graph
literal still wins regardless of what's in `param_schema`.
`find_editable_text_fields` (`core/workflow_analyzer.py`) reflects this: it
returns every text-shaped literal in `workflow_json`, marking
`is_variable=true` when the same (node_id, input_key) is also a param — that
flag is purely informational (so an editor can warn "a cell with its own
value won't be affected"), not a fork in which tool to use.

There used to be a second, parallel path here: a promoted field excluded
from the baked list and redirected to `PATCH .../variable-default`, which
wrote `NodeTemplate.param_schema`'s own field `default` instead. Traced and
confirmed **dead** for generation (2026-08-09) — `resolve_node_inputs` never
read it, `NodeCell.tsx`'s text field rendered `?? ""` not `?? field.default`,
and `chooseTemplate` seeds a fresh node's params from `NodeTemplate.defaults`
(a different, flat dict, always `{}` from `create_node_type`/MCP-authored
types) — never from any per-field `default`. It only ever changed what
`CapabilityTextFieldsModal` *displayed*, despite the UI's own hint string
claiming otherwise. Removed outright the same day, route and UI branch both
(`update_variable_default`, `CapabilityVariableDefaultUpdate`,
`variable_text_fields()`) rather than wired up, once it was clear the
original split ("baked" stops applying once a field is promoted, the
promoted field's own default takes over) had never actually been finished on
the generation side, and the literal was already the one thing that worked
for both cases anyway. An MCP tool for the dead path (`set_node_type_default`)
was briefly built, then removed the same day once this traced through.

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

## Agent chats (`agent_runner/`, `routes/agent_chats.py`, `AgentChat.tsx`)

The **Agent** tab has two kinds of chat with a Claude Code CLI agent: a
*project* chat, bound to the current project and working only through this
app's own `/mcp` (no shell, no files), and a *dev* chat, which is Claude Code
in this very dev copy -- it can change and deploy the site, and whatever its
permission rules don't allow comes to the page as Allow / Deny buttons
(opt-in via `RUNNER_DEV_ENABLED=1`; one dev chat runs at a time, and a dev
chat shares this working tree with any terminal session, so don't run both on
the same change). A dev chat can run on **Claude Code, Codex or Antigravity** (one `Provider` class each in `agent_runner/providers.py`, chosen by the brand-icon buttons next to "+"; project chats can use any of them too -- Codex in a read-only sandbox with only the orchestrator's MCP, Antigravity edits-only in one fixed folder -- but only Claude can be limited to nothing but MCP and ask Allow / Deny; the other two run headless with a fixed permission policy per chat, and night reports stay on Claude). The sessions live in a **separate user-level service, `comfy-agent-runner`**
(runs as keresh, `127.0.0.1:8765`), not in this process -- a restart of
`comfy-orchestrator-api` must not kill an agent mid-turn, and the transcript
stream (SSE keyed by a per-chat sequence) resumes by itself through the proxy.
The proxy authenticates to the runner with this instance's own `API_TOKEN`,
so the runner's `RUNNER_TOKENS` must list it (`agent_runner/install.sh`).
`deploy.sh` does not restart the runner. Details: `agent_runner/README.md`.
**Agent dock** (`AgentDock.tsx`, `state/agentDock.ts`): the project chats
are also reachable from the grid, board and design doc through a 💬 bubble
(bottom sheet on a phone, side panel when wide). It reuses the tab's own
`ChatView` (a `dock` prop, not a copy), and lists project chats only, never
dev chats. Every message sent from it starts with an English line
saying which view it came from. That line can't be switched off, by the
user's choice: to send without it, use the Agent tab. Grid cells attach via 🎯 pick-mode or the
selection bar, and go as node_id/dashboard_id text plus the face picture.
Unsent text/pictures persist per chat in IndexedDB (`chatDrafts.ts`),
attached cells per project in localStorage.
**Night mode** (`agent_runner/night.py`): while the person has it switched on,
agents can file bugs in this app with the MCP tool `report_bug`; a dev agent
works each one unattended, refuses non-bugs with reasons, fixes real ones, and
the *runner* (not the agent) backs up prod, stashes work in progress, deploys,
health-checks, and commits on a `night/<session>` branch only once prod came
up -- else discards and redeploys. The morning accept/reject is for the whole
night (fast-forward main, or redeploy main); no partial picks, by the user's
choice. If you find the dev copy on a `night/*` branch with a stash named
"night session ...", a night session is open: decide it in the Agent tab
rather than by hand.

**Handoff** (`agent_runner/handoff.py`, config `handoff.json` in the runner's
data dir): a project-chat agent can pass its task to another provider+model
**one way** -- the source chat freezes for good, nothing comes back, so the
sender never verifies a result. The runner enforces everything, never the
agent's prompt: a chain ban per model *identity* (no returning the task through
another CLI), allow/deny rules per *target* (`provider:model`) learned from the
person's four-button card, a timeout that is a soft no, and limits judged per
pool by pace (100% or far ahead of the window's elapsed time = hidden from the
agent outright; moderately ahead = "scarce", which always asks Yes/No; under 30%
used is always ok). Model profiles (`good_for`/`appetite`/`draws_natively`) are
**hand-written and editable, never inferred**: the person refused a "learn
capabilities from observation" stage, and `draws_natively` means the model's
*own* image generator -- every agent can draw through the orchestrator anyway.
The new chat's first message is system-assembled (English service text, the
person's own messages verbatim and carried down the whole chain). On
Antigravity, project chats run hook mode `project`, which refuses commands and
file writes (`comfy_hook_bridge.py`). Briefs: `docs/agent-handoff/`.

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
