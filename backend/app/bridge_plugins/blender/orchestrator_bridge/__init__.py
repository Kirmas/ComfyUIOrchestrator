"""ComfyUI Orchestrator bridge for Blender.

Two-way:
- the orchestrator's "Send to Blender" lands an asset in the scene that is
  open right now (at the 3D cursor; sending the same asset again replaces it
  in place);
- "Send to Orchestrator" (sidebar > Orchestrator, or the object's right-click
  menu) exports the selected objects to GLB -- or, from the Image Editor, the
  open image to PNG -- into a project's library, and optionally as a cell on a
  new row of a chosen graph.

The add-on dials out to the server over a WebSocket, so nothing needs to be
opened on this PC. It never holds the site's API token: "Connect" asks the
server to pair, the person approves the request on the site's Settings page
(the same code shows in both places), and this add-on gets its own device key,
valid for a month, kept in Blender's config dir (not in the .blend, not in the
preferences). Server-side counterpart: backend/app/api/routes/bridge.py.
"""

bl_info = {
    "name": "ComfyUI Orchestrator Bridge",
    "author": "ComfyUI Orchestrator",
    "version": (1, 0, 6),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Orchestrator; Image Editor > Image menu",
    "description": "Send assets between ComfyUI Orchestrator and Blender",
    "category": "Import-Export",
}

import json
import os
import platform
import queue
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .wsclient import WSClient, WSClosed

VERSION = "1.0.6"
PROP = "orch_asset_id"


# ---------------------------------------------------------------- state ----
class _State:
    def __init__(self):
        self.thread = None
        self.stop = threading.Event()
        self.ws = None
        self.status = "disconnected"
        self.main = queue.Queue()  # callables to run on Blender's main thread
        self.targets = []  # [{"id","name","graphs":[{"id","name"}]}]
        self.last = ""
        self.server = ""
        self.token = ""  # this device's key (never the site's API token)
        self.key_file = ""
        self.key_expires = ""
        self.pair_code = ""


S = _State()


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _redraw():
    wm = bpy.context.window_manager
    for win in getattr(wm, "windows", []):
        for area in win.screen.areas:
            area.tag_redraw()


def _set_status(text):
    S.status = text
    S.main.put(_redraw)


def _say(text, error=False):
    """Status line in the panel, plus a popup when there's a window to show it in."""
    def show():
        S.last = text
        _redraw()
        wm = bpy.context.window_manager
        # A timer runs with no window in its context, and popup_menu then
        # crashes Blender outright -- give it one explicitly (none headless).
        if not bpy.app.background and len(wm.windows):
            def draw(menu, _ctx):
                menu.layout.label(text=text)
            with bpy.context.temp_override(window=wm.windows[0]):
                wm.popup_menu(draw, title="Orchestrator", icon="ERROR" if error else "INFO")
        print(f"[orchestrator] {text}")
    S.main.put(show)


# ------------------------------------------------------------------ http ----
def _request(method, path, body=None, headers=None, timeout=120):
    req = urllib.request.Request(S.server.rstrip("/") + path, data=body, method=method)
    if S.token:
        req.add_header("Authorization", f"Bearer {S.token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise RuntimeError(f"{exc.code}: {detail}") from None


def _multipart(fields, file_field, filename, data, mime):
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f"Content-Type: {mime}\r\n\r\n".encode())
    out.append(data)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------- device key ----
