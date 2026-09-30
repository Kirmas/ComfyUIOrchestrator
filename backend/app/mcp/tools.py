"""MCP tools -- the agent-facing surface of the orchestrator.

Granularity is the node, never the grid and never a raw ComfyUI job: the
pipeline is step-sequential by construction (a column can't start before the
one before it has settled), and the agent doesn't care what ComfyUI is doing,
only what a node produces.

Almost every tool here is a thin call into this app's own REST API through
app/mcp/client.py, so the routes' validation stays the single source of truth
instead of being mirrored (and drifting) here.
"""
import asyncio
import base64
import io
import math
import mimetypes
import uuid
from pathlib import Path

from PIL import Image as PILImage
from sqlalchemy import select

from app.db.base import async_session_maker
from app.db.models import Asset, Node
from app.core.storage import get_storage
from app.mcp.client import get_client, raise_for_api_error
from app.mcp.server import mcp_server

# Terminal node states -- await_node stops on any of these.
_TERMINAL = {"done", "error", "discarded"}


async def _get(path: str, **params):
    async with get_client() as client:
        r = await client.get(path, params=params or None)
        raise_for_api_error(r)
        return r.json()


async def _post(path: str, payload: dict | None = None):
    async with get_client() as client:
        r = await client.post(path, json=payload if payload is not None else {})
        raise_for_api_error(r)
        return r.json() if r.content else None


async def _patch(path: str, payload: dict):
    async with get_client() as client:
        r = await client.patch(path, json=payload)
        raise_for_api_error(r)
        return r.json() if r.content else None


async def _put(path: str, payload: dict):
    async with get_client() as client:
        r = await client.put(path, json=payload)
        raise_for_api_error(r)
        return r.json() if r.content else None


async def _delete(path: str) -> None:
    async with get_client() as client:
        r = await client.delete(path)
        raise_for_api_error(r)


# ---------- projects / tracks ----------
@mcp_server.tool()
async def list_projects() -> list[dict]:
    """List all projects (id, name, start_kind, category_id). category_id is
    the folder it's filed under on the projects page (see
    list_project_categories); null = top level."""
    return await _get("/api/projects")


@mcp_server.tool()
async def list_project_categories() -> list[dict]:
    """The projects page's folders (id, name, parent_id -- they nest; null
    parent = top level). Organisation only: nothing in a grid reads them."""
    return await _get("/api/project-categories")


@mcp_server.tool()
async def create_project(name: str, category_id: str | None = None) -> dict:
    """Create an empty project, optionally filed under a category from
    list_project_categories. It has no tracks yet -- call create_track next."""
    return await _post("/api/projects", {"name": name, "category_id": category_id})


@mcp_server.tool()
async def get_project_recipe(project_id: str, dashboard_id: str | None = None) -> dict:
    """Read a project step by step: ordered tracks, the nodes at each step with
    their params, plus which cells are occupied or blocked by a spanning card.

    Call this before creating nodes -- create_node needs a concrete
    (track_id, step_index) and a collision is a hard error.

    Any asset node here with a non-null `subgraph_dashboard_id` is a smart
    pointer into its own separate grid ("sub-dashboard") -- pass that value
    as `dashboard_id` in a second call to read the nested grid the same way
    (its own tracks/steps/spans, not a filter over this one). `project_id`
    stays the same top-level project either way.
    """
    params = {"dashboard_id": dashboard_id} if dashboard_id else {}
    return await _get(f"/api/projects/{project_id}/recipe", **params)


@mcp_server.tool()
async def list_tracks(project_id: str, dashboard_id: str | None = None) -> list[dict]:
    """Tracks (grid rows) of a project, already in top-to-bottom order.

    Pass a sub-dashboard's id (from an asset node's `subgraph_dashboard_id`,
    see get_project_recipe) to list that nested grid's own tracks instead of
    the main grid's.
    """
    params = {"dashboard_id": dashboard_id} if dashboard_id else {}
    return await _get(f"/api/projects/{project_id}/tracks", **params)


@mcp_server.tool()
async def create_track(
    project_id: str, after_track_id: str | None = None, place_at_head: bool = False, dashboard_id: str | None = None
) -> dict:
    """Add a row. Placement is relative: after a given track, at the head, or
    (default) appended at the bottom -- there is no numeric row index.

    dashboard_id omitted adds to the project's main grid; pass a
    sub-dashboard's id (see get_project_recipe) to add a row inside that
    nested grid instead. Omit it when after_track_id is set -- the new row
    joins whichever scope that track is already in."""
    payload: dict = {"project_id": project_id, "place_at_head": place_at_head}
    if after_track_id:
        payload["after_track_id"] = after_track_id
    if dashboard_id:
        payload["dashboard_id"] = dashboard_id
    return await _post("/api/tracks", payload)


# ---------- nodes ----------
@mcp_server.tool()
async def list_node_types() -> list[dict]:
    """Available node types (both DB-backed template.* and built-in native.*),
    with the param_schema whose image/file fields decide a node's row span.

    Doesn't include each type's `defaults` -- a "fixed" image/file field can
    bake megabytes of base64 in there, which used to ride along on every
    single call here regardless of relevance. Call get_node_type_defaults for
    one specific type's defaults if something actually needs them."""
    return await _get("/api/node-templates")


@mcp_server.tool()
async def get_node_type_description(node_type_slug: str) -> dict:
    """What a node type does, plus the facts it was derived from (model, LoRAs,
    image inputs, prompt) -- shown per backend where they differ."""
    return await _get(f"/api/node-templates/by-slug/{node_type_slug}/description")


