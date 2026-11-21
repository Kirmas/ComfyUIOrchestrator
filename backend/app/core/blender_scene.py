"""Live Blender scenes: a workshop an agent opens, works in for as long as it
needs, takes a result out of, and closes.

Why not a Blender per edit: a start-up is ~0.6 s and re-importing a model more,
so a hundred small edits would take minutes. Why not one Blender kept for
good: it holds 0.3-6 GB and the person asked for the opposite of a service that
lingers. So: one background Blender per scene (blender_scene_server.py), a
checkpoint .blend written after every successful call, an idle close, and a
memory cap -- all enforced HERE, never trusted to the agent.

State lives on disk, one directory per scene under <cache_dir>/scenes/<id>/
(meta.json, scene.blend, in/, renders/, blender.log). That is what the "Scenes"
page lists and deletes; nothing about a scene is in the database, because a
scene is disposable by design -- what is worth keeping leaves through
export() as an ordinary orchestrator asset. A scene's directory is therefore
also safe to delete by hand.

A deploy restarts the API unit and systemd stops its children with it, so a
scene is always "closed, checkpoint intact" after one, and the next call
reopens it (python variables from earlier calls are gone; the scene is not).

Measured limits behind the settings (core/blender_scene notes in memory
blender_node_and_3d_split.md): a 6M-triangle mesh costs 2.1 GB to import and
5.7 GB to decimate and Blender never hands the memory back -- hence the
automatic simplification of dense inputs (mesh_simplify.py), the soft limit
that restarts a bloated scene from its checkpoint, and the hard limit that
kills it before it can take the API down with it.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import get_settings
from app.core.blender import find_blender_binary
from app.core.mesh_simplify import glb_triangle_count

logger = logging.getLogger(__name__)
SERVER_SCRIPT = Path(__file__).with_name("blender_scene_server.py")
BACKEND_DIR = Path(__file__).resolve().parents[2]
_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_GPU_ERROR = re.compile(r"GPU|Vulkan|OpenGL|EGL|GHOST|no display", re.I)
_MIME_EXT = {
    "model/gltf-binary": ".glb", "model/stl": ".stl", "model/gltf+json": ".gltf", "image/png": ".png", "image/jpeg": ".jpg",
    "image/webp": ".webp", "model/obj": ".obj", "model/stl": ".stl", "model/fbx": ".fbx",
}
_IMPORTABLE = {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply"}
EXPORT_FORMATS = {"glb": "model/gltf-binary", "fbx": "application/octet-stream", "obj": "model/obj"}


class SceneError(Exception):
    """A problem worth telling the caller about verbatim (it is the API's `detail`)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class SceneNotFound(SceneError):
    def __init__(self, scene_id: str):
        super().__init__(f"no scene {scene_id!r} (list_scenes shows the open ones)", 404)


SCENE_GUIDE = """\
BLENDER SCENES -- how to work (Python `bpy`, background Blender, no UI)

Lifecycle: scene_create(asset_ids) -> work with scene_run / scene_render /
scene_info -> scene_export (result becomes an orchestrator asset) ->
scene_destroy. A scene closes itself after ~15 idle minutes but keeps its last
checkpoint for a week; any call reopens it. At most a couple of scenes are
open at once -- destroy yours when done.

Inputs: every asset you pass is written to a file. GLB/FBX/OBJ/STL/PLY are
imported automatically; images (a reference, a painted mask) are not --
INPUTS lists them all as {asset_id, kind, path, objects, ...}. A model denser
than ~1M triangles is first simplified to ~300k (the original asset is never
touched); the input's `note` says so. Rigged/animated/multi-part models are
never simplified.

scene_run(code) executes in ONE persistent namespace: variables and imports
survive between calls (unless the scene was restarted -- see `recycled` in the
reply). stdout is returned; an exception is returned with its traceback and
leaves earlier changes in place. A call may run up to its timeout (default
120 s); a call that overruns is killed, and the scene returns to the
checkpoint of the last SUCCESSFUL call.
Pillow/requests are NOT available inside Blender; numpy and mathutils are.

Predefined names:
  bpy, np, math, Vector, INPUTS
  objects() / meshes()            content objects (helper camera+lights excluded)
  info()                          dict: objects, vertices/faces, modifiers, bones, actions, bounds
  bbox(objs=None)                 world-space (min, max) of content (evaluated, so posed rigs count)
  import_file(path)               import by extension; returns new object names
  set_view(view, margin, ortho)   aim the helper camera at all content.
                                  view: front back left right top bottom three_quarter
                                  three_quarter_back, an (x,y,z) direction, or "current"
  render_to(path, view="front", engine="workbench"|"eevee"|"cycles", size=512,
            frame=None, ortho=False)   -> PNG. workbench is instant and the default.
  verts_under_mask(mask_png, obj)  vertex indices a person painted (white) on a render
  grow(obj, idx, hops)            widen a vertex selection by edges
  falloff(obj, idx, radius, power) per-vertex weights 1..0 around a selection
  export_to(path, fmt)            glb | fbx | obj (scene_export does this for you)
Posing (rigs):
  pose_report(focus=None, frame=None)  the pose in words and cm (= scene_pose tool)
  hands()                         bone names that look like hands
  pose_hand(hand, "fist", key=False, index=0)   preset (open flat relaxed fist grip
                                  point pinch) + per-finger overrides: curl or (curl, spread)
  pose_finger(hand, "index", curl=0.5, spread=0, key=False)
                                  curl 0 straight .. 1 into the palm; spread + = toward thumb.
                                  No bone axes involved -- prefer these to raw rotations.
  hand_frame(hand_bone)           world axes of a hand in the current pose: origin,
                                  along (to the knuckles), across (to the thumb),
                                  palm (OUT of the palm), fingers {thumb: [pose bones]..}
  palm_flip(hand_bone)            if every finger reads as bent "backwards", the palm
                                  side was guessed wrong -- flip it (saved with the scene)
  find_bone(name) -> (armature, pose bone)
  set_view/render_to(..., focus=<bone>)  close-up on that bone and its children
Blender is Z-up; imported models face -Y, so "front" looks at their face.

Rules that matter:
 * Set the render resolution BEFORE set_view/render_to -- framing depends on
   the aspect. verts_under_mask must run with the same camera and size the mask
   was painted on (render first, then call it).
 * A mask is a hint of WHERE, and it only reaches the camera-facing surface.
   Before deforming, grow() it and use falloff() weights so the back of the part
   moves with the front; check the result from another view.
 * scene_render shows you the picture -- LOOK at it after every real change
   instead of trusting the numbers. For animation pass frames=[...] and several
   views to get a contact sheet.
 * Prefer bpy.data / modifiers / bmesh / numpy (obj.data.vertices.foreach_get /
   foreach_set) over bpy.ops, which depend on context and selection.
 * HANDS: never trust a finger pose you have not looked at up close. After
   posing, call scene_pose(focus=<hand>) and fix every warning (a finger bent
   backwards, bent sideways, inside a prop), then
   scene_render(focus=<hand>, views=["palm", "thumb_side"]). Set fingers with
   pose_hand / pose_finger, not guessed local bone axes -- bone roll differs
   between rigs. For anything they can't express, rotate about hand_frame()'s
   axes (curl = about along x palm, so the tip moves toward the palm). For an animation, scene_pose(frames=[...]) checks every
   key at once.
 * Modifiers are applied on export; you need not apply them first.
 * Heavy full-mesh operations (smooth/decimate on millions of faces) inflate
   Blender's memory and it never gives it back; the scene is then restarted
   from its checkpoint automatically.
Example -- lengthen whatever the mask covers by 20%:
    o = meshes()[0]
    sel = grow(o, verts_under_mask(INPUTS[1]["path"], o), 4)
    w = falloff(o, sel)
    n = len(o.data.vertices); co = np.empty(n*3, np.float32)
    o.data.vertices.foreach_get("co", co); co = co.reshape(-1, 3)
    co[:, 2] += w * 0.2 * (co[sel, 2].max() - co[sel, 2].min())
    o.data.vertices.foreach_set("co", co.reshape(-1)); o.data.update()
"""


@dataclass
class SceneMeta:
    id: str
    name: str
    created_at: float
    last_activity: float
    state: str = "closed"  # starting | running | closed
    owner: str | None = None
    pid: int | None = None
    inputs: list[dict] = field(default_factory=list)
    runs: int = 0
    renders: int = 0
    peak_rss_mb: int = 0
    error: str | None = None


@dataclass
class SceneInput:
    asset_id: str
    kind: str
    mime: str
    data: bytes


def _now() -> float:
    return time.time()


def _mem_available_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 1 << 30


def _rss_mb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _pid_is_scene(pid: int | None, scene_dir: Path) -> bool:
    """True only for a live process that is really THIS scene's Blender (guards
    against a recycled pid; a zombie has an empty cmdline and counts as dead)."""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace")
    except OSError:
        return False
    return "blender_scene_server.py" in cmd and str(scene_dir) in cmd


def _tail(path: Path, chars: int = 1500) -> str:
    try:
        return path.read_text(errors="replace")[-chars:].strip()
    except OSError:
        return ""


def _child_env(home: Path) -> dict[str, str]:
    """No inherited secrets (the unit's EnvironmentFile carries API_TOKEN and the
    database URL), a throwaway HOME, UTF-8."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8"}


class SceneManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._kill_reason: dict[str, str] = {}
        self._create_lock = asyncio.Lock()
        self._last_ttl_sweep = 0.0

    # ----------------------------------------------------------- paths/meta --
    @property
    def root(self) -> Path:
        root = Path(get_settings().cache_dir) / "scenes"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _dir(self, scene_id: str) -> Path:
        if not _ID_RE.match(scene_id or ""):
            raise SceneNotFound(str(scene_id)[:40])
        return self.root / scene_id

    def _lock(self, scene_id: str) -> asyncio.Lock:
        return self._locks.setdefault(scene_id, asyncio.Lock())

    def _load(self, scene_id: str) -> SceneMeta:
        path = self._dir(scene_id) / "meta.json"
        try:
            return SceneMeta(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            raise SceneNotFound(scene_id) from None

    def _save(self, meta: SceneMeta) -> None:
        path = self._dir(meta.id) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meta)))
        os.replace(tmp, path)

    def _live(self, meta: SceneMeta) -> bool:
        return meta.state in ("starting", "running") and _pid_is_scene(meta.pid, self._dir(meta.id))

    def _live_count(self) -> int:
        count = 0
        for path in self.root.iterdir():
            try:
                meta = self._load(path.name)
                if self._live(meta) or meta.state == "starting":
                    count += 1
            except SceneError:
                continue
        return count

    # --------------------------------------------------------------- create --
    async def create(self, inputs: list[SceneInput], name: str | None = None, owner: str | None = None,
                     max_tris: int | None = None) -> dict:
        settings = get_settings()
        if find_blender_binary() is None:
            raise SceneError("Blender is not installed on this server", 503)
        if not inputs:
            raise SceneError("pass at least one asset (a mesh to work on)")
        async with self._create_lock:
            self._check_capacity()
            scene_id = uuid.uuid4().hex[:12]
            scene_dir = self._dir(scene_id)
            (scene_dir / "in").mkdir(parents=True)
            (scene_dir / "renders").mkdir()
            meta = SceneMeta(id=scene_id, name=(name or "scene")[:80], created_at=_now(), last_activity=_now(),
                             state="starting", owner=owner)
            self._save(meta)
        try:
            limit = settings.blender_simplify_over_tris if max_tris is None else max_tris
            server_inputs = []
            for n, item in enumerate(inputs):
                entry = await self._write_input(scene_dir, n, item, limit)
                meta.inputs.append({k: v for k, v in entry.items() if k != "path"})
                server_inputs.append(entry)
            (scene_dir / "inputs.json").write_text(json.dumps(server_inputs))
            self._save(meta)
            async with self._lock(scene_id):
                await self._start(meta, "fresh")
                reply = await self._exec(meta, {"op": "run", "code": "import_inputs()\n_ensure_stage()", "save": True}, 600)
                if not reply["ok"]:
                    raise SceneError("importing the inputs failed:\n" + reply["out"][-1200:])
                await self._render_preview(meta)
            return await self.summary(scene_id)
        except BaseException:
            await self._destroy_files(scene_id)
            raise

    def _check_capacity(self) -> None:
        settings = get_settings()
        if self._live_count() >= settings.blender_max_scenes:
            raise SceneError(
                f"{settings.blender_max_scenes} scene(s) are already open -- scene_destroy (or close) one first; "
                "list_scenes shows them", 409)
        free = _mem_available_mb()
        if free < settings.blender_min_free_mb:
            raise SceneError(f"only {free} MB of memory is free; not starting another Blender", 503)

    async def _write_input(self, scene_dir: Path, n: int, item: SceneInput, limit: int) -> dict:
        data = item.data
        if data[:4] == b"glTF":
            ext = ".glb"
        elif data[:18] == b"Kaydara FBX Binary":
            ext = ".fbx"
        else:
            ext = _MIME_EXT.get(item.mime, ".bin")
        path = scene_dir / "in" / f"{n}{ext}"
        entry = {"asset_id": item.asset_id, "kind": item.kind, "path": str(path), "file": path.name,
                 "auto_import": ext in _IMPORTABLE, "objects": []}
        await asyncio.to_thread(path.write_bytes, data)
        if ext == ".glb":
            try:
                tris = glb_triangle_count(data)
            except Exception as exc:  # a GLB we can't read here may still import in Blender
                entry["note"] = f"could not read triangle count ({exc})"
                return entry
            entry["triangles"] = tris
            if limit and tris and tris > limit:
                await self._simplify(path, scene_dir, get_settings().blender_simplify_target_tris, entry)
        return entry

    async def _simplify(self, path: Path, scene_dir: Path, target: int, entry: dict) -> None:
        out = path.with_name(path.stem + ".simplified.glb")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.core.mesh_simplify", str(path), str(out), str(target),
            cwd=str(BACKEND_DIR), env={"PATH": "/usr/bin:/bin", "HOME": str(scene_dir)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            entry["note"] = "simplification timed out; using the full-detail mesh"
            return
        try:
            report = json.loads(stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            entry["note"] = "simplification failed; using the full-detail mesh: " + stderr.decode()[-200:]
            return
        if report.get("written"):
            path.unlink()  # the full-detail copy is not needed: the asset itself is untouched
            out.rename(path)
            entry["simplified_from"] = report["before"]
            entry["triangles"] = report["after"]
            entry["note"] = (f"simplified from {report['before']:,} to {report['after']:,} triangles for working; "
                             "the original asset is unchanged")
        elif report.get("skipped"):
            entry["note"] = f"left at full detail ({report['skipped']})"

    # -------------------------------------------------------------- process --
    async def _start(self, meta: SceneMeta, mode: str) -> None:
        settings = get_settings()
        blender = find_blender_binary()
        if blender is None:
            raise SceneError("Blender is not installed on this server", 503)
        scene_dir = self._dir(meta.id)
        sock = scene_dir / "s.sock"
        if len(str(sock)) > 100:
            raise SceneError("the scenes directory path is too long for a unix socket; shorten cache_dir", 500)
        if sock.exists():
            sock.unlink()
        argv = [blender, "-b", "--factory-startup", "-noaudio"]
        if mode == "resume":
            argv.append(str(scene_dir / "scene.blend"))
        argv += ["--python-exit-code", "1", "-P", str(SERVER_SCRIPT), "--", str(sock),
                 str(settings.blender_scene_idle_s + 120), str(scene_dir), mode]
        log = open(scene_dir / "blender.log", "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(scene_dir), env=_child_env(scene_dir), stdout=log, stderr=log, start_new_session=True)
        finally:
            log.close()
        self._procs[meta.id] = proc
        meta.pid, meta.state, meta.error = proc.pid, "starting", None
        self._save(meta)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                meta.state, meta.pid = "closed", None
                self._save(meta)
                raise SceneError("Blender exited while starting:\n" + _tail(scene_dir / "blender.log"), 500)
            if sock.exists():
                try:
                    await self._rpc(meta.id, {"op": "ping"}, 5)
                    meta.state = "running"
                    self._save(meta)
                    logger.info("blender scene %s (%s) started (%s)", meta.id, meta.name, mode)
                    return
                except (SceneError, OSError, asyncio.TimeoutError):
                    pass
            await asyncio.sleep(0.1)
        await self._kill(meta, "did not start within 90 s")
        raise SceneError("Blender did not start within 90 s:\n" + _tail(scene_dir / "blender.log"), 500)

    async def _rpc(self, scene_id: str, req: dict, timeout: float) -> dict:
        sock = str(self._dir(scene_id) / "s.sock")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(sock, limit=1 << 26), 5)
        except (OSError, asyncio.TimeoutError):
            raise SceneError("the scene process is not responding") from None
        try:
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
        if not line:
            raise SceneError(self._kill_reason.pop(scene_id, None) or "the scene process died during the call", 500)
        return json.loads(line)

    async def _exec(self, meta: SceneMeta, req: dict, timeout: float) -> dict:
        """One request to a running scene; a timeout kills it (nothing can interrupt a bpy call)."""
        try:
            reply = await self._rpc(meta.id, req, timeout)
        except asyncio.TimeoutError:
            await self._kill(meta, f"timed out after {timeout:.0f} s")
            raise SceneError(
                f"the call ran longer than {timeout:.0f} s, so the scene process was stopped. The scene is back to its "
                "last checkpoint (the previous successful call) and the next call reopens it; python variables are gone",
                408) from None
        except SceneError:
            meta.state, meta.pid = "closed", None
            self._save(meta)
            raise
        meta.last_activity = _now()
        rss = _rss_mb(meta.pid) if meta.pid else None
        if rss:
            meta.peak_rss_mb = max(meta.peak_rss_mb, rss)
        self._save(meta)
        return reply

    async def _kill(self, meta: SceneMeta, reason: str) -> None:
        logger.warning("blender scene %s (%s) killed: %s", meta.id, meta.name, reason)
        self._kill_reason[meta.id] = f"the scene was stopped: {reason}. It reopens from its last checkpoint on the next call"
        proc = self._procs.pop(meta.id, None)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        elif meta.pid and _pid_is_scene(meta.pid, self._dir(meta.id)):
            try:
                os.kill(meta.pid, 9)
            except OSError:
                pass
        meta.state, meta.pid = "closed", None
        meta.error = reason
        self._save(meta)

    async def _stop(self, meta: SceneMeta) -> None:
        """Graceful close: ask the scene to quit, kill it if it won't."""
        if self._live(meta):
            try:
                await self._rpc(meta.id, {"op": "destroy"}, 10)
            except (SceneError, asyncio.TimeoutError, OSError):
                pass
            proc = self._procs.pop(meta.id, None)
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            elif meta.pid and _pid_is_scene(meta.pid, self._dir(meta.id)):
                try:
                    os.kill(meta.pid, 9)
                except OSError:
                    pass
        meta.state, meta.pid = "closed", None
        self._save(meta)

    async def _ensure_running(self, meta: SceneMeta) -> None:
        if self._live(meta):
            return
        if not (self._dir(meta.id) / "scene.blend").exists():
            raise SceneError("this scene has no checkpoint to reopen (it never finished starting); destroy it", 409)
        async with self._create_lock:
            self._check_capacity()
        await self._start(meta, "resume")

    # ----------------------------------------------------------------- work --
    async def run(self, scene_id: str, code: str, timeout_s: float = 120) -> dict:
        if not code or not code.strip():
            raise SceneError("code is empty")
        timeout = min(max(float(timeout_s or 120), 1.0), 900.0)
        async with self._lock(scene_id):
            meta = self._load(scene_id)
            await self._ensure_running(meta)
            reply = await self._exec(meta, {"op": "run", "code": code, "save": True}, timeout)
            meta.runs += 1
            note = await self._maybe_recycle(meta)
            self._save(meta)
        result = {"ok": reply["ok"], "output": reply["out"], "ms": reply["ms"]}
        if note:
            result["recycled"] = note
        return result

    async def _maybe_recycle(self, meta: SceneMeta) -> str | None:
        soft = get_settings().blender_scene_soft_mb
        rss = _rss_mb(meta.pid) if meta.pid else None
        if not rss or rss < soft:
            return None
        logger.info("blender scene %s (%s) restarted from its checkpoint at %d MB", meta.id, meta.name, rss)
        await self._stop(meta)
        await self._start(meta, "resume")
        return (f"the scene process had grown to {rss} MB and was restarted from its checkpoint to free memory: the scene "
                "is intact, but python variables from earlier calls are gone")

    async def call(self, scene_id: str, fn: str, **kwargs) -> dict:
        async with self._lock(scene_id):
            meta = self._load(scene_id)
            await self._ensure_running(meta)
            reply = await self._exec(meta, {"op": "call", "fn": fn, "kwargs": kwargs}, 120)
        if not reply["ok"]:
            raise SceneError(reply["out"][-1200:])
        return reply["result"]

    async def info(self, scene_id: str) -> dict:
        data = await self.call(scene_id, "info")
        return {"scene": await self.summary(scene_id), "blender": data}

    async def pose(self, scene_id: str, focus: str | None = None, frames: list[int] | None = None,
                   contacts: bool = True) -> dict:
        """pose_report() inside the scene, for one frame or several."""
        if frames and len(frames) > 48:
            raise SceneError("at most 48 frames per call")
        if not frames:
            return await self.call(scene_id, "pose_report", focus=focus, contacts=contacts)
        return {"frames": [await self.call(scene_id, "pose_report", focus=focus, frame=int(f), contacts=contacts)
                           for f in frames]}

    async def render(self, scene_id: str, views: list[str] | None = None, frames: list[int] | None = None,
                     engine: str = "workbench", size: int = 512, focus: str | None = None) -> dict:
        views = views or ["front"]
        if engine not in ("workbench", "eevee", "cycles"):
            raise SceneError("engine must be workbench, eevee or cycles")
        size = min(max(int(size), 64), 2048)
        if len(views) * len(frames or [None]) > 24:
            raise SceneError("at most 24 pictures per call")
        started = time.perf_counter()
        used = engine
        async with self._lock(scene_id):
            meta = self._load(scene_id)
            await self._ensure_running(meta)
            tiles: list[tuple[str, Path]] = []
            for frame in frames or [None]:
                for view in views:
                    out = self._dir(scene_id) / "renders" / f"tile{len(tiles)}.png"
                    used = await self._render_one(meta, out, view, frame, engine, size, used, focus)
                    label = (f"{focus} " if focus else "") + (view if isinstance(view, str) else "custom")
                    tiles.append((label + (f" f{frame}" if frame is not None else ""), out))
            sheet = await asyncio.to_thread(self._compose, tiles, len(views), self._dir(scene_id) / "renders" / "latest.png")
            for _, tile in tiles:
                tile.unlink(missing_ok=True)
            meta.renders += 1
            await asyncio.to_thread(self._thumbnail, meta)
            self._save(meta)
        return {"width": sheet[0], "height": sheet[1], "views": views, "frames": frames, "focus": focus, "engine": used,
                "ms": round((time.perf_counter() - started) * 1000)}

    async def _render_one(self, meta: SceneMeta, out: Path, view, frame, engine: str, size: int, used: str,
                          focus: str | None = None) -> str:
        def code(eng: str) -> str:
            return (f"render_to({str(out)!r}, view={view!r}, engine={eng!r}, size={size}, frame={frame!r}, focus={focus!r}"
                    + (", samples=16" if eng == "cycles" else "") + ")")

        reply = await self._exec(meta, {"op": "run", "code": code(engine)}, 600)
        if not reply["ok"] and engine != "cycles" and _GPU_ERROR.search(reply["out"]):
            # No usable GPU for this user: Cycles is CPU-only and always works.
            reply = await self._exec(meta, {"op": "run", "code": code("cycles")}, 600)
            used = "cycles"
        if not reply["ok"]:
            raise SceneError("render failed:\n" + reply["out"][-1200:])
        return used

    @staticmethod
    def _compose(tiles: list[tuple[str, Path]], cols: int, dest: Path) -> tuple[int, int]:
        images = [Image.open(p).convert("RGB") for _, p in tiles]
        w, h = images[0].size
        cols = max(1, min(cols, len(images)))
        rows = -(-len(images) // cols)
        sheet = Image.new("RGB", (w * cols, h * rows), (24, 24, 26))
        draw = ImageDraw.Draw(sheet)
        for i, ((label, _), img) in enumerate(zip(tiles, images)):
            x, y = (i % cols) * w, (i // cols) * h
            sheet.paste(img, (x, y))
            if len(images) > 1:
                draw.text((x + 6, y + 4), label, fill=(255, 220, 120))
        sheet.save(dest)
        return sheet.size

    def _thumbnail(self, meta: SceneMeta) -> None:
        src = self._dir(meta.id) / "renders" / "latest.png"
        if src.exists():
            with Image.open(src) as img:
                img.thumbnail((256, 256))
                img.convert("RGB").save(self._dir(meta.id) / "preview.png")

    async def _render_preview(self, meta: SceneMeta) -> None:
        """First picture of a new scene, so the list has something to show."""
        out = self._dir(meta.id) / "renders" / "tile0.png"
        await self._render_one(meta, out, "three_quarter", None, "workbench", 384, "workbench")
        await asyncio.to_thread(self._compose, [("", out)], 1, self._dir(meta.id) / "renders" / "latest.png")
        out.unlink(missing_ok=True)
        await asyncio.to_thread(self._thumbnail, meta)

    def render_path(self, scene_id: str, thumb: bool = False) -> Path:
        self._load(scene_id)
        path = self._dir(scene_id) / ("preview.png" if thumb else "renders/latest.png")
        if not path.exists():
            raise SceneError("this scene has no render yet", 404)
        return path

    async def export(self, scene_id: str, fmt: str = "glb") -> tuple[bytes, str, str]:
        """(bytes, mime, extension) of everything in the scene."""
        fmt = (fmt or "glb").lower()
        if fmt not in EXPORT_FORMATS:
            raise SceneError("format must be glb, fbx or obj")
        async with self._lock(scene_id):
            meta = self._load(scene_id)
            await self._ensure_running(meta)
            out_dir = self._dir(scene_id) / "export"
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir()
            try:
                path = out_dir / f"scene.{fmt}"
                reply = await self._exec(meta, {"op": "call", "fn": "export_to", "kwargs": {"path": str(path), "fmt": fmt}}, 600)
                if not reply["ok"]:
                    raise SceneError("export failed:\n" + reply["out"][-1200:])
                data = await asyncio.to_thread(path.read_bytes)
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)
        return data, EXPORT_FORMATS[fmt], fmt

    # ------------------------------------------------------------ lifecycle --
    async def close(self, scene_id: str) -> dict:
        async with self._lock(scene_id):
            meta = self._load(scene_id)
            await self._stop(meta)
        return await self.summary(scene_id)

    async def destroy(self, scene_id: str) -> None:
        self._load(scene_id)
        async with self._lock(scene_id):
            await self._destroy_files(scene_id)

    async def _destroy_files(self, scene_id: str) -> None:
        try:
            meta = self._load(scene_id)
            await self._stop(meta)
        except SceneError:
            pass
        shutil.rmtree(self._dir(scene_id), ignore_errors=True)
        self._locks.pop(scene_id, None)
        self._procs.pop(scene_id, None)

    def _disk_bytes(self, scene_dir: Path) -> int:
        total = 0
        for base, _dirs, files in os.walk(scene_dir):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    pass
        return total

    async def summary(self, scene_id: str) -> dict:
        meta = self._load(scene_id)
        scene_dir = self._dir(scene_id)
        live = self._live(meta)
        state = meta.state if live else "closed"
        return {
            "id": meta.id, "name": meta.name, "state": state, "owner": meta.owner, "created_at": meta.created_at,
            "last_activity": meta.last_activity, "inputs": meta.inputs, "runs": meta.runs, "renders": meta.renders,
            "rss_mb": (_rss_mb(meta.pid) if live and meta.pid else None), "peak_rss_mb": meta.peak_rss_mb,
            "disk_bytes": await asyncio.to_thread(self._disk_bytes, scene_dir), "error": meta.error,
            "has_preview": (scene_dir / "preview.png").exists(),
            "has_checkpoint": (scene_dir / "scene.blend").exists(),
        }

    async def list_scenes(self) -> dict:
        scenes = []
        for path in sorted(self.root.iterdir()):
            try:
                scenes.append(await self.summary(path.name))
            except SceneError:
                continue
        scenes.sort(key=lambda s: s["last_activity"], reverse=True)
        settings = get_settings()
        return {"scenes": scenes, "live": sum(1 for s in scenes if s["state"] != "closed"),
                "max_live": settings.blender_max_scenes, "disk_bytes": sum(s["disk_bytes"] for s in scenes),
                "mem_available_mb": _mem_available_mb()}

    # ---------------------------------------------------- background upkeep --
    async def recover(self) -> None:
        """At startup: a scene whose process is gone is simply closed."""
        for path in self.root.iterdir():
            try:
                meta = self._load(path.name)
            except SceneError:
                continue
            if meta.state != "closed" and not self._live(meta):
                meta.state, meta.pid = "closed", None
                self._save(meta)

    async def sweep(self) -> None:
        settings = get_settings()
        now = _now()
        live = []
        for path in self.root.iterdir():
            try:
                meta = self._load(path.name)
            except SceneError:
                continue
            if self._live(meta):
                live.append(meta)
        for meta in live:
            rss = _rss_mb(meta.pid) or 0
            if rss > settings.blender_scene_hard_mb:
                await self._kill(meta, f"it used {rss} MB, over the {settings.blender_scene_hard_mb} MB limit")
                continue
            lock = self._lock(meta.id)
            if not lock.locked() and now - meta.last_activity > settings.blender_scene_idle_s:
                async with lock:
                    fresh = self._load(meta.id)
                    if self._live(fresh) and _now() - fresh.last_activity > settings.blender_scene_idle_s:
                        logger.info("blender scene %s (%s) closed after %d s idle; checkpoint kept", fresh.id, fresh.name,
                                    settings.blender_scene_idle_s)
                        await self._stop(fresh)
        if live and _mem_available_mb() < settings.blender_emergency_free_mb:
            biggest = max(live, key=lambda m: _rss_mb(m.pid) or 0)
            await self._kill(biggest, "the server is nearly out of memory and this was the largest scene")
        if now - self._last_ttl_sweep > 600:
            self._last_ttl_sweep = now
            await self._ttl_sweep()

    async def _ttl_sweep(self) -> None:
        cutoff = _now() - get_settings().blender_closed_keep_days * 86400
        for path in list(self.root.iterdir()):
            try:
                meta = self._load(path.name)
            except SceneError:
                # A directory with no readable meta is a creation that never finished.
                if path.is_dir() and time.time() - path.stat().st_mtime > 3600:
                    shutil.rmtree(path, ignore_errors=True)
                continue
            if not self._live(meta) and meta.last_activity < cutoff:
                logger.info("blender scene %s (%s) deleted: closed for over %d days", meta.id, meta.name,
                            get_settings().blender_closed_keep_days)
                shutil.rmtree(path, ignore_errors=True)
                self._locks.pop(meta.id, None)

    async def shutdown(self) -> None:
        for path in list(self.root.iterdir()):
            try:
                meta = self._load(path.name)
            except SceneError:
                continue
            if self._live(meta):
                await self._stop(meta)


scene_manager = SceneManager()


async def scene_sweeper_loop() -> None:
    while True:
        await asyncio.sleep(2)
        try:
            await scene_manager.sweep()
        except asyncio.CancelledError:
            raise
        except Exception:  # upkeep must never die on one bad scene
            import logging

            logging.getLogger(__name__).exception("scene sweep failed")