def _keys():
    try:
        with open(S.key_file, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _server_id():
    return S.server.rstrip("/").lower()


def _load_key():
    entry = _keys().get(_server_id()) or {}
    S.token, S.key_expires = entry.get("key", ""), entry.get("expires_at", "")


def _save_key(key, expires_at):
    keys = _keys()
    if key:
        keys[_server_id()] = {"key": key, "expires_at": expires_at}
    else:
        keys.pop(_server_id(), None)
    os.makedirs(os.path.dirname(S.key_file), exist_ok=True)
    fd = os.open(S.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(keys, f)
    S.token, S.key_expires = key, expires_at


# ------------------------------------------------------------ connection ----
def _ws_url():
    base = S.server.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[8:] + "/api/bridge/device/ws"
    return "ws://" + base.split("://", 1)[-1] + "/api/bridge/device/ws"


def _pair(name, stop, status):
    """Ask the server to pair and wait for the person to approve it on the
    site. True once a key is stored."""
    status("asking the orchestrator to pair...")
    ask = json.loads(_request("POST", "/api/bridge/device/pair",
                              json.dumps({"app": "blender", "name": name}).encode(),
                              {"Content-Type": "application/json"}, timeout=30))
    S.pair_code = ask["code"]
    status(f"code {ask['code']} -- approve it on the site: Settings > Unreal / Blender bridge")
    poll = json.dumps({"secret": ask["secret"]}).encode()
    while not stop.wait(2):
        res = json.loads(_request("POST", f"/api/bridge/device/pair/{ask['request_id']}/poll", poll,
                                  {"Content-Type": "application/json"}, timeout=30))
        if res["status"] == "approved":
            S.pair_code = ""
            _save_key(res["key"], res.get("expires_at") or "")
            return True
        if res["status"] == "denied":
            status("pairing was denied on the site")
            return False
        if res["status"] == "expired":
            status("pairing request expired -- press Connect again")
            return False
    return False


def _loop(name, stop):
    """One connection thread. `stop` is its own event: a reconnect starts a new
    thread with a new event, and this one must then touch nothing shared."""
    def status(text):
        if stop is S.stop:
            _set_status(text)

    def finish():
        if stop is S.stop:
            S.thread = None
            S.main.put(_redraw)

    if not S.token:
        try:
            if not _pair(name, stop, status):
                return finish()
        except Exception as exc:  # noqa: BLE001
            status(f"pairing failed: {exc}")
            return finish()

    while not stop.is_set():
        ws = None
        try:
            status("connecting...")
            ws = WSClient(_ws_url(), {"Authorization": f"Bearer {S.token}"})
            ws.send_text(json.dumps({"type": "hello", "version": f"{VERSION} / Blender {bpy.app.version_string}"}))
            welcome = json.loads(ws.recv_text())
            if welcome.get("type") != "welcome":
                raise WSClosed(f"unexpected reply {welcome}")
            if stop.is_set():
                break
            S.ws = ws
            status("connected")
            S.main.put(lambda: refresh_targets(quiet=True))
            while True:
                msg = json.loads(ws.recv_text())
                if msg.get("type") == "import":
                    threading.Thread(target=_download, args=(msg,), daemon=True).start()
        except WSClosed as exc:
            if stop.is_set():
                break
            if exc.code == 4401:
                # expired or revoked: retrying can't help, the person has to approve again
                _save_key("", "")
                status(f"{exc.reason or 'device key no longer valid'} (press Connect)")
                return finish()
            status(f"offline: {exc}")
        except Exception as exc:  # noqa: BLE001 -- any other failure means "retry later"
            if stop.is_set():
                break
            status(f"offline: {exc}")
        finally:
            if ws is not None:
                if S.ws is ws:
                    S.ws = None
                ws.close()
        stop.wait(5)


def connect():
    disconnect()
    p = _prefs()
    S.server = p.server_url.strip()
    S.key_file = os.path.join(bpy.utils.user_resource("CONFIG", path="orchestrator_bridge"), "devices.json")
    if not S.server:
        _set_status("set the server URL in the add-on preferences")
        return
    _load_key()
    S.stop = threading.Event()
    S.thread = threading.Thread(target=_loop, args=(p.client_name.strip() or platform.node(), S.stop), daemon=True)
    S.thread.start()


def disconnect():
    if S.thread is not None:
        S.stop.set()
        ws, S.ws = S.ws, None
        if ws is not None:
            ws.close()
        S.thread = None
    S.status = "disconnected"


def forget():
    """Drop this PC's key for the current server (the site keeps listing the
    device until it expires or is revoked there)."""
    disconnect()
    if not S.key_file:
        S.key_file = os.path.join(bpy.utils.user_resource("CONFIG", path="orchestrator_bridge"), "devices.json")
    S.server = _prefs().server_url.strip()
    _save_key("", "")


def _reply(request_id, ok, message):
    ws = S.ws
    if ws is None:
        return
    try:
        ws.send_text(json.dumps({"type": "result", "request_id": request_id, "ok": ok, "message": message}))
    except OSError:
        pass


# --------------------------------------------------------------- import ----
def _download(msg):
    asset = msg["asset"]
    try:
        data = _request("GET", asset["url"], timeout=600)
        fd, tmp = tempfile.mkstemp(suffix="." + (asset.get("format") or "bin"))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except Exception as exc:  # noqa: BLE001
        _reply(msg["request_id"], False, f"download failed: {exc}")
        _say(f"Download of {asset.get('filename')} failed: {exc}", error=True)
        return
    S.main.put(lambda: _import(msg, tmp))


def _store_dir(project):
    """Imported files stay on disk (an Alembic cache streams from its file):
    next to the .blend if it's saved, else in Blender's user data dir."""
    safe = "".join(c if c.isalnum() or c in " -_." else "_" for c in (project or "misc")).strip() or "misc"
    if bpy.data.filepath:
        base = os.path.join(os.path.dirname(bpy.data.filepath), "orchestrator")
    else:
        base = bpy.utils.user_resource("DATAFILES", path="orchestrator_bridge", create=True)
    path = os.path.join(base, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _view3d_override():
    wm = bpy.context.window_manager
    for win in getattr(wm, "windows", []):
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": win, "area": area, "region": region}
    return {}


def _run_importer(fmt, kind, path):
    if fmt in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif fmt == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif fmt == "abc":
        bpy.ops.wm.alembic_import(filepath=path)
    elif fmt == "obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif fmt == "stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif kind == "image":
        if hasattr(bpy.ops.image, "import_as_mesh_planes"):
            bpy.ops.image.import_as_mesh_planes(files=[{"name": os.path.basename(path)}],
                                                directory=os.path.dirname(path))
        else:
            bpy.data.images.load(path)
    else:
        raise RuntimeError(f"Blender can't import a {kind} ({fmt or 'unknown format'}) asset")


def _import(msg, tmp):
    asset = msg["asset"]
    aid, fmt, kind = asset["id"], (asset.get("format") or "").lower(), asset.get("kind")
    try:
        final = os.path.join(_store_dir(asset.get("project_name")), asset.get("filename") or os.path.basename(tmp))
        shutil.move(tmp, final)

        old = [o for o in bpy.data.objects if o.get(PROP) == aid]
        old_roots = [o for o in old if o.parent not in old]
        # matrix_world is stale until the depsgraph re-evaluates (a location
        # typed a moment ago would read as the old one).
        bpy.context.view_layer.update()
        old_matrix = old_roots[0].matrix_world.copy() if len(old_roots) == 1 else None
        anchor = old_roots[0].matrix_world.translation.copy() if old_roots else bpy.context.scene.cursor.location.copy()
        if bpy.context.object is not None and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for o in old:
            bpy.data.objects.remove(o, do_unlink=True)

        before = set(bpy.data.objects)
        with bpy.context.temp_override(**_view3d_override()):
            _run_importer(fmt, kind, final)
        new = [o for o in bpy.data.objects if o not in before]
        for o in new:
            o[PROP] = aid
        roots = [o for o in new if o.parent not in new]
        if old_matrix is not None and len(roots) == 1:
            roots[0].matrix_world = old_matrix  # replaced in place: keep where it was put
        else:
            for root in roots:
                root.location = root.location + anchor
        what = f"{len(new)} object(s)" if new else "image"
        verb = "replaced" if old else "imported"
        _reply(msg["request_id"], True, f"{verb} {asset.get('filename')} into '{bpy.context.scene.name}' ({what})")
        _say(f"{verb.capitalize()} {asset.get('filename')} from Orchestrator")
    except Exception as exc:  # noqa: BLE001
        _reply(msg["request_id"], False, str(exc))
        _say(f"Import of {asset.get('filename')} failed: {exc}", error=True)


# --------------------------------------------------------------- export ----
def refresh_targets(quiet=False):
    if not S.server:
        return

    def work():
        try:
            targets = json.loads(_request("GET", "/api/bridge/device/targets", timeout=30))
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                _say(f"Couldn't load projects: {exc}", error=True)
            return

        def apply():
            S.targets = targets
            _redraw()
        S.main.put(apply)
    threading.Thread(target=work, daemon=True).start()


_ENUM_CACHE = {}  # Blender requires enum item strings to stay referenced


def _project_items(_self, _ctx):
    items = [(t["id"], t["name"], "") for t in S.targets] or [("", "(no projects -- refresh)", "")]
    _ENUM_CACHE["p"] = items
    return items


def _graph_items(self, _ctx):
    proj = next((t for t in S.targets if t["id"] == self.orch_project), None)
    graphs = proj["graphs"] if proj else [{"id": None, "name": "Main"}]
    items = [(g["id"] or "MAIN", g["name"], "") for g in graphs]
    _ENUM_CACHE["g"] = items
    return items


# glTF has no "emission strength driven by a mask": the exporter writes the strength as a flat
# factor and drops whatever drove it, so a glowing dagger arrived as a solid white-hot blob
# (2026-11-22). Walking the shader graph by hand to find "the one image behind the strength" broke
# the moment the glow got a second input (a flicker/noise mix on top of the mask) -- the walk found
# two linked sockets where it expected one, and bailed out silently, with no warning. Baking the
# live Emission output through Cycles sidesteps that entirely: it does not need to understand the
# graph (mask, flicker, add, mix, anything), it just renders what the material actually produces,
# and that bake is what gets wired into Emission Color for the export.
GLOW_STRENGTH_CAP = 3.0  # a viewer clips; a huge factor turns the orange glow white
GLOW_BAKE_SIZE = 1024


def _bake_glow_for_export(objs, warnings):
    """Bakes each material's live Emission (colour x strength, however the shader arrives at it)
    into a flat image for the glTF export; returns the function that undoes it. Anything it could
    not do is appended to `warnings` (shown to the person)."""
    import numpy as np
    undo = []
    done = set()
    scene = bpy.context.scene
    engine_before = scene.render.engine
    samples_before = getattr(scene.cycles, "samples", None)
    sel_before = list(bpy.context.selected_objects)
    active_before = bpy.context.view_layer.objects.active

    for ob in objs:
        for slot in getattr(ob, "material_slots", []):
            mat = slot.material
            if mat is None or not mat.use_nodes or mat.name in done:
                continue
            done.add(mat.name)
            nt = mat.node_tree
            for bsdf in [n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]:
                strength, color = bsdf.inputs["Emission Strength"], bsdf.inputs["Emission Color"]
                if not strength.is_linked and not color.is_linked:
                    continue  # flat emission already exports fine as-is
                if not ob.data.uv_layers:
                    warnings.append(f"{mat.name}: '{ob.name}' has no UVs -- can't bake its glow for export")
                    continue

                img = bpy.data.images.new("orch_glow_" + mat.name, GLOW_BAKE_SIZE, GLOW_BAKE_SIZE,
                                          alpha=False, float_buffer=True)
                # Set BEFORE any read/write of .pixels, never again after: Blender silently
                # zeroes an image's buffer (alpha aside) the moment colorspace_settings.name
                # changes on an image that already has data -- verified directly (2026-11-22).
                # Missing this is why the first cut of this bake shipped a blank black texture:
                # the JSON looked right (an emissiveTexture slot was there), nothing glowed.
                # "sRGB" (not "Non-Color") because glTF emissive textures are sRGB-encoded by
                # spec -- Blender applies that encode on save only when the tag says so.
                img.colorspace_settings.name = "sRGB"
                node = nt.nodes.new("ShaderNodeTexImage")
                node.image = img
                for n in nt.nodes:
                    n.select = False
                node.select = True
                active_node_before = nt.nodes.active
                nt.nodes.active = node

                bpy.ops.object.select_all(action="DESELECT")
                ob.select_set(True)
                bpy.context.view_layer.objects.active = ob
                scene.render.engine = "CYCLES"
                scene.cycles.samples = 32
                try:
                    bpy.ops.object.bake(type="EMIT")
                except RuntimeError as exc:
                    warnings.append(f"{mat.name}: emission bake failed ({exc}) -- "
                                    f"the glow will export as a flat colour")
                    nt.nodes.remove(node)
                    bpy.data.images.remove(img)
                    nt.nodes.active = active_node_before
                    continue

                px = np.empty(GLOW_BAKE_SIZE * GLOW_BAKE_SIZE * 4, dtype=np.float32)
                img.pixels.foreach_get(px)
                px = px.reshape(-1, 4)
                # the 99.5th percentile, not the true max: at this sample count a noise-driven
                # glow (like the flicker on the quill) leaves a handful of firefly pixels that
                # would otherwise make emissiveStrength swing wildly between two sends of the
                # same, unchanged material.
                peak = float(np.percentile(px[:, :3], 99.5)) if px[:, :3].max() > 0 else 0.0
                out_strength = min(max(peak, 1.0), GLOW_STRENGTH_CAP)
                if peak > 0:
                    px[:, :3] = np.minimum(px[:, :3] / peak, 1.0)  # keep the baked shape, clip the firefly tail
                px[:, 3] = 1.0
                img.pixels.foreach_set(px.ravel())
                img.pack()

                old_color_value = tuple(color.default_value)
                old_color_links = [(l.from_socket, l.to_socket) for l in color.links]
                for l in list(color.links):
                    nt.links.remove(l)
                link = nt.links.new(node.outputs["Color"], color)

                old_strength_value = strength.default_value
                old_strength_links = [(l.from_socket, l.to_socket) for l in strength.links]
                for l in list(strength.links):
                    nt.links.remove(l)
                strength.default_value = out_strength

                def restore(nt=nt, node=node, img=img, link=link,
                            color=color, old_color_value=old_color_value, old_color_links=old_color_links,
                            strength=strength, old_strength_value=old_strength_value,
                            old_strength_links=old_strength_links, active_node=active_node_before):
                    nt.links.remove(link)
                    nt.nodes.remove(node)
                    bpy.data.images.remove(img)
                    color.default_value = old_color_value
                    for a, b in old_color_links:
                        nt.links.new(a, b)
                    strength.default_value = old_strength_value
                    for a, b in old_strength_links:
                        nt.links.new(a, b)
                    nt.nodes.active = active_node
                undo.append(restore)

    scene.render.engine = engine_before
    if samples_before is not None:
        scene.cycles.samples = samples_before
    bpy.ops.object.select_all(action="DESELECT")
    for o in sel_before:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active_before

    def undo_all():
        for fn in reversed(undo):
            fn()
    return undo_all


def _upload(path, filename, mime, wm_project, wm_graph, place, source_path):
    fields = {"project_id": wm_project, "dashboard_id": "" if wm_graph in ("", "MAIN") else wm_graph,
              "place": "true" if place else "false", "source_path": source_path}

    def work():
        try:
            with open(path, "rb") as f:
                data = f.read()
            body, ctype = _multipart(fields, "file", filename, data, mime)
            res = json.loads(_request("POST", "/api/bridge/device/upload", body, {"Content-Type": ctype}, timeout=600))
            where = " and placed on a new row" if res.get("placed") else ""
            _say(f"Sent {filename} to the project library{where}")
        except Exception as exc:  # noqa: BLE001
            _say(f"Sending {filename} failed: {exc}", error=True)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    threading.Thread(target=work, daemon=True).start()


def _check_target(op, wm):
    if S.status != "connected":
        op.report({"ERROR"}, f"Not connected to the orchestrator ({S.status})")
        return False
    if not wm.orch_project:
        op.report({"ERROR"}, "Pick a project first (Refresh if the list is empty)")
        return False
    return True


# Blender runs an operator on its one main thread, so a second click during the multi-second bake is
# not concurrent: the window is frozen, the click waits in the event queue and fires the moment the
# first send returns -- a second, identical send. A flag that is cleared on return can't see it, so
# an identical send (same objects) that starts within this window of the last one finishing is the
# queued click, not a deliberate re-send (that takes a human longer than this).
_SEND_DEBOUNCE_S = 1.5
_last_send = {"key": None, "done": 0.0}


class ORCH_OT_send_selected(bpy.types.Operator):
    bl_idname = "orchestrator.send_selected"
    bl_label = "Send to Orchestrator"
    bl_description = "Export the selected objects as GLB into the chosen project"

    @classmethod
    def poll(cls, ctx):
        return bool(ctx.selected_objects)

    def execute(self, ctx):
        wm = ctx.window_manager
        if not _check_target(self, wm):
            return {"CANCELLED"}
        if ctx.object is not None and ctx.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        objs = ctx.selected_objects
        key = tuple(sorted(o.name for o in objs))
        if key == _last_send["key"] and time.monotonic() - _last_send["done"] < _SEND_DEBOUNCE_S:
            return {"CANCELLED"}  # the click that queued up while the previous send was baking
        name = (ctx.active_object or objs[0]).name if len(objs) == 1 else (ctx.active_object or objs[0]).name + "_group"
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        tmpdir = tempfile.mkdtemp(prefix="orch_")
        path = os.path.join(tmpdir, safe + ".glb")
        # use_active_scene: the exporter's default is EVERY scene in the .blend, so a file with several
        # scenes sent the whole project's meshes (97 MB for one dagger) along with the selection.
        # cursor_set: a masked/driven glow gets baked through Cycles before export (see
        # _bake_glow_for_export), which blocks Blender's main thread for a few seconds -- the first
        # bake in a session is slower still (Cycles compiles its kernels). Without this the window
        # looks frozen and a second click fires a second, independent send.
        ctx.window.cursor_set("WAIT")
        try:
            warnings = []
            undo = _bake_glow_for_export(objs, warnings)
            for w in warnings:
                self.report({"WARNING"}, w)
            try:
                bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True, use_visible=True,
                                          use_active_scene=True)
            finally:
                undo()
        finally:
            ctx.window.cursor_set("DEFAULT")
        _upload(path, safe + ".glb", "model/gltf-binary", wm.orch_project, wm.orch_graph, wm.orch_place,
                f"{bpy.data.filepath or 'unsaved'}:{name}")
        self.report({"INFO"}, f"Sending {safe}.glb...")
        _last_send.update(key=key, done=time.monotonic())
        return {"FINISHED"}


class ORCH_OT_send_image(bpy.types.Operator):
    bl_idname = "orchestrator.send_image"
    bl_label = "Send Image to Orchestrator"
    bl_description = "Save the open image as PNG into the chosen project"

    @classmethod
    def poll(cls, ctx):
        return getattr(ctx.space_data, "image", None) is not None

    def execute(self, ctx):
        wm = ctx.window_manager
        if not _check_target(self, wm):
            return {"CANCELLED"}
        img = ctx.space_data.image
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in os.path.splitext(img.name)[0]) or "image"
        tmpdir = tempfile.mkdtemp(prefix="orch_")
        path = os.path.join(tmpdir, safe + ".png")
        if img.type == "RENDER_RESULT":
            img.save_render(path)
        else:
            copy = img.copy()
            try:
                copy.filepath_raw = path
                copy.file_format = "PNG"
                copy.save()
            finally:
                bpy.data.images.remove(copy)
        _upload(path, safe + ".png", "image/png", wm.orch_project, wm.orch_graph, wm.orch_place,
                f"{bpy.data.filepath or 'unsaved'}:{img.name}")
        self.report({"INFO"}, f"Sending {safe}.png...")
        return {"FINISHED"}


class ORCH_OT_connect(bpy.types.Operator):
    bl_idname = "orchestrator.connect"
    bl_label = "Connect"

    def execute(self, _ctx):
        connect()
        return {"FINISHED"}


class ORCH_OT_disconnect(bpy.types.Operator):
    bl_idname = "orchestrator.disconnect"
    bl_label = "Disconnect"

    def execute(self, _ctx):
        disconnect()
        _redraw()
        return {"FINISHED"}


class ORCH_OT_forget(bpy.types.Operator):
    bl_idname = "orchestrator.forget"
    bl_label = "Forget pairing"
    bl_description = "Delete this PC's device key; Connect will then ask for approval again"

    def execute(self, _ctx):
        forget()
        _redraw()
        return {"FINISHED"}


class ORCH_OT_refresh(bpy.types.Operator):
    bl_idname = "orchestrator.refresh"
    bl_label = "Refresh projects"

    def execute(self, _ctx):
        refresh_targets()
        return {"FINISHED"}


def _draw_target(layout, ctx):
    wm = ctx.window_manager
    row = layout.row(align=True)
    row.prop(wm, "orch_project", text="")
    row.operator("orchestrator.refresh", text="", icon="FILE_REFRESH")
    layout.prop(wm, "orch_graph", text="Graph")
    layout.prop(wm, "orch_place")


class ORCH_PT_panel(bpy.types.Panel):
    bl_label = "Orchestrator"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Orchestrator"

    def draw(self, ctx):
        col = self.layout.column()
        connected = S.status == "connected"
        if S.pair_code and S.thread is not None and not S.token:
            box = col.box()
            box.label(text="Approve on the site:", icon="KEY_HLT")
            box.label(text="Settings > Unreal / Blender bridge")
            box.label(text=f"code  {S.pair_code}")
        else:
            col.label(text=S.status, icon="LINKED" if connected else "UNLINKED")
        if S.thread is None:
            col.operator("orchestrator.connect")
        else:
            col.operator("orchestrator.disconnect")
        if S.key_expires:
            col.label(text=f"key valid until {S.key_expires[:10]}")
        col.separator()
        _draw_target(col, ctx)
        col.operator("orchestrator.send_selected", icon="EXPORT")
        if S.last:
            col.separator()
            col.label(text=S.last)


class ORCH_PT_image(bpy.types.Panel):
    bl_label = "Orchestrator"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Orchestrator"

    def draw(self, ctx):
        col = self.layout.column()
        col.label(text=S.status, icon="LINKED" if S.status == "connected" else "UNLINKED")
        _draw_target(col, ctx)
        col.operator("orchestrator.send_image", icon="EXPORT")


def _restart(_self, _ctx):
    if S.thread is not None:
        connect()


class ORCH_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    server_url: StringProperty(name="Server URL", default="http://192.168.0.3:8000", update=_restart)
    client_name: StringProperty(name="This PC's name", default=platform.node(), update=_restart)
    auto_connect: BoolProperty(name="Connect when Blender starts", default=True)

    def draw(self, _ctx):
        col = self.layout.column()
        col.prop(self, "server_url")
        col.prop(self, "client_name")
        col.prop(self, "auto_connect")
        row = col.row()
        row.operator("orchestrator.connect", text="Connect / pair")
        row.operator("orchestrator.forget")
        col.label(text="No token needed: Connect asks the site, you approve it there "
                       "(Settings > Unreal / Blender bridge). The key lasts a month.")


def _object_menu(self, _ctx):
    self.layout.separator()
    self.layout.operator("orchestrator.send_selected", icon="EXPORT")


def _image_menu(self, _ctx):
    self.layout.separator()
    self.layout.operator("orchestrator.send_image", icon="EXPORT")


def _pump():
    while True:
        try:
            fn = S.main.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[orchestrator] {exc}")
    return 0.25


def _autostart():
    """Reconnect by itself only with a key already in hand -- a new pairing
    request is only ever made by pressing Connect, never at every start-up."""
    try:
        p = _prefs()
    except KeyError:
        return None
    if p.auto_connect and p.server_url.strip():
        S.server = p.server_url.strip()
        S.key_file = os.path.join(bpy.utils.user_resource("CONFIG", path="orchestrator_bridge"), "devices.json")
        _load_key()
        if S.token:
            connect()
        else:
            _set_status("not paired -- press Connect")
    return None


CLASSES = (ORCH_OT_send_selected, ORCH_OT_send_image, ORCH_OT_connect, ORCH_OT_disconnect, ORCH_OT_forget,
           ORCH_OT_refresh,
           ORCH_PT_panel, ORCH_PT_image, ORCH_Preferences)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    wm = bpy.types.WindowManager
    wm.orch_project = EnumProperty(name="Project", items=_project_items)
    wm.orch_graph = EnumProperty(name="Graph", items=_graph_items)
    wm.orch_place = BoolProperty(name="Also place as a cell", default=True,
                                 description="Besides the project library, put it on a new row of the graph")
    bpy.types.VIEW3D_MT_object_context_menu.append(_object_menu)
    bpy.types.IMAGE_MT_image.append(_image_menu)
    bpy.app.timers.register(_pump, persistent=True)
    bpy.app.timers.register(_autostart, first_interval=1.0)


def unregister():
    disconnect()
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    bpy.types.VIEW3D_MT_object_context_menu.remove(_object_menu)
    bpy.types.IMAGE_MT_image.remove(_image_menu)
    wm = bpy.types.WindowManager
    del wm.orch_project, wm.orch_graph, wm.orch_place
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