@mcp_server.tool()
async def get_node_type_defaults(node_type_slug: str) -> dict:
    """One node type's baked defaults -- deliberately not part of
    list_node_types (see its own docstring): a "fixed" image/file field's
    value lives here as base64 and can be megabytes."""
    return await _get(f"/api/node-templates/by-slug/{node_type_slug}/defaults")


@mcp_server.tool()
async def write_agent_description(node_type_slug: str, description: str, source_length: int) -> dict:
    """Replace a long auto description with a shorter reading of the same
    thing, so the next reader doesn't have to wade through the original.

    `source_length` is the character count of whatever was actually read to
    write it (a workflow graph, a baked prompt, the existing description). The
    write is refused unless the summary really is shorter, and a hand-written
    description is never overwritten.
    """
    return await _post(
        f"/api/node-templates/by-slug/{node_type_slug}/agent-description",
        {"description": description, "source_length": source_length},
    )


@mcp_server.tool()
async def create_node(
    track_id: str,
    step_index: int,
    node_type: str,
    params: dict | None = None,
    kind: str = "workflow",
) -> dict:
    """Create a node at one cell.

    Column kind (asset vs workflow) alternates project-wide and is dictated by
    step_index, not by `kind` -- only the very first node in a project uses it
    to fix the pattern.
    """
    return await _post(
        "/api/nodes",
        {
            "track_id": track_id,
            "step_index": step_index,
            "kind": kind,
            "node_type": node_type,
            "params": params or {},
        },
    )


@mcp_server.tool()
async def get_node(node_id: str) -> dict:
    """Read one node: status, params, inputs, node_type, error."""
    return await _get(f"/api/nodes/{node_id}")


@mcp_server.tool()
async def set_node_params(node_id: str, params: dict) -> dict:
    """Replace a node's params (prompts exposed as grid-editable fields,
    sampler settings, etc.). For a workflow's *baked* prompt use set_prompt."""
    return await _patch(f"/api/nodes/{node_id}", {"params": params})


@mcp_server.tool()
async def upload_reference_image(
    node_id: str,
    image_base64: str | None = None,
    file_path: str | None = None,
    filename: str = "upload.png",
    mime_type: str = "image/png",
) -> dict:
    """Upload image bytes as a brand-new asset owned by an asset-kind node.

    For anything but a small/thumbnail-sized file, don't call this tool at
    all -- multipart-POST it yourself, directly, to
    `/api/nodes/{node_id}/upload-asset` (form field name `file`) on the exact
    same host:port you reached this MCP server through, with the same bearer
    token you're already using for this call. This app's REST API and its MCP
    server are one process on one port (see CLAUDE.md's MCP section) -- there
    is no separate host, port, or credential to go find first, on this
    machine or any other one on the network. Bytes sent that way never pass
    through this tool call, or any MCP call, at all -- so there's no context
    cost regardless of file size or how many you're sending.
    Example: `curl -X POST -H "Authorization: Bearer <token>" -F "file=@Chart_Gambeson.png" http://<same-host>/api/nodes/<node_id>/upload-asset`

    This tool exists for the two cases where that isn't the better option:
    - `image_base64` -- bytes that only exist in the caller's own context
      (e.g. freshly generated pixels with nothing written to disk anywhere).
      Inlining a real file this way costs the *caller's* own tokens: a 2 MB
      PNG is ~2.9 MB of base64 text, easily hundreds of thousands of tokens
      for one image -- fine for something small, a real mistake for a batch
      of full-resolution source images.
    - `file_path` -- only if whatever is calling this tool happens to share a
      filesystem with this orchestrator process itself (e.g. both running on
      its own box). This server reads that path directly; only the path
      string crosses into the call. Not useful, and will just fail with a
      "does not exist" error, if the file actually lives on a different
      machine than this server -- use the direct POST above in that case, not
      this parameter.

    `filename`/`mime_type` only apply to the `image_base64` path; `file_path`
    uses its own name and a sniffed content type instead.

    To point a cell at an image that already exists elsewhere *in this app*,
    use link_reference_asset instead -- that stores a pointer rather than a
    copy either way.
    """
    if (file_path is None) == (image_base64 is None):
        raise RuntimeError("Pass exactly one of file_path or image_base64, not both and not neither.")

    if file_path is not None:
        path = Path(file_path)
        if not path.is_file():
            raise RuntimeError(f"file_path does not exist or is not a file: {file_path}")
        data = path.read_bytes()
        filename = path.name
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    else:
        data = base64.b64decode(image_base64)

    async with get_client() as client:
        r = await client.post(
            f"/api/nodes/{node_id}/upload-asset",
            files={"file": (filename, data, mime_type)},
        )
        raise_for_api_error(r)
        return r.json()


@mcp_server.tool()
async def link_reference_asset(track_id: str, step_index: int, source_node_id: str, source_asset_id: str) -> dict:
    """Place an existing asset in another cell without copying the file --
    creates a reference node pointing at the original."""
    return await _post(
        "/api/nodes",
        {
            "track_id": track_id,
            "step_index": step_index,
            "kind": "asset",
            "node_type": "asset.refasset",
            "inputs": [{"type": "explicit", "node_id": source_node_id, "output_id": source_asset_id}],
        },
    )


