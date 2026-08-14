"""ComfyUIBackend: JobBackend implementation wrapping one ComfyUI instance's
HTTP/WS API.

Reference endpoints on a stock ComfyUI instance:
  POST /prompt              submit a workflow, returns {"prompt_id": ...}
  GET  /history/{prompt_id} completed job outputs (empty dict while pending)
  GET  /view?filename=&subfolder=&type=  download a generated file
  POST /upload/image        upload an input image, returns {"name": ...}
  GET  /queue                current running/pending queue
  POST /interrupt            stop whatever is currently executing
  POST /queue {"delete":[id]} remove a still-queued prompt without running it
  GET  /system_stats          heartbeat / liveness info
  GET  /object_info/{class}   one node class's input definitions (combo options)
  WS   /ws?clientId=          progress + status events
"""
import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from io import BytesIO
from typing import Any

import httpx
import websockets
from PIL import Image

from app.core.job_backend import AssetRef, CapacityInfo, JobStatus
from app.core.template_engine import build_workflow

logger = logging.getLogger(__name__)


class ComfyUIBackend:
    def __init__(self, base_url: str, client_id: str | None = None) -> None:
        base_url = base_url.rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"  # backends are entered as "host:port"; httpx requires an explicit scheme
        self.base_url = base_url
        self.client_id = client_id or str(uuid.uuid4())

    @property
    def _ws_url(self) -> str:
        return self.base_url.replace("https://", "wss://").replace("http://", "ws://") + f"/ws?clientId={self.client_id}"

    async def submit(self, execution_config: dict, inputs: dict[str, Any]) -> str:
        workflow_json = execution_config["workflow_json"]
        param_mapping = execution_config.get("param_mapping", {})

        resolved: dict[str, Any] = {}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            for field_name, value in inputs.items():
                if isinstance(value, (bytes, bytearray)):
                    resolved[field_name] = await self._upload_image(client, value)
                else:
                    resolved[field_name] = value

            workflow = build_workflow(workflow_json, param_mapping, resolved)
            resp = await client.post("/prompt", json={"prompt": workflow, "client_id": self.client_id})
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"ComfyUI rejected prompt: {data['error']}")
            return data["prompt_id"]

    async def _upload_image(self, client: httpx.AsyncClient, data: bytes) -> str:
        files = {"image": (f"{uuid.uuid4()}.png", data, "image/png")}
        resp = await client.post("/upload/image", files=files)
        resp.raise_for_status()
        return resp.json()["name"]

    @staticmethod
    async def _queue_snapshot(client: httpx.AsyncClient) -> tuple[list[str], list[str]]:
        """Shared GET /queue fetch+parse -- status/queue_position/capacity/cancel
        all need this same (running_ids, pending_ids) pair. Kept in ComfyUI's own
        queue order (not a set) since queue_position needs .index() on it;
        membership-only callers can use `in` on the list just as well."""
        resp = await client.get("/queue")
        resp.raise_for_status()
        queue = resp.json()
        running_ids = [item[1] for item in queue.get("queue_running", [])]
        pending_ids = [item[1] for item in queue.get("queue_pending", [])]
        return running_ids, pending_ids

    async def status(self, job_id: str) -> JobStatus:
        # A transient network blip here must not read as "job failed" --
        # _poll_until_terminal's own loop already retries this call every 2s
        # regardless, so swallowing a transport error and reporting "still
        # pending" just lets that natural retry happen instead of blowing up
        # the whole job over one dropped response. A backend that's actually
        # gone (not just one flaky request) is caught separately and much
        # faster by _wait_with_stall_detection's queue_position check in
        # worker/tasks.py -- this method deliberately doesn't duplicate that.
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
                history = await self._get_history_entry(client, job_id)
                if history is not None:
                    status_info = history.get("status", {})
                    if status_info.get("status_str") == "error" or any(
                        m[0] == "execution_error" for m in status_info.get("messages", [])
                    ):
                        return JobStatus.error
                    if history.get("outputs"):
                        return JobStatus.done

                running_ids, pending_ids = await self._queue_snapshot(client)
                if job_id in running_ids:
                    return JobStatus.running
                if job_id in pending_ids:
                    return JobStatus.pending

                # Neither in the queue nor still running/pending there: if we do have
                # a history entry, the prompt genuinely ran and produced no outputs
                # (a real failure ComfyUI didn't flag via status_str/execution_error
                # -- see _poll_until_terminal's docstring in worker/tasks.py for the
                # observed case). No history at all just means it hasn't been picked
                # up yet, so treat that as still pending rather than an error.
                return JobStatus.error if history is not None else JobStatus.pending
        except httpx.TransportError:
            logger.warning("status check failed for %s on %s", job_id, self.base_url, exc_info=True)
            return JobStatus.pending

    async def error_detail(self, job_id: str) -> str | None:
        """Pulls the "execution_error" message's exception_message/node_type
        out of history["status"]["messages"] -- the same payload status()
        already inspects for status_str/execution_error, just read for its
        content this time instead of merely its presence. None if history is
        gone (evicted, or the prompt never got that far) or carries no such
        message -- callers fall back to a generic string in that case."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            history = await self._get_history_entry(client, job_id)
            if history is None:
                return None
            for msg_type, data in history.get("status", {}).get("messages", []):
                if msg_type == "execution_error":
                    node_type = data.get("node_type")
                    exception_message = data.get("exception_message")
                    if node_type and exception_message:
                        return f"{node_type}: {exception_message}"
                    return exception_message or node_type
            return None

    async def queue_position(self, job_id: str) -> int | None:
        """0 if currently executing, else this job's 0-based distance from the
        front of ComfyUI's pending queue, or None if it's not in the queue at
        all (done/errored/dropped). Used by the stall watchdog in
        worker/tasks.py to tell "queue is progressing, just slowly" apart from
        "queue is wedged" -- a flat execution-progress check alone would see no
        movement at all while a job is still waiting its turn behind others.

        Deliberately lets httpx.TransportError propagate rather than
        swallowing it here: the caller (_wait_with_stall_detection) needs to
        tell "backend didn't answer this one time" apart from "genuinely not
        in the queue" so it can fail fast on a backend that's actually gone
        instead of only noticing via the much longer no-progress window."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            running_ids, pending_ids = await self._queue_snapshot(client)
            if job_id in running_ids:
                return 0
            if job_id in pending_ids:
                return pending_ids.index(job_id)
            return None

    async def _get_history_entry(self, client: httpx.AsyncClient, job_id: str) -> dict | None:
        resp = await client.get(f"/history/{job_id}")
        resp.raise_for_status()
        data = resp.json()
        return data.get(job_id)

    @staticmethod
    def _mask_save_node_ids(prompt_graph: dict[str, dict]) -> set[str]:
        """Which SaveImage node ids are fed directly by a MaskToImage -- those
        are visualizing a MASK, not a real generated picture (see the "get
        mask as its own asset" design thread), so result() stores them as
        AssetKind.mask and shrinks them back to one channel before storage
        instead of keeping the 3x-duplicated RGB MaskToImage produces just to
        satisfy SaveImage's IMAGE input. Graph-structural (one hop up the
        same history["prompt"] graph the save_node_ids filter above already
        walks), not name/title based -- works regardless of what the node
        happens to be titled."""
        ids: set[str] = set()
        for node_id, node in prompt_graph.items():
            if not isinstance(node, dict) or node.get("class_type") != "SaveImage":
                continue
            source = node.get("inputs", {}).get("images")
            if isinstance(source, list) and len(source) == 2 and isinstance(source[0], str):
                upstream = prompt_graph.get(source[0])
                if isinstance(upstream, dict) and upstream.get("class_type") == "MaskToImage":
                    ids.add(node_id)
        return ids

    @staticmethod
    def _flatten_to_grayscale_png(data: bytes) -> bytes:
        """Collapses MaskToImage's R/G/B-duplicated PNG back to a single
        grayscale channel before it ever reaches storage -- same "don't pay
        for channels the mask never had" instinct as native.mask's own
        capped bilevel PNGs (core/native_backend.py), just applied to a mask
        that came out of a ComfyUI graph instead of the frontend's paint
        canvas."""
        image = Image.open(BytesIO(data)).convert("L")
        buf = BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    async def result(self, job_id: str) -> list[AssetRef]:
        assets: list[AssetRef] = []
        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            history = await self._get_history_entry(client, job_id)
            if not history:
                return assets

            # history["outputs"] has an entry for every OUTPUT_NODE in the
            # graph, not just the one meant as the deliverable -- a leftover
            # PreviewImage used to eyeball a crop/reference while building the
            # workflow in ComfyUI's own UI shows up here too, and without this
            # filter its image leaks into the job's result assets alongside
            # (or instead of) the real SaveImage output. history["prompt"] is
            # [number, prompt_id, {node_id: node, ...}, ...] -- the exact graph
            # that was submitted, so it's an honest source of each node's
            # class_type regardless of what workflow_json in our own capability
            # config looked like at edit time. Only "images" is gated on this --
            # no capability actually produces meshes yet, so there's no known
            # save-node class_type to allow-list for that branch.
            prompt = history.get("prompt")
            save_node_ids: set[str] | None = None
            mask_node_ids: set[str] = set()
            if isinstance(prompt, list) and len(prompt) > 2 and isinstance(prompt[2], dict):
                graph = prompt[2]
                save_node_ids = {
                    node_id
                    for node_id, node in graph.items()
                    if isinstance(node, dict) and node.get("class_type") == "SaveImage"
                }
                mask_node_ids = self._mask_save_node_ids(graph)

            for node_id, node_output in history.get("outputs", {}).items():
                is_save_node = save_node_ids is None or node_id in save_node_ids
                for image in node_output.get("images", []) if is_save_node else []:
                    resp = await client.get(
                        "/view",
                        params={
                            "filename": image["filename"],
                            "subfolder": image.get("subfolder", ""),
                            "type": image.get("type", "output"),
                        },
                    )
                    resp.raise_for_status()
                    mime_type = resp.headers.get("content-type", "image/png")
                    data = resp.content
                    kind = "image"
                    if node_id in mask_node_ids:
                        kind = "mask"
                        data = self._flatten_to_grayscale_png(data)
                        mime_type = "image/png"
                    assets.append(AssetRef(data=data, mime_type=mime_type, kind=kind, meta=image))
                for mesh_key in ("3d", "meshes", "gltf"):
                    for mesh in node_output.get(mesh_key, []):
                        resp = await client.get(
                            "/view",
                            params={
                                "filename": mesh["filename"],
                                "subfolder": mesh.get("subfolder", ""),
                                "type": mesh.get("type", "output"),
                            },
                        )
                        resp.raise_for_status()
                        assets.append(
                            AssetRef(
                                data=resp.content,
                                mime_type=resp.headers.get("content-type", "model/gltf-binary"),
                                kind="mesh",
                                meta=mesh,
                            )
                        )
        return assets

    async def capacity(self) -> CapacityInfo:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
                resp = await client.get("/queue")
                resp.raise_for_status()
                queue = resp.json()
                length = len(queue.get("queue_running", [])) + len(queue.get("queue_pending", []))
                # Cap the dispatcher's view of this backend's room to 1 in-flight job --
                # ComfyUI's own /prompt queue would happily buffer more, but stacking jobs
                # there commits them to this instance before a second instance can spin up
                # and take a share (see select_backend's max_queue_length check).
                return CapacityInfo(is_alive=True, queue_length=length, max_queue_length=1)
        except Exception:
            logger.warning("ComfyUI backend %s unreachable", self.base_url, exc_info=True)
            return CapacityInfo(is_alive=False, queue_length=0)

    async def cancel(self, job_id: str) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            resp = await client.get("/queue")
            resp.raise_for_status()
            queue = resp.json()
            running_ids = {item[1] for item in queue.get("queue_running", [])}
            if job_id in running_ids:
                await client.post("/interrupt")
            else:
                await client.post("/queue", json={"delete": [job_id]})

    async def fetch_object_info(self, class_types: Iterable[str]) -> dict[str, dict]:
        """class_type -> that node class's ComfyUI input definition, for the
        classes asked about. Used by the template wizard to learn which widgets
        are combos and what their options are -- a workflow.json records only
        the *chosen* value ("4:5 (Artistic Frame)"), never the list it came
        from, so this is the only place the options exist.

        Asks per class rather than fetching the whole /object_info: the full
        payload is megabytes of every installed node, and the wizard only cares
        about a handful. A class the instance doesn't have installed answers
        {} -- expected, not an error (a custom node can be present on one
        backend and missing on another, which is exactly why this is asked of
        the backend the capability is being created for). Failures are
        swallowed: options are an enhancement, and the wizard stays usable
        against an unreachable instance.
        """
        found: dict[str, dict] = {}
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
                for class_type in dict.fromkeys(class_types):  # de-dupe, keep order
                    try:
                        resp = await client.get(f"/object_info/{class_type}")
                        resp.raise_for_status()
                        entry = resp.json().get(class_type)
                    except Exception:
                        logger.debug("object_info for %s failed on %s", class_type, self.base_url, exc_info=True)
                        continue
                    if isinstance(entry, dict):
                        found[class_type] = entry
        except Exception:
            logger.warning("ComfyUI backend %s unreachable while reading object_info", self.base_url, exc_info=True)
        return found

    async def heartbeat(self) -> dict:
        """Used by the worker's periodic heartbeat task. Returns raw /system_stats payload."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            resp = await client.get("/system_stats")
            resp.raise_for_status()
            return resp.json()

    async def listen_progress(self, prompt_id: str, on_progress) -> None:
        """Connect to the instance's WS endpoint and call on_progress(dict) for every
        message concerning prompt_id, until execution finishes or errors out."""
        async with websockets.connect(self._ws_url, max_size=None) as ws:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue  # binary frames carry preview images, not JSON progress
                message = json.loads(raw)
                data = message.get("data", {})
                if data.get("prompt_id") not in (None, prompt_id):
                    continue
                await on_progress(message)
                if message.get("type") == "executing" and data.get("node") is None and data.get("prompt_id") == prompt_id:
                    return
                if message.get("type") == "execution_error" and data.get("prompt_id") == prompt_id:
                    return


async def wait_with_timeout(coro, timeout_seconds: int):
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"operation exceeded {timeout_seconds}s")
