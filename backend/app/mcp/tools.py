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
import uuid

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


async def _delete(path: str) -> None:
    async with get_client() as client:
        r = await client.delete(path)
        raise_for_api_error(r)


# ---------- projects / tracks ----------
@mcp_server.tool()
async def list_projects() -> list[dict]:
    """List all projects (id, name, start_kind)."""
    return await _get("/api/projects")


@mcp_server.tool()
async def create_project(name: str) -> dict:
    """Create an empty project. It has no tracks yet -- call create_track next."""
    return await _post("/api/projects", {"name": name})


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
    with the param_schema whose image/file fields decide a node's row span."""
    return await _get("/api/node-templates")


@mcp_server.tool()
async def get_node_type_description(node_type_slug: str) -> dict:
    """What a node type does, plus the facts it was derived from (model, LoRAs,
    image inputs, prompt) -- shown per backend where they differ."""
    return await _get(f"/api/node-templates/by-slug/{node_type_slug}/description")


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
async def upload_reference_image(node_id: str, image_base64: str, filename: str = "upload.png", mime_type: str = "image/png") -> dict:
    """Upload image bytes as a brand-new asset owned by an asset-kind node.

    To point a cell at an image that already exists elsewhere, use
    link_reference_asset instead -- that stores a pointer rather than a copy.
    """
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


# ---------- candidates ----------
@mcp_server.tool()
async def get_candidates(node_id: str, max_images: int = 8) -> list:
    """Fetch a node's outputs as actual images, so they can be looked at and
    judged rather than guessed at from metadata."""
    from mcp.server.fastmcp import Image

    outputs = await _get(f"/api/nodes/{node_id}/outputs")
    storage = get_storage()
    content: list = [{"type": "text", "text": f"{len(outputs)} candidate(s) for node {node_id}"}]

    for asset in outputs[:max_images]:
        summary = {
            "asset_id": asset["id"],
            "selected": asset.get("selected"),
            "mime_type": asset.get("mime_type"),
            "created_at": asset.get("created_at"),
        }
        content.append({"type": "text", "text": str(summary)})
        try:
            data = storage.get_object(asset["storage_key"])
        except OSError as exc:
            content.append({"type": "text", "text": f"(image unreadable: {exc})"})
            continue
        fmt = (asset.get("mime_type") or "image/png").split("/")[-1]
        content.append(Image(data=data, format=fmt))
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
@mcp_server.tool()
async def flag_cell(node_id: str, note: str) -> dict:
    """Mark a cell as needing a human look, with a note, and carry on.

    Use this instead of stopping to ask a question when running unattended.
    The flag shows up as a comment block on the grid, the same object a person
    creates by hand, so flags are reviewed alongside their own notes.
    """
    node = await _get(f"/api/nodes/{node_id}")
    track = await _get(f"/api/tracks/{node['track_id']}")
    return await _post(
        "/api/annotations",
        {"project_id": track["project_id"], "node_ids": [node_id], "text": note, "source": "agent"},
    )


@mcp_server.tool()
async def list_flags(project_id: str) -> list[dict]:
    """All comment blocks on a project -- both agent flags and hand-written notes."""
    return await _get(f"/api/projects/{project_id}/annotations")


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