@mcp_server.tool()
async def set_prompt(capability_id: str, workflow_node_id: str, input_key: str, value: str) -> dict:
    """Edit the literal prompt text baked into this capability's own ComfyUI
    graph, at (workflow_node_id, input_key) -- get those from list_prompt_fields.

    Use this for BOTH kinds of field list_prompt_fields returns, not just the
    non-param (is_variable: false) ones: a param-mapped field's node.params
    value only overrides this literal when a node instance has actually been
    touched (see set_node_params); a fresh, never-touched instance still runs
    on whatever's sitting here -- confirmed 2026-08-09 by tracing
    resolve_node_inputs/build_workflow, neither of which falls back to
    NodeTemplate.defaults or param_schema's own field default for a text
    field. This is the only thing that changes what a fresh instance of a
    mapped field generates.

    If this capability follows another instance's prompts, the write is
    redirected to its leader -- which is also mirrored into every other
    follower of that leader. The response lists every capability changed, so
    the caller can see it edited more than the one instance it named.
    """
    capability = await _get(f"/api/capabilities/{capability_id}")
    leader_id = (capability.get("config") or {}).get("prompt_leader_id")
    target_id = str(leader_id) if leader_id else capability_id

    updated = await _patch(
        f"/api/capabilities/{target_id}/text-fields",
        {"node_id": workflow_node_id, "input_key": input_key, "value": value},
    )

    siblings = await _get("/api/capabilities")
    affected = [target_id] + [
        str(c["id"]) for c in siblings if (c.get("config") or {}).get("prompt_leader_id") == target_id
    ]
    return {
        "requested_capability_id": capability_id,
        "written_to_capability_id": target_id,
        "redirected_to_leader": bool(leader_id),
        "affected_capability_ids": affected,
        "capability": updated,
    }


@mcp_server.tool()
async def list_prompt_fields(capability_id: str) -> list[dict]:
    """Every prompt-shaped text field worth knowing about for this
    capability's node type, each with the (node_id, input_key) set_prompt
    needs -- the discovery step for it.

    `is_variable` tells you whether the SAME field is also independently
    settable per node instance via set_node_params (true), or only exists in
    the graph (false) -- it does NOT change which tool edits what a fresh
    instance generates; that's always set_prompt, on this same (node_id,
    input_key), either way. See set_prompt's docstring for why. (There is
    deliberately no tool for `PATCH .../variable-default` -- it only writes
    param_schema's own cosmetic `default`, never consulted at generation
    time; see CLAUDE.md's MCP section.)
    """
    return await _get(f"/api/capabilities/{capability_id}/text-fields")


# ---------- sub-dashboards (smart pointers, see api/routes/dashboards.py) ----------
# These were a pure gap, not a design choice: dashboards.py's REST routes have
# existed since sub-dashboards shipped, but nothing in this file ever wrapped
# them, so create_node(node_type="asset.subgraph") could produce a node whose
# subgraph_dashboard_id stays permanently null -- a dead pointer with no nested
# grid behind it, and no MCP path to attach one. Fixed 2026-09-14. There is
# still deliberately no delete_dashboard/delete_node/delete_track tool here --
# not attempted, at the user's own instruction.
async def _resolve_or_create_pointer_cell(node_id: str | None, track_id: str | None, step_index: int | None) -> str:
    """Shared by every tool below that needs a free asset cell to turn into a
    pointer: reuse an existing one (`node_id`) or create a fresh empty one at
    (`track_id`, `step_index`) -- the same call the UI's own "+ asset" button
    makes (kind="asset", no node_type yet) -- so the common case doesn't need
    a separate create_node round trip first."""
    if node_id is not None:
        return node_id
    if track_id is None or step_index is None:
        raise RuntimeError("Pass either node_id, or both track_id and step_index, to name the pointer cell.")
    node = await _post("/api/nodes", {"track_id": track_id, "step_index": step_index, "kind": "asset"})
    return node["id"]


@mcp_server.tool()
async def create_dashboard(
    track_id: str | None = None,
    step_index: int | None = None,
    name: str = "",
    node_id: str | None = None,
) -> dict:
    """Create a brand-new, empty sub-dashboard (its own nested grid) and the
    pointer cell that opens it, in one call.

    Common case: pass `track_id`/`step_index` for a fresh cell there -- no
    need to call create_node first. Pass `node_id` instead to reuse an
    *existing* free asset cell: it must not be a workflow's own materialized
    output and must not already point at a dashboard. Don't upload a picture
    into that cell beforehand either way -- a subgraph node's face is
    Dashboard.result_asset_id (see set_dashboard_result), not whatever asset
    the cell held before it became a pointer; anything uploaded there first
    would just go orphaned.

    `name` is the only place in this app a container gets a name at all --
    individual nodes and tracks have none. Pass the chart/picture name here,
    e.g. "Chart_Gambeson". The response's `id` is the new dashboard: pass it as
    `dashboard_id` to create_track / create_node / get_project_recipe to build
    out its contents (an asset cell for the source image, a workflow cell next
    to it, etc.).
    """
    resolved_node_id = await _resolve_or_create_pointer_cell(node_id, track_id, step_index)
    return await _post("/api/dashboards", {"node_id": resolved_node_id, "name": name})


@mcp_server.tool()
async def get_dashboard(dashboard_id: str) -> dict:
    """Read one sub-dashboard: its name, live node/pointer counts, owner node,
    and result_asset_id (the picture every pointer into it shows)."""
    return await _get(f"/api/dashboards/{dashboard_id}")


@mcp_server.tool()
async def rename_dashboard(dashboard_id: str, name: str | None = None, asset_only_view: bool | None = None) -> dict:
    """Rename a sub-dashboard and/or toggle its asset-only view. Omit whichever
    of the two you're not changing -- only the ones passed are touched."""
    payload = {k: v for k, v in {"name": name, "asset_only_view": asset_only_view}.items() if v is not None}
    return await _patch(f"/api/dashboards/{dashboard_id}", payload)


