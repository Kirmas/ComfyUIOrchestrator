import asyncio
import logging
import random
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.config import get_settings
from app.core import dispatcher
from app.core.comfyui_backend import ComfyUIBackend, wait_with_timeout
from app.core.job_backend import JobStatus as BackendJobStatus
from app.core.node_types import resolve_effective_template, sync_legacy_fields
from app.core.queue import job_queue
from app.core.storage import get_storage
from app.core.template_engine import validate_params
from app.core.ws_manager import ws_manager
from app.db.base import async_session_maker
from app.db.models import Asset, Backend, Job, JobStatusEnum, Node, NodeKind, NodeStatus, Track

logger = logging.getLogger(__name__)

# ComfyUI seed widgets -- including custom ones like ComfyUI-Easy-Use's "easy
# seed" node -- reject values above 2**50 (1125899906842624): seen in
# practice as "Value ... bigger than max of 1125899906842624: seed" prompt
# validation errors from ComfyUI itself, which silently drops the whole
# output rather than clamping. random.randint(0, 2**63 - 1) routinely
# exceeded that, so stay under the tightest known limit rather than the
# full int64 range.
MAX_SEED_VALUE = 2**50 - 1


async def _selected_or_latest_output(db, node: Node) -> Asset | None:
    result = await db.execute(select(Asset).where(Asset.node_id == node.id).order_by(Asset.created_at))
    assets = list(result.scalars().all())
    if not assets:
        return None
    selected = [a for a in assets if a.selected]
    return selected[0] if selected else assets[-1]


async def _prev_asset_node_output(db, track_id, step_index) -> Asset | None:
    """Nearest asset-kind node before step_index in this track -- by construction
    of the asset/workflow alternation this is normally exactly step_index - 1."""
    result = await db.execute(
        select(Node)
        .where(Node.track_id == track_id, Node.step_index < step_index, Node.kind == NodeKind.asset)
        .order_by(Node.step_index.desc())
    )
    prev_node = result.scalars().first()
    if prev_node is None:
        return None
    return await _selected_or_latest_output(db, prev_node)


async def _asset_at_cell_index(db, node: Node, index: int) -> Asset | None:
    """Position-based input resolution for the row-span paradigm (mirrors
    the frontend's Grid.tsx effectiveRow/nodesByRowStep): slot `index` reads
    whatever asset node's row (its track's row_index) equals this workflow
    node's own home row (its track's row_index) + index, in the column
    right before it. A node's row is always exactly its track's row_index --
    "moving" a node to a different row means reassigning its track_id (see
    Grid.tsx's dropAssetAt/applyRowMove), never a display-only override.
    Deliberately independent of the frontend's span-sizing/collision
    calculation -- this only cares what's actually sitting in that one grid
    cell right now, not how big the workflow node's card is currently
    rendered as."""
    home_track = await db.get(Track, node.track_id)
    if home_track is None:
        return None
    target_row = home_track.row_index + index
    target_step = node.step_index - 1
    result = await db.execute(
        select(Node)
        .join(Track, Track.id == Node.track_id)
        .where(
            Track.project_id == home_track.project_id,
            Node.kind == NodeKind.asset,
            Node.step_index == target_step,
            Track.row_index == target_row,
        )
    )
    asset_node = result.scalars().first()
    if asset_node is None:
        return None
    return await _selected_or_latest_output(db, asset_node)


async def find_output_asset_node(db, track_id, step_index: int) -> Node | None:
    """The current (non-discarded) asset-kind node immediately after a workflow
    node at step_index -- the one place "this generation's picture-cell" lives.
    Always excludes discarded nodes: a re-roll leaves the old asset node behind
    marked discarded while a fresh one takes its place at the same step_index,
    so an unfiltered lookup could non-deterministically land on either one
    instead of the live cell actually shown in the UI. Shared by every call
    site that needs to find this node (see api/routes/nodes.py's reroll_node)
    so they can't drift out of sync on that filter again."""
    result = await db.execute(
        select(Node).where(
            Node.track_id == track_id,
            Node.step_index == step_index + 1,
            Node.kind == NodeKind.asset,
            Node.status != NodeStatus.discarded,
        )
    )
    return result.scalars().first()