@mcp_server.tool()
async def copy_dashboard(
    dashboard_id: str,
    track_id: str | None = None,
    step_index: int | None = None,
    name: str = "",
    node_id: str | None = None,
) -> dict:
    """Copy an existing sub-dashboard's structure and workflow settings into a
    brand-new one, and turn a pointer cell (fresh at track_id/step_index, or
    an existing free one via node_id -- same rules as create_dashboard) into
    that copy's owner.

    Useful for a batch of near-identical charts: build one dashboard fully
    (source image, upscale workflow with the right params, result cell), then
    copy_dashboard it for each of the others instead of re-entering the same
    template/params by hand. What actually copies (core/subgraph_copy.py):
    structure and a workflow node's own settings (template, params, slot refs,
    variants, backend, use_api) -- yes, including the numbers, so identical
    source dimensions mean nothing to re-tune per copy. What does NOT copy:
    any workflow's own materialized output (left as a hole to regenerate), and
    the source *picture* itself -- the copy's asset cell comes across as a
    reference to the same original file, not a new upload, so for a batch
    where every chart is a different picture you still need to point that
    cell at (or upload) the correct image for the new one afterward.
    """
    resolved_node_id = await _resolve_or_create_pointer_cell(node_id, track_id, step_index)
    return await _post(f"/api/dashboards/{dashboard_id}/copy", {"node_id": resolved_node_id, "name": name})


@mcp_server.tool()
async def add_pointer(
    dashboard_id: str,
    track_id: str | None = None,
    step_index: int | None = None,
    node_id: str | None = None,
) -> dict:
    """Point an additional free asset cell (fresh at track_id/step_index, or
    an existing free one via node_id) at an existing sub-dashboard -- a second
    way in to the same nested grid, not a copy of it. Unlike the dashboard's
    owner (the one create_dashboard/copy_dashboard makes), this pointer is a
    non-tree edge and is always safe to leave in place or discard later
    without affecting the dashboard's contents."""
    resolved_node_id = await _resolve_or_create_pointer_cell(node_id, track_id, step_index)
    return await _post(f"/api/dashboards/{dashboard_id}/pointers", {"node_id": resolved_node_id})


@mcp_server.tool()
async def set_dashboard_result(dashboard_id: str, asset_id: str | None = None) -> dict:
    """Choose which asset generated inside this sub-dashboard is its result --
    the face every pointer into it (including the one sitting in the parent
    grid) shows from then on. Pass asset_id=None to clear it back to blank.

    The asset must have been produced inside this dashboard's own grid. A
    reference (asset.refasset) to a picture made elsewhere is refused on
    purpose: a dashboard's face is what that dashboard made. Don't work around
    it by copying the picture in (e.g. a full-frame native.crop) -- if the
    result you want lives somewhere else, flag the cell and leave it for the
    person to decide."""
    return await _post(f"/api/dashboards/{dashboard_id}/result", {"asset_id": asset_id})


@mcp_server.tool()
async def transfer_ownership(dashboard_id: str, node_id: str) -> dict:
    """Hand the main-pointer role to another existing pointer at the same
    dashboard. Rarely needed by an agent -- mainly here for parity with the UI
    -- since create_dashboard already makes its own pointer the owner."""
    return await _post(f"/api/dashboards/{dashboard_id}/transfer-ownership", {"node_id": node_id})


# ---------- running ----------
@mcp_server.tool()
async def run_node(node_id: str, variants: int = 1, backend: str = "comfy", session_mode: str = "interactive") -> dict:
    """Start generating. Returns immediately -- node_id is the handle; poll with
    await_node or get_runs_status.

    backend "comfy" is the local GPU (free), "api" is a paid provider. In
    session_mode "auto" (unattended/overnight) a paid run is refused outright
    and the cell is flagged for review instead, so nothing bills while nobody
    is watching. Which GPU actually takes the job is the scheduler's business,
    not the caller's.
    """
    use_api = backend == "api"
    if use_api and session_mode == "auto":
        node = await _get(f"/api/nodes/{node_id}")
        track = await _get(f"/api/tracks/{node['track_id']}")
        await _post(
            "/api/annotations",
            {
                "project_id": track["project_id"],
                "node_ids": [node_id],
                "text": "Skipped in unattended mode: this step needs a paid API backend. Run it by hand.",
                "source": "agent",
            },
        )
        return {
            "blocked": True,
            "node_id": node_id,
            "reason": "paid backend refused in session_mode=auto; cell flagged for manual review",
        }

    await _patch(
        f"/api/nodes/{node_id}",
        {"requested_variants": variants, "use_api": use_api, "backend_mode": "auto"},
    )
    await _post(f"/api/nodes/{node_id}/generate")
    return {"blocked": False, "node_id": node_id, "status": "queued", "requested_variants": variants}


@mcp_server.tool()
async def await_node(node_id: str, timeout_seconds: int = 300) -> dict:
    """Wait for a node to finish, up to timeout_seconds. Always returns -- on
    timeout it reports the last status seen rather than blocking forever.

    Reads status straight from the database rather than through the HTTP API:
    this is the one polling loop in the tool set, and a nested request per poll
    would hold a connection from the same pool the workers are using.
    """
    poll_interval = max(2.0, min(10.0, timeout_seconds / 20))
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    node_uuid = uuid.UUID(node_id)

    while True:
        async with async_session_maker() as db:
            node = await db.get(Node, node_uuid)
            if node is None:
                raise RuntimeError(f"Node {node_id} not found")
            status = node.status.value if hasattr(node.status, "value") else str(node.status)
            error = node.error
        if status in _TERMINAL:
            return {"node_id": node_id, "status": status, "error": error, "timed_out": False}
        if asyncio.get_running_loop().time() >= deadline:
            return {"node_id": node_id, "status": status, "error": error, "timed_out": True}
        await asyncio.sleep(poll_interval)


@mcp_server.tool()
async def get_runs_status(node_ids: list[str]) -> list[dict]:
    """Non-blocking status check for several nodes at once."""
    out = []
    for node_id in node_ids:
        node = await _get(f"/api/nodes/{node_id}")
        out.append(
            {
                "node_id": node_id,
                "status": node["status"],
                "error": node.get("error"),
                "requested_variants": node.get("requested_variants"),
            }
        )
    return out


@mcp_server.tool()
async def rerun_node(node_id: str) -> dict:
    """Re-roll a failed or orphaned node: discards the old attempt and its
    outputs, then queues a fresh one with the same inputs and params.

    A server restart mid-generation leaves jobs that can only be recovered this
    way -- there is no durable queue behind them.
    """
    return await _post(f"/api/nodes/{node_id}/reroll")


@mcp_server.tool()
async def cancel_node(node_id: str) -> dict:
    """Stop a node's generation: every variant still queued, waiting for a
    backend or running is cancelled. Variants that already finished are kept,
    and the node settles as done on them -- the way out when some variants
    came back and the rest are stuck. Takes a moment to land; confirm with
    get_runs_status."""
    jobs = await _get(f"/api/nodes/{node_id}/jobs")
    live = [j for j in jobs if j["status"] in ("pending", "running", "waiting_for_backend")]
    await _post(f"/api/nodes/{node_id}/cancel")
    return {
        "node_id": node_id,
        "cancelled_variants": [j["variant_index"] for j in live],
        "kept_variants": [j["variant_index"] for j in jobs if j["status"] == "done"],
    }


# ---------- candidates ----------
# Claude looks at no more than ~1568 px on the long edge; anything bigger is
# downscaled on arrival anyway, so sending it only costs transfer. The originals
# are the problem this exists for: an 8K upscale is tens of MB, and returning
# one as is closed the MCP connection outright (2026-09-27).
_VIEW_MAX_PX = 1568


def _agent_view(data: bytes, max_px: int, region: list[float] | None) -> tuple[bytes, str, tuple[int, int]]:
    """An image as the agent should see it: `region` (fractions of the
    original) cut out first, then scaled to fit max_px. Returns the bytes,
    their format and the size actually sent. Blocking -- decoding an 8K PNG
    takes about a second and ~130 MB, so callers run it in a thread."""
    with PILImage.open(io.BytesIO(data)) as img:
        if region is None and not (max_px and max(img.size) > max_px):
            return data, (img.format or "png").lower(), img.size
        # Real transparency is kept (native.mask bakes its holes into alpha), so
        # that goes out as PNG; everything else as JPEG, several times smaller.
        alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
        view = img
        if region is not None:
            width, height = img.size
            left, top, right, bottom = region
            # Outward to whole pixels, so a region never rounds away to nothing
            # (a thin strip of a small image would otherwise crop to 0 px).
            box = (
                math.floor(left * width),
                math.floor(top * height),
                max(math.floor(left * width) + 1, math.ceil(right * width)),
                max(math.floor(top * height) + 1, math.ceil(bottom * height)),
            )
            view = img.crop(box)
        if view.mode not in ("RGB", "RGBA"):
            view = view.convert("RGBA" if alpha else "RGB")
        if max_px and max(view.size) > max_px:
            view.thumbnail((max_px, max_px), PILImage.LANCZOS)
        buf = io.BytesIO()
        if alpha and view.getchannel("A").getextrema()[0] < 255:
            view.save(buf, "PNG")
            return buf.getvalue(), "png", view.size
        view.convert("RGB").save(buf, "JPEG", quality=90)
        return buf.getvalue(), "jpeg", view.size


@mcp_server.tool()
async def get_candidates(
    node_id: str, max_images: int = 8, max_px: int = _VIEW_MAX_PX, region: list[float] | None = None
) -> list:
    """Look at a node's outputs as actual images, so they can be judged rather
    than guessed at from metadata.

    The first line lists every output -- asset_id, whether it's selected, its
    full size -- whatever max_images is, so max_images=0 is the cheap way to
    get just the ids (for select_candidate or set_dashboard_result).

    Images come back scaled to fit max_px on the long edge; about 1568 is the
    most a model actually looks at. To inspect detail -- seams between upscale
    tiles, lettering, a hand -- pass region=[left, top, right, bottom] as
    fractions of the image (e.g. [0.4, 0.4, 0.6, 0.6] for the middle): the crop
    is cut from the full-size original before scaling, so a small enough
    region shows real pixels. The same region applies to every candidate,
    which makes it easy to compare them. max_px=0 sends images unscaled --
    avoid it on anything big.
    """
    from mcp.server.fastmcp import Image

    if region is not None:
        if len(region) != 4 or not (0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1):
            raise ValueError("region must be [left, top, right, bottom], fractions of the image with left < right and top < bottom")

    outputs = await _get(f"/api/nodes/{node_id}/outputs")
    storage = get_storage()
    listing = [
        {
            "asset_id": asset["id"],
            "selected": asset.get("selected"),
            "size": f"{asset['width']}x{asset['height']}" if asset.get("width") else None,
            "mime_type": asset.get("mime_type"),
            "created_at": asset.get("created_at"),
        }
        for asset in outputs
    ]
    content: list = [{"type": "text", "text": f"{len(outputs)} candidate(s) for node {node_id}: {listing}"}]

    for asset in outputs[: max(0, max_images)]:
        try:
            data = await asyncio.to_thread(storage.get_object, asset["storage_key"])
            view, fmt, size = await asyncio.to_thread(_agent_view, data, max_px, region)
        except Exception as exc:  # one unreadable output shouldn't cost the agent the rest
            content.append({"type": "text", "text": f"asset {asset['id']}: can't show it as an image ({exc!r})"})
            continue
        shown = "region " + str(region) + ", " if region else ""
        content.append({"type": "text", "text": f"asset {asset['id']} ({shown}sent at {size[0]}x{size[1]})"})
        content.append(Image(data=view, format=fmt))
    return content