async def _get_or_create_output_asset_node(db, workflow_node: Node, is_native: bool) -> Node:
    """A workflow node's results never attach to itself -- they materialize as
    the asset-kind node immediately after it in the track, created lazily on
    first result and reused by every variant job of the same workflow node.

    Native backends (is_native, see core/node_types.py) are deterministic and
    always run exactly one variant (enqueue_node_job forces requested_variants
    to 1 for them server-side) -- there's never more than one result to choose
    between, so materialize straight to a settled "asset.single" instead of
    an "asset.select" picker that would always have exactly one candidate to
    immediately select anyway."""
    asset_node = await find_output_asset_node(db, workflow_node.track_id, workflow_node.step_index)
    if asset_node is None:
        asset_node = Node(
            track_id=workflow_node.track_id,
            step_index=workflow_node.step_index + 1,
            kind=NodeKind.asset,
            status=NodeStatus.running,
            node_type="asset.single" if is_native else "asset.select",
            is_picker=not is_native,
        )
        db.add(asset_node)
        await db.flush()
    return asset_node


async def resolve_node_inputs(db, node: Node, param_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Merge Node.inputs (InputRef list, matched positionally to image/file fields
    in param_schema) with Node.params (direct values for the rest) into the flat
    dict a JobBackend.submit() expects.

    This runs once per variant job (see run_variant_job), so a field marked
    "seed" in param_schema gets a fresh random value on every call regardless
    of whatever's stored in Node.params -- requesting N variants means N
    different seeds instead of N identical (and thus ComfyUI-cached, see
    _wait_for_completion) runs of the same generation. The field is hidden
    from NodeCell's param form (frontend/src/components/NodeCell.tsx) since
    there's nothing meaningful for the user to set on it.

    param_schema comes from resolve_effective_template's EffectiveTemplate --
    works the same whether it's native (code registry) or template (DB) backed."""
    fields = (param_schema or {}).get("fields", [])
    slot_fields = [f["name"] for f in fields if f.get("type") in ("image", "file")]
    seed_fields = [f["name"] for f in fields if f.get("type") == "seed"]
    storage = get_storage()

    resolved: dict[str, Any] = dict(node.params or {})
    for field_name in seed_fields:
        resolved[field_name] = random.randint(0, MAX_SEED_VALUE)

    track = await db.get(Track, node.track_id)

    for i, field_name in enumerate(slot_fields):
        if i >= len(node.inputs or []):
            continue
        ref = node.inputs[i]
        ref_type = ref.get("type")

        if ref_type == "self_prev":
            asset = await _prev_asset_node_output(db, node.track_id, node.step_index)
        elif ref_type == "track_below_prev":
            result = await db.execute(
                select(Track).where(Track.project_id == track.project_id, Track.row_index == track.row_index + 1)
            )
            below = result.scalars().first()
            # Same step_index bound as self_prev (not +1): kind-per-step
            # (_kind_for_step in api/routes/nodes.py) is keyed off
            # Project.start_kind, the same for every track, so this workflow
            # node's own step_index is never an asset-kind step in *any*
            # track -- "below" needs no extra slack to skip past it.
            asset = await _prev_asset_node_output(db, below.id, node.step_index) if below else None
        elif ref_type in ("upload", "explicit"):
            asset_id = ref.get("asset_id") or ref.get("output_id")
            asset = await db.get(Asset, asset_id) if asset_id else None
        elif ref_type == "cell_index":
            idx = ref.get("index")
            asset = await _asset_at_cell_index(db, node, idx) if idx is not None else None
        elif ref_type == "text":
            resolved[field_name] = ref.get("value", "")
            continue
        else:
            asset = None

        if asset is not None:
            resolved[field_name] = storage.get_object(asset.storage_key)

    return resolved


async def enqueue_node_job(node_id: str) -> None:
    async with async_session_maker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            logger.warning("enqueue_node_job: node %s not found", node_id)
            return
        if node.kind != NodeKind.workflow:
            logger.warning("enqueue_node_job: node %s is not a workflow node", node_id)
            return
        effective = await resolve_effective_template(db, node)
        if effective:
            validate_params(effective.param_schema, node.params or {})

        settings = get_settings()
        variants = min(node.requested_variants, settings.max_variants_per_node)
        # Native execution is pure/deterministic (no seed field, no stochastic
        # backend) -- N variants would just be N pixel-identical outputs at
        # N times the CPU cost, so it's forced to 1 regardless of what the
        # node/UI requested.
        if effective and effective.is_native:
            variants = 1

        # Clicking "Generate" again on a node that already ran (allowed once
        # it's done/error -- see nodes.py's generate_node) previously left the
        # old Job rows in place: list_node_jobs returns *all* of a node's jobs
        # ever, with no batch/generation grouping to filter by, so NodeCell.tsx
        # kept rendering one progress bar per historical job, accumulating
        # forever across repeated runs. A fresh generate has nothing to say
        # about those, so clear them first.
        await db.execute(delete(Job).where(Job.node_id == node.id))

        node.status = NodeStatus.queued
        node.error = None
        jobs = []
        for i in range(variants):
            job = Job(node_id=node.id, variant_index=i, status=JobStatusEnum.pending)
            db.add(job)
            jobs.append(job)
        await db.commit()
        for job in jobs:
            await db.refresh(job)

        for job in jobs:
            await job_queue.enqueue(run_variant_job, str(job.id))


async def _poll_until_terminal(instance, external_job_id: str) -> BackendJobStatus:
    """listen_progress() (the WS path) can return without ever having seen the
    backend's completion event -- e.g. the socket closes early -- in which case
    this HTTP-polling fallback is what actually has to notice the job finished.
    Wrapping *this whole loop* (not each individual status() call) in the
    caller's wait_with_timeout is what bounds it: previously each status() call
    got its own fresh timeout budget, so a backend that kept answering "pending"
    forever (e.g. the prompt already dropped out of both /history and /queue)
    made this loop run indefinitely instead of ever timing out."""
    status = await instance.status(external_job_id)
    while status not in (BackendJobStatus.done, BackendJobStatus.error):
        await asyncio.sleep(2)
        status = await instance.status(external_job_id)
    return status


async def _wait_for_completion(instance: ComfyUIBackend, external_job_id: str, on_progress) -> BackendJobStatus:
    """Race the WS progress listener against HTTP status polling; act on
    whichever notices completion first.

    Needed because ComfyUI doesn't queue/replay events for a client that
    wasn't connected yet: our WS only opens *after* submit() returns, so a
    fast or cache-hit execution (e.g. an unchanged seed -- see
    resolve_node_inputs) can finish and emit its final "executing" event
    before that connection is even open. listen_progress() then has nothing
    left to receive and would otherwise sit there until job_timeout_seconds,
    even though the result has been sitting in /history the whole time."""
    listen_task = asyncio.create_task(instance.listen_progress(external_job_id, on_progress))
    poll_task = asyncio.create_task(_poll_until_terminal(instance, external_job_id))
    try:
        done, pending = await asyncio.wait({listen_task, poll_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        listen_task.cancel()
        poll_task.cancel()
        await asyncio.gather(listen_task, poll_task, return_exceptions=True)
        raise

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if poll_task in done:
        return poll_task.result()

    exc = listen_task.exception()
    if exc is not None:
        raise exc
    # listen_progress caught the terminal WS event but doesn't itself carry a
    # status -- one direct check now that we know execution is over.
    return await instance.status(external_job_id)


_STALL_POLL_INTERVAL_SECONDS = 60


async def _wait_with_stall_detection(
    instance: ComfyUIBackend, external_job_id: str, on_progress, job: Job, stall_seconds: int
) -> BackendJobStatus:
    """Replaces a flat wall-clock timeout on the generation itself: a job is only
    given up on once neither its execution progress (job.progress, updated by
    on_progress from WS messages) nor its position in ComfyUI's own /queue has
    moved for `stall_seconds`. That lets a single generation run for however
    long it actually needs -- minutes or hours -- while still catching a
    genuinely wedged backend (queue frozen, or executing but no progress
    events) within `stall_seconds` of it happening."""
    completion_task = asyncio.create_task(_wait_for_completion(instance, external_job_id, on_progress))
    loop = asyncio.get_running_loop()
    last_signal: tuple[int, int | None] | None = None
    last_change = loop.time()

    try:
        while True:
            done, _ = await asyncio.wait({completion_task}, timeout=_STALL_POLL_INTERVAL_SECONDS)
            if completion_task in done:
                return completion_task.result()

            position = await instance.queue_position(external_job_id)
            signal = (job.progress, position)
            now = loop.time()
            if signal != last_signal:
                last_signal = signal
                last_change = now
            elif now - last_change >= stall_seconds:
                completion_task.cancel()
                await asyncio.gather(completion_task, return_exceptions=True)
                raise TimeoutError(f"no progress for {stall_seconds}s -- backend considered stalled")
    except asyncio.CancelledError:
        completion_task.cancel()
        await asyncio.gather(completion_task, return_exceptions=True)
        raise


async def _materialize_job_result(db, job: Job, node: Node, effective, instance, external_job_id: str, project_id: str) -> None:
    """Fetches a finished job's output assets and marks it done -- shared by
    the normal dispatch path (run_variant_job) and orphan recovery
    (_resume_orphaned_job) once each has confirmed the backend actually
    finished successfully, so the two can never attach results differently."""
    settings = get_settings()
    assets = await wait_with_timeout(instance.result(external_job_id), settings.job_timeout_seconds)
    storage = get_storage()
    asset_node = await _get_or_create_output_asset_node(db, node, effective.is_native)
    for asset_ref in assets:
        key = storage.put_object(asset_ref.data, asset_ref.mime_type, prefix=f"nodes/{asset_node.id}")
        db.add(
            Asset(
                node_id=asset_node.id,
                storage_key=key,
                mime_type=asset_ref.mime_type,
                kind=asset_ref.kind,
                selected=False,
                meta=asset_ref.meta or {},
            )
        )

    job.status = JobStatusEnum.done
    job.progress = 100
    job.finished_at = datetime.now(UTC)
    await db.commit()
    await ws_manager.broadcast(
        project_id, {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "done", "progress": 100}
    )
    await ws_manager.broadcast(project_id, {"type": "node", "node_id": str(asset_node.id), "status": "running"})


async def run_variant_job(job_id: str, exclude_backend_ids: list[str] | None = None) -> None:
    settings = get_settings()
    exclude = set(exclude_backend_ids or [])

    async with async_session_maker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
        node = await db.get(Node, job.node_id)
        track = await db.get(Track, node.track_id)
        project_id = str(track.project_id)
        effective = await resolve_effective_template(db, node)

        if effective is None:
            # No template chosen yet (or it/its native handler no longer
            # exists) -- unlike "no backend available right now", retrying
            # this can never succeed, so fail the job outright instead of
            # looping into waiting_for_backend forever.
            job.status = JobStatusEnum.error
            job.error = "no node type/template resolved for this node"
            job.finished_at = datetime.now(UTC)
            node.status = NodeStatus.error
            node.error = job.error
            await db.commit()
            await ws_manager.broadcast(
                project_id, {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "error", "error": job.error}
            )
            return

        choice = await dispatcher.select_backend(
            db,
            effective,
            mode=node.backend_mode,
            manual_backend_id=str(node.manual_backend_id) if node.manual_backend_id else None,
            exclude_backend_ids=exclude,
        )

        if choice is None:
            job.status = JobStatusEnum.waiting_for_backend
            await db.commit()
            await ws_manager.broadcast(
                project_id, {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "waiting_for_backend"}
            )
            await job_queue.enqueue(
                run_variant_job, job_id, list(exclude), delay=settings.dispatch_poll_interval_seconds
            )
            return

        job.status = JobStatusEnum.running
        job.backend_id = choice.backend.id if choice.backend else None
        job.started_at = datetime.now(UTC)
        node.status = NodeStatus.running
        node.backend_used_id = choice.backend.id if choice.backend else None
        await db.commit()
        await ws_manager.broadcast(
            project_id,
            {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "running", "progress": 0},
        )

        try:
            resolved_inputs = await resolve_node_inputs(db, node, effective.param_schema)
            try:
                external_job_id = await wait_with_timeout(
                    choice.instance.submit(choice.capability.config if choice.capability else {}, resolved_inputs),
                    settings.job_timeout_seconds,
                )
            finally:
                # Release as soon as the backend's real queue reflects this job
                # (or definitely never will, on failure) -- see dispatcher._reserved.
                # Native has no reservation to release (no Backend row at all).
                if choice.backend:
                    await dispatcher.release_backend(str(choice.backend.id))
            job.external_job_id = external_job_id
            await db.commit()

            if isinstance(choice.instance, ComfyUIBackend):

                async def on_progress(message: dict) -> None:
                    if message.get("type") != "progress":
                        return
                    data = message.get("data", {})
                    total = data.get("max") or 1
                    pct = min(100, int(100 * data.get("value", 0) / total))
                    job.progress = pct
                    await db.commit()
                    await ws_manager.broadcast(
                        project_id,
                        {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "running", "progress": pct},
                    )

                status = await _wait_with_stall_detection(
                    choice.instance, external_job_id, on_progress, job, settings.stall_timeout_seconds
                )
            else:
                status = await wait_with_timeout(
                    _poll_until_terminal(choice.instance, external_job_id), settings.job_timeout_seconds
                )

            if status == BackendJobStatus.error:
                raise RuntimeError("backend reported execution error")

            await _materialize_job_result(db, job, node, effective, choice.instance, external_job_id, project_id)

        except Exception as exc:
            logger.exception("job %s failed", job_id)
            job.retries += 1
            job.error = str(exc)
            if job.retries <= settings.max_retries:
                job.status = JobStatusEnum.pending
                await db.commit()
                # A stall/timeout means this backend was slow, not necessarily
                # broken -- unlike a real execution error, don't exclude it. In
                # a single-backend setup, excluding here left nothing else to
                # pick, and the job sat in waiting_for_backend forever instead
                # of ever retrying (see the 2026-07-13 incident). Retries are
                # still capped by max_retries, so a backend that's actually
                # dead still ends in a terminal error rather than looping.
                # httpx.TransportError (ReadTimeout, ConnectError, ...) is not
                # a subclass of the builtin TimeoutError, so a transient
                # network blip during polling used to slip past this guard
                # and exclude the backend anyway (2026-07-14 recurrence).
                if choice.backend and not isinstance(exc, (TimeoutError, httpx.TransportError)):
                    exclude.add(str(choice.backend.id))
                await job_queue.enqueue(run_variant_job, job_id, list(exclude))
                return
            job.status = JobStatusEnum.error
            job.finished_at = datetime.now(UTC)
            await db.commit()
            await ws_manager.broadcast(
                project_id,
                {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "error", "error": str(exc)},
            )

        await _finalize_node_if_done(db, node.id, project_id)


async def _finalize_node_if_done(db, node_id, project_id: str) -> None:
    result = await db.execute(select(Job).where(Job.node_id == node_id))
    jobs = list(result.scalars().all())
    if not jobs or any(j.status in (JobStatusEnum.pending, JobStatusEnum.running, JobStatusEnum.waiting_for_backend) for j in jobs):
        return

    node = await db.get(Node, node_id)
    any_done = any(j.status == JobStatusEnum.done for j in jobs)
    any_error = any(j.status == JobStatusEnum.error for j in jobs)
    if any_done:
        node.status = NodeStatus.done
        node.error = None
    elif any_error:
        node.status = NodeStatus.error
        node.error = "all variants failed"
    else:
        # Every variant was cancelled before any of them produced a result --
        # a deliberate stop (see cancel_job), not a failure, so this doesn't
        # get badged as an error the way an all-failed run does.
        node.status = NodeStatus.discarded
        node.error = None

    # Flip the paired output asset-node's status alongside the workflow node that
    # feeds it -- it only exists once at least one variant produced a result.
    asset_node = await find_output_asset_node(db, node.track_id, node.step_index)
    if asset_node is not None:
        asset_node.status = node.status
        asset_node.error = "source workflow failed" if node.status == NodeStatus.error else None

    await db.commit()
    await ws_manager.broadcast(project_id, {"type": "node", "node_id": str(node_id), "status": node.status.value})
    if asset_node is not None:
        await ws_manager.broadcast(project_id, {"type": "node", "node_id": str(asset_node.id), "status": asset_node.status.value})


async def _fail_orphaned_job(db, job: Job, project_id: str, reason: str) -> None:
    """The fallback outcome recover_orphaned_jobs always had: nothing external
    to check (or reconnecting to what should be there failed), so unblock the
    cell by marking the job/node errored -- same as before this function grew
    a real-recovery path."""
    job.status = JobStatusEnum.error
    job.error = reason
    job.finished_at = datetime.now(UTC)
    node = await db.get(Node, job.node_id)
    if node and node.status in (NodeStatus.queued, NodeStatus.running):
        node.status = NodeStatus.error
        node.error = reason
    await db.commit()
    await ws_manager.broadcast(
        project_id, {"type": "job", "job_id": str(job.id), "node_id": str(job.node_id), "status": "error", "error": reason}
    )


async def _resume_orphaned_job(job_id: str) -> None:
    """Background continuation for an orphaned job recover_orphaned_jobs found
    still genuinely in flight on its backend (ComfyUI wasn't restarted along
    with us, so it may well still be working on it) -- polls it to completion
    via the same generic _poll_until_terminal every dispatch path uses, then
    finishes it exactly like a normal run. Doesn't resubmit anything; the
    backend is already working on it, this only reconnects and waits."""
    async with async_session_maker() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != JobStatusEnum.running:
            return
        node = await db.get(Node, job.node_id)
        track = await db.get(Track, node.track_id)
        project_id = str(track.project_id)
        effective = await resolve_effective_template(db, node)
        if effective is None or job.backend_id is None or job.external_job_id is None:
            await _fail_orphaned_job(db, job, project_id, "orphaned by server restart")
            await _finalize_node_if_done(db, node.id, project_id)
            return

        instance = await dispatcher.reconnect_instance(db, str(job.backend_id), effective.node_type_slug)
        if instance is None:
            await _fail_orphaned_job(db, job, project_id, "backend no longer available after restart")
            await _finalize_node_if_done(db, node.id, project_id)
            return

        settings = get_settings()
        try:
            status = await wait_with_timeout(
                _poll_until_terminal(instance, job.external_job_id), settings.job_timeout_seconds
            )
            if status == BackendJobStatus.error:
                raise RuntimeError("backend reported execution error")
            await _materialize_job_result(db, job, node, effective, instance, job.external_job_id, project_id)
        except Exception as exc:
            logger.exception("resumed orphaned job %s failed", job_id)
            await _fail_orphaned_job(db, job, project_id, str(exc))

        await _finalize_node_if_done(db, node.id, project_id)


async def recover_orphaned_jobs() -> None:
    """The in-process job queue (app/core/queue.py) starts every boot with zero
    tasks -- a job left mid-flight by a previous process (crash, dev --reload,
    or a deploy restart) has nothing left to resume it, and used to always be
    marked errored even though the real backend (ComfyUI, an API provider --
    a separate process we didn't just restart) might still be working on it,
    or have already finished while we were down.

    Called once at startup, before job_queue.start() (queuing a follow-up here
    is still safe -- InProcessQueue.enqueue just puts onto an asyncio.Queue,
    which buffers fine before any worker is consuming it yet). A job only
    gets reconnected if it actually reached a real backend (backend_id +
    external_job_id both set) -- native execution has no external state to
    survive a restart at all, and a job that hadn't gotten that far yet
    (pending, or waiting_for_backend/running before submit() completed) has
    nothing out there to check either. Those still fall back to the old
    "orphaned by server restart" error, same as before this function learned
    to actually reconnect."""
    async with async_session_maker() as db:
        result = await db.execute(
            select(Job).where(Job.status.in_((JobStatusEnum.pending, JobStatusEnum.running, JobStatusEnum.waiting_for_backend)))
        )
        jobs = list(result.scalars().all())
        if not jobs:
            return

        recovered = 0
        resumed = 0
        failed = 0

        for job in jobs:
            node = await db.get(Node, job.node_id)
            track = await db.get(Track, node.track_id) if node else None
            project_id = str(track.project_id) if track else None

            recoverable = (
                job.status == JobStatusEnum.running
                and job.backend_id is not None
                and job.external_job_id is not None
                and node is not None
                and project_id is not None
            )
            if not recoverable:
                if project_id is not None:
                    await _fail_orphaned_job(db, job, project_id, "orphaned by server restart")
                    await _finalize_node_if_done(db, node.id, project_id)
                failed += 1
                continue

            try:
                effective = await resolve_effective_template(db, node)
                instance = (
                    await dispatcher.reconnect_instance(db, str(job.backend_id), effective.node_type_slug)
                    if effective is not None
                    else None
                )
                if effective is None or instance is None:
                    await _fail_orphaned_job(db, job, project_id, "orphaned by server restart")
                    await _finalize_node_if_done(db, node.id, project_id)
                    failed += 1
                    continue

                status = await instance.status(job.external_job_id)
                if status == BackendJobStatus.done:
                    await _materialize_job_result(db, job, node, effective, instance, job.external_job_id, project_id)
                    await _finalize_node_if_done(db, node.id, project_id)
                    recovered += 1
                elif status == BackendJobStatus.error:
                    await _fail_orphaned_job(db, job, project_id, "backend reported execution error")
                    await _finalize_node_if_done(db, node.id, project_id)
                    failed += 1
                else:
                    # Still genuinely in flight on the backend -- resume
                    # waiting on it in the background instead of deciding
                    # anything about it here (this loop shouldn't block
                    # server startup on a long poll). Not yet terminal, so no
                    # _finalize_node_if_done call -- _resume_orphaned_job does
                    # that itself once it actually reaches an outcome.
                    await job_queue.enqueue(_resume_orphaned_job, str(job.id))
                    resumed += 1
            except Exception:
                logger.exception("couldn't reconnect orphaned job %s to its backend", job.id)
                await _fail_orphaned_job(db, job, project_id, "orphaned by server restart")
                await _finalize_node_if_done(db, node.id, project_id)
                failed += 1

        logger.warning(
            "orphaned job recovery: %d reconnected as done, %d resumed in background, %d failed/unrecoverable (of %d total)",
            recovered,
            resumed,
            failed,
            len(jobs),
        )


async def cancel_job(job_id: str) -> None:
    async with async_session_maker() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status not in (JobStatusEnum.running, JobStatusEnum.pending, JobStatusEnum.waiting_for_backend):
            return
        node = await db.get(Node, job.node_id)
        track = await db.get(Track, node.track_id)

        if job.backend_id and job.external_job_id:
            backend = await db.get(Backend, job.backend_id)
            if backend and backend.base_url:
                try:
                    await ComfyUIBackend(base_url=backend.base_url).cancel(job.external_job_id)
                except Exception:
                    logger.warning("cancel failed for job %s on backend %s", job_id, backend.id, exc_info=True)

        job.status = JobStatusEnum.cancelled
        job.finished_at = datetime.now(UTC)
        await db.commit()
        await ws_manager.broadcast(str(track.project_id), {"type": "job", "job_id": str(job.id), "node_id": str(node.id), "status": "cancelled"})
        await _finalize_node_if_done(db, node.id, str(track.project_id))