@mcp_server.tool()
async def select_candidate(node_id: str, kept_asset_id: str) -> dict:
    """Settle one candidate as the chosen image for its cell.

    Nothing is destroyed: the kept image takes over the original cell and any
    remaining candidates move to their own branch, so a different choice can
    still be made later by hand.

    The leftovers move, so the picker's own id is no longer what sits in the
    original cell -- both are reported back.
    """
    async with async_session_maker() as db:
        picker = await db.get(Node, uuid.UUID(node_id))
        if picker is None:
            raise RuntimeError(f"Node {node_id} not found")
        origin_track_id, origin_step = picker.track_id, picker.step_index

    await _post(f"/api/nodes/{node_id}/pick-candidate", {"kept_asset_id": kept_asset_id})

    async with async_session_maker() as db:
        result = await db.execute(
            select(Node).where(
                Node.track_id == origin_track_id,
                Node.step_index == origin_step,
                Node.status != "discarded",
            )
        )
        settled = result.scalars().first()
        picker = await db.get(Node, uuid.UUID(node_id))
        leftovers = 0
        if picker is not None:
            count = await db.execute(select(Asset).where(Asset.node_id == picker.id))
            leftovers = len(list(count.scalars().all()))

    return {
        "settled_node_id": str(settled.id) if settled else None,
        "settled_node_type": settled.node_type if settled else None,
        "picker_node_id": node_id,
        "picker_moved": bool(picker and picker.track_id != origin_track_id),
        "remaining_candidates": leftovers,
    }


# ---------- review ----------
# A cell's comments are one thread (one per set of cells), shared with the
# person: flag_cell starts it or continues it, reply_to_flag answers in it,
# resolve_flag marks it done. What anyone said is never removed from here --
# there is deliberately no delete tool, the same way there is none for nodes:
# closing a thread is how an agent says "handled", and cleaning up stays a
# person's call.
@mcp_server.tool()
async def flag_cell(node_id: str, note: str) -> dict:
    """Leave a note on a cell for the person to read, and carry on.

    Use this instead of stopping to ask a question when running unattended.
    It shows up as a comment frame on the grid, the same thread a person
    writes in by hand. If the cell already has a thread this continues it
    (and reopens it if it was resolved) rather than stacking a second frame
    on the same cell -- so for a status update on work you announced earlier,
    prefer reply_to_flag on that thread, or edit_flag_message if you'd rather
    update your earlier message in place.
    """
    node = await _get(f"/api/nodes/{node_id}")
    track = await _get(f"/api/tracks/{node['track_id']}")
    return await _post(
        "/api/annotations",
        {"project_id": track["project_id"], "node_ids": [node_id], "text": note, "source": "agent"},
    )


@mcp_server.tool()
async def list_flags(project_id: str, include_resolved: bool = False) -> list[dict]:
    """Comment threads on a project's grid, each with every message in it,
    oldest first, and `source` saying who wrote it ("user" is the person,
    "agent" is you or another agent).

    Read the last message to see where a thread stands: one ending in a
    "user" message is the person talking to you -- an instruction, feedback
    on something you made -- and is yours to act on, then answer with
    reply_to_flag. Resolved threads are left out unless include_resolved; a
    new message in one reopens it, so anything the person comes back to
    shows up here again by itself.
    """
    threads = await _get(f"/api/projects/{project_id}/annotations")
    return threads if include_resolved else [t for t in threads if not t["resolved"]]


@mcp_server.tool()
async def reply_to_flag(flag_id: str, text: str, resolve: bool = False) -> dict:
    """Answer in a comment thread (flag_id is the thread's id from list_flags
    or flag_cell).

    resolve=True also marks the thread done in the same call -- the usual way
    to report back on something the person asked for ("done: ..., have a
    look"). A resolved thread stays on the grid, just quieter, and reopens if
    anyone writes in it again.
    """
    thread = await _post(f"/api/annotations/{flag_id}/messages", {"text": text, "source": "agent"})
    if resolve:
        thread = await _patch(f"/api/annotations/{flag_id}", {"resolved": True, "source": "agent"})
    return thread


@mcp_server.tool()
async def resolve_flag(flag_id: str, resolved: bool = True) -> dict:
    """Mark a comment thread done without writing anything, or reopen it
    (resolved=False). Nothing in it is deleted either way."""
    return await _patch(f"/api/annotations/{flag_id}", {"resolved": resolved, "source": "agent"})


@mcp_server.tool()
async def edit_flag_message(message_id: str, text: str) -> dict:
    """Rewrite one of your own messages in a thread -- e.g. turn an earlier
    "in progress, don't touch" into "done". Only messages you wrote
    (source="agent") can be edited; a person's message is theirs, so answer
    it with reply_to_flag instead."""
    return await _patch(f"/api/annotation-messages/{message_id}", {"text": text, "source": "agent"})


# ---------- backends ----------
@mcp_server.tool()
async def list_backends() -> list[dict]:
    """Configured backends (GPU instances and paid API providers), with their
    ids -- needed to turn a machine you know by address into an id."""
    return await _get("/api/backends")


# ---------- authoring ----------
@mcp_server.tool()
async def create_node_type(
    workflow_json: dict,
    name: str,
    node_type_slug: str,
    backend_id: str,
    param_mapping: dict,
) -> dict:
    """Register a ComfyUI workflow as a new node type, bound to one backend.

    `param_mapping` maps a field name to the workflow input it fills:
    `{"seed": {"node_id": "3", "input_key": "seed"}, ...}`. It is required, and
    must include `seed` -- without a seed to vary, asking for several variants
    would produce the same image several times.

    A LoadImage node's own "image" input can be named here too -- e.g.
    `"pose_reference": {"node_id": "151", "input_key": "image"}` -- which is
    the only way to tell two image roles apart on a workflow with more than
    one LoadImage node; each entry's own `"label"` (falls back to that
    LoadImage's ComfyUI title) becomes the field's label. Any LoadImage left
    unnamed still gets an auto-numbered field of its own, so mapping images
    at all is optional, but every LoadImage in the workflow ends up with
    exactly one image field either way.

    Add `"optional": true` to an image entry (e.g. a growable reference-image
    node like Qwen Image 2.1's, which takes images.image_1..image_16) to let
    that slot be left empty per node instance -- it still reserves its row on
    the grid (row-span is fixed per node type, not per instance), but an
    unfilled optional slot's LoadImage node is pruned from the graph at run
    time instead of submitting whatever placeholder the workflow was captured
    with. Only settable this way; an auto-numbered (unnamed) image field is
    always required.

    The workflow is checked against the mapping first; on any mismatch nothing
    is created at all, and the error names what's available.
    """
    return await _post(
        "/api/node-types",
        {
            "workflow_json": workflow_json,
            "name": name,
            "node_type_slug": node_type_slug,
            "backend_id": backend_id,
            "param_mapping": param_mapping,
        },
    )


@mcp_server.tool()
async def capability_exists(node_type_slug: str, backend_id: str) -> dict:
    """Check whether a node type can actually run on a given backend.

    Worth checking before running a node on a specific machine: a node type can
    exist while having no binding for that machine, in which case the job never
    progresses and nothing says why.
    """
    return await _get(f"/api/node-types/{node_type_slug}/capability-exists", backend_id=backend_id)


@mcp_server.tool()
async def get_capability_workflow(node_type_slug: str, exclude_backend_id: str | None = None) -> dict:
    """Fetch a working version of this node type from another backend, to adapt
    for one that lacks it -- the recipe that already works elsewhere."""
    params = {"exclude_backend_id": exclude_backend_id} if exclude_backend_id else {}
    return await _get(f"/api/node-types/{node_type_slug}/reference-capability", **params)


@mcp_server.tool()
async def add_capability(node_type_slug: str, backend_id: str, workflow_json: dict, param_mapping: dict) -> dict:
    """Make an existing node type runnable on another backend, using a workflow
    adapted to whatever models that machine actually has.

    Validated like create_node_type, plus one more rule: the workflow must take
    the same number of images as the node type already declares, since that
    count sets how many rows the node covers on the grid for every backend.
    """
    return await _post(
        f"/api/node-types/{node_type_slug}/capabilities",
        {"backend_id": backend_id, "workflow_json": workflow_json, "param_mapping": param_mapping},
    )


# ---------- design doc ----------
@mcp_server.tool()
async def get_design_doc(project_id: str, lang: str = "uk") -> dict:
    """The project's design doc in one language (`lang`: "uk" or "en" -- the
    project has one doc per language, kept separately, not auto-translated):
    its markdown text plus what each reference in it currently resolves to
    (`refs`, keyed by the reference).

    References are ordinary markdown links/images with a scheme instead of a
    URL: `![caption](node:<node id>)` embeds a grid cell as whatever picture it
    stands for now, `![caption](dashboard:<dashboard id>)` a sub-dashboard's
    current result (follows it when a new result is chosen inside),
    `[text](board:<board item id>)` links a sticker, and `asset:<asset id>`
    pins one fixed file."""
    return await _get(f"/api/projects/{project_id}/design-doc", lang=lang)


@mcp_server.tool()
async def set_design_doc(project_id: str, content: str, lang: str = "uk") -> dict:
    """Replace the project's design doc in `lang` ("uk"/"en") with `content`
    (the whole markdown text -- read it with get_design_doc first and edit,
    don't overwrite blind: it's the person's document). Changing one language
    doesn't touch the other; keep them in step yourself if that's the task.
    See get_design_doc for reference syntax."""
    return await _put(f"/api/projects/{project_id}/design-doc?lang={lang}", {"content": content})


@mcp_server.tool()
async def list_global_design_docs() -> list[dict]:
    """Design docs that belong to a folder rather than a project -- e.g. a
    world's lore that several projects draw on. `category_id` is the folder
    (null = top level). Read/write them with get_global_design_doc /
    set_global_design_doc; a project's own doc is get_design_doc."""
    return await _get("/api/design-docs")


@mcp_server.tool()
async def get_global_design_doc(doc_id: str, lang: str = "uk") -> dict:
    """One language ("uk"/"en") of a global design doc, with its references
    resolved -- same shape and reference syntax as get_design_doc."""
    return await _get(f"/api/design-docs/{doc_id}/text", lang=lang)


@mcp_server.tool()
async def set_global_design_doc(doc_id: str, content: str, lang: str = "uk") -> dict:
    """Replace one language of a global design doc with `content`. Same rules
    as set_design_doc: read first, edit, don't overwrite blind."""
    return await _put(f"/api/design-docs/{doc_id}/text?lang={lang}", {"content": content})


# ---------- idea board (roadmap.md §1) ----------
# The point of exposing the board to the agent is that an idea has somewhere to
# land other than a chat that disappears: "propose eight directions for this
# character" becomes eight stickers the user can see, circle and pick from.
@mcp_server.tool()
async def get_board(project_id: str) -> dict:
    """The project's idea board (created on first access). Pre-production lives
    here -- the brief, the references, the divergence -- because the grid is
    convergent by construction and can't hold any of it."""
    return await _get(f"/api/projects/{project_id}/board")


@mcp_server.tool()
async def list_board_items(board_id: str) -> list[dict]:
    """Everything on a board: text stickers, media, circles, freehand strokes,
    connectors and comments. Each item carries its own x/y -- unlike the grid,
    position here is stored, not derived."""
    return await _get(f"/api/boards/{board_id}/items")


@mcp_server.tool()
async def create_note(board_id: str, text: str, x: float = 0, y: float = 0, tag: str | None = None, color: str | None = None) -> dict:
    """Put a text sticker (markdown) on the board.

    `tag` makes it referencable from a node's prompt as `{tag}`, resolved at run
    time. Tags are unique per project; reusing one is rejected rather than left
    ambiguous. Leave it unset for a sticker that's just a thought.

    Stickers you create are marked source="agent" so the user can tell at a
    glance which ideas came from where.
    """
    payload: dict = {"kind": "text", "text": text, "x": x, "y": y, "source": "agent"}
    if tag:
        payload["tag"] = tag
    if color:
        payload["color"] = color
    return await _post(f"/api/boards/{board_id}/items", payload)


@mcp_server.tool()
async def comment_on_board_item(board_id: str, item_id: str, text: str) -> dict:
    """Leave a remark about one sticker -- the board's equivalent of flag_cell."""
    return await _post(
        f"/api/boards/{board_id}/items",
        {"kind": "comment", "target_item_id": item_id, "text": text, "source": "agent"},
    )


@mcp_server.tool()
async def connect_board_items(board_id: str, source_item_id: str, target_item_id: str) -> dict:
    """Draw an arrow between two stickers. Anchored to the items themselves, so
    it follows them when they're moved."""
    return await _post(
        f"/api/boards/{board_id}/items",
        {"kind": "connector", "source_item_id": source_item_id, "target_item_id": target_item_id, "source": "agent"},
    )


@mcp_server.tool()
async def update_board_item(item_id: str, text: str | None = None, x: float | None = None, y: float | None = None) -> dict:
    """Edit a sticker's text or move it."""
    payload = {k: v for k, v in {"text": text, "x": x, "y": y}.items() if v is not None}
    return await _patch(f"/api/board-items/{item_id}", payload)


@mcp_server.tool()
async def delete_board_item(item_id: str) -> dict:
    """Remove a sticker. Its connectors and comments go with it."""
    await _delete(f"/api/board-items/{item_id}")
    return {"deleted": item_id}


@mcp_server.tool()
async def list_reference_assets(project_id: str, tag: str | None = None) -> list[dict]:
    """The project's reference library -- images the board owns, which no grid
    cell does. Place one in a cell with place_reference_asset."""
    return await _get(f"/api/projects/{project_id}/assets", **({"tag": tag} if tag else {}))


@mcp_server.tool()
async def place_reference_asset(track_id: str, step_index: int, asset_id: str) -> dict:
    """Put a library image into a grid cell as a reference node.

    A reference, never a copy and never an owned asset: assets owned by a cell
    are destroyed when that cell is deleted, which would take the picture off
    the board with it. The grid references the library; the board owns it.
    """
    return await _post(
        "/api/nodes",
        {
            "track_id": track_id,
            "step_index": step_index,
            "kind": "asset",
            "node_type": "asset.refasset",
            "inputs": [{"type": "explicit", "output_id": asset_id}],
        },
    )


# ---------- bug reports (night mode, agent_runner/night.py) ----------
@mcp_server.tool()
async def report_bug(summary: str, details: str) -> dict:
    """Report a bug in this orchestrator itself -- not in your own work.

    For when a tool contradicts its own documentation or the app's design:
    a 500, a result that doesn't match the docstring, two tools that disagree.
    NOT for wishes: a missing tool or parameter, a limit you'd like lifted, or
    behaviour that is documented as deliberate (e.g. there are no delete
    tools for agents) -- those are refused. Before filing, re-read the tool's
    docstring: a wrong call on your side is refused too, with an explanation.

    `details`: what you called (tool + arguments), what you expected and why
    (quote the docstring/design), what happened (exact error text). Max 20000
    chars; summary max 300.

    Only works while the person has night mode switched on; otherwise it
    fails, and you should tell the person yourself. A developer agent then
    works the report unattended. If it's a real bug, it fixes and deploys
    it -- the site restarts for about a minute, so your MCP calls fail
    meanwhile. Poll get_bug_report every minute or two until status is
    "done", then act on its verdict and reply.
    """
    return await _post("/api/agent-reports", {"summary": summary, "details": details})


@mcp_server.tool()
async def get_bug_report(report_id: str) -> dict:
    """Where a report_bug report stands.

    status: queued -> preparing -> investigating -> checking -> deploying ->
    done. While "deploying" the site is restarting: wait, don't retry yet.
    Once "done": verdict is the developer agent's call (bug-fixed, not-a-bug,
    agent-misuse, feature-request, unsure), outcome is what actually happened
    (deployed means the fix is live now; rolled-back / failed-checks mean the
    bug is still there), and reply is its answer to you -- for a refusal, what
    you're doing wrong and how to get there with the tools that exist.
    """
    return await _get(f"/api/agent-reports/{report_id}")
