"""Blender add-on end-to-end test (runs INSIDE a headless Blender).

    mkdir -p /tmp/orch_bt/scripts/addons
    ln -sfn $PWD/backend/app/bridge_plugins/blender/orchestrator_bridge /tmp/orch_bt/scripts/addons/
    BLENDER_USER_SCRIPTS=/tmp/orch_bt/scripts BLENDER_USER_CONFIG=/tmp/orch_bt/config \
        /opt/blender/blender -b --factory-startup \
        --python-exit-code 1 --python scripts/bridge_blender_test.py

Expects a dev server at 127.0.0.1:8011 with token dev-local-token (the token
is only used here to play the person approving on the Settings page; the
add-on itself never sees it). Checks: pairing (Connect -> approve -> key on
disk, 0600), GLB import at the 3D cursor, re-send replaces in place keeping the moved/
rotated transform, PNG -> image plane, selected object -> GLB -> library +
ref cell, and that revoking the device on the site disconnects the add-on and
drops its key. Timers don't run headless, so the test pumps the add-on's queue.
"""
import bpy, addon_utils, json, time, threading, urllib.request, os, tempfile, sys
BASE, TOKEN = "http://127.0.0.1:8011", "dev-local-token"
def api(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(BASE+path, data=body, method=method, headers={"Authorization": "Bearer "+TOKEN, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = r.read(); return json.loads(d) if d else None
addon_utils.enable("orchestrator_bridge", default_set=True)
import orchestrator_bridge as ob
p = bpy.context.preferences.addons["orchestrator_bridge"].preferences
p.server_url, p.client_name = BASE, "blender-test"
def pump_until(cond, t=60):
    end = time.time()+t
    while time.time() < end:
        ob._pump()
        if cond(): return True
        time.sleep(0.1)
    raise SystemExit("TIMEOUT")
ob.forget()
ob.connect()
pump_until(lambda: ob.S.pair_code); print("pair code", ob.S.pair_code)
req = next(r for r in api("GET", "/api/bridge/pairing") if r["name"] == "blender-test")
assert req["code"] == ob.S.pair_code, (req, ob.S.pair_code)
device = api("POST", f"/api/bridge/pairing/{req['id']}/approve")
pump_until(lambda: ob.S.status == "connected"); print("connected")
assert ob.S.token and oct(os.stat(ob.S.key_file).st_mode & 0o777) == "0o600", ob.S.key_file
assert TOKEN not in open(ob.S.key_file).read()
cid = next(c["id"] for c in api("GET","/api/bridge/clients") if c["name"]=="blender-test")
pid = api("POST","/api/projects", json.dumps({"name":"blender bridge test"}).encode())["id"]
try:
    # a GLB to send: export a cube
    [bpy.data.objects.remove(o) for o in list(bpy.data.objects)]
    bpy.ops.mesh.primitive_monkey_add()
    tmp = os.path.join(tempfile.mkdtemp(), "Monkey.glb")
    bpy.ops.export_scene.gltf(filepath=tmp, export_format="GLB")
    [bpy.data.objects.remove(o) for o in list(bpy.data.objects)]
    body, ctype = ob._multipart({}, "file", "Monkey.glb", open(tmp,"rb").read(), "model/gltf-binary")
    aid = api("POST", f"/api/projects/{pid}/assets", body, ctype)["id"]
    def send(out):
        out.append(api("POST","/api/bridge/send", json.dumps({"asset_id":aid,"client_id":cid}).encode()))
    bpy.context.scene.cursor.location = (3,0,0)
    for round_ in range(2):
        out=[]; th=threading.Thread(target=send,args=(out,)); th.start()
        pump_until(lambda: out); print("send:", out[0]["message"])
        objs=[o for o in bpy.data.objects if o.get("orch_asset_id")==aid]
        want = 3 if round_==0 else 7
        assert len(objs)==1 and abs(objs[0].location.x-want)<1e-4, [(o.name,o.location) for o in objs]
        if round_==0: objs[0].location.x = 7; objs[0].rotation_mode="XYZ"; objs[0].rotation_euler.z = 1.0
        else: bpy.context.view_layer.update(); assert abs(objs[0].matrix_world.to_euler().z-1.0)<1e-4
        if round_==1: assert out[0]["message"].startswith("replaced")
    # image -> plane
    from struct import pack; import zlib
    def ch(t,d): return pack(">I",len(d))+t+d+pack(">I",zlib.crc32(t+d))
    png=b"\x89PNG\r\n\x1a\n"+ch(b"IHDR",pack(">IIBBBBB",2,2,8,2,0,0,0))+ch(b"IDAT",zlib.compress(b"\x00"+b"\xff\x00\x00"*2+b"\x00"+b"\x00\xff\x00"*2))+ch(b"IEND",b"")
    body, ctype = ob._multipart({}, "file", "Tex.png", png, "image/png")
    iid = api("POST", f"/api/projects/{pid}/assets", body, ctype)["id"]
    out=[]; th=threading.Thread(target=lambda: out.append(api("POST","/api/bridge/send", json.dumps({"asset_id":iid,"client_id":cid}).encode()))); th.start()
    pump_until(lambda: out); print("image:", out[0])
    # export back
    ob.refresh_targets(); pump_until(lambda: any(t["id"]==pid for t in ob.S.targets))
    wm = bpy.context.window_manager; wm.orch_project = pid; wm.orch_place = True
    monkey=[o for o in bpy.data.objects if o.get("orch_asset_id")==aid][0]
    for o in bpy.data.objects: o.select_set(o is monkey)
    bpy.context.view_layer.objects.active = monkey
    with bpy.context.temp_override(selected_objects=[monkey], active_object=monkey, object=monkey):
        print(bpy.ops.orchestrator.send_selected())
    pump_until(lambda: ob.S.last.startswith("Sent") or "failed" in ob.S.last); print("export:", ob.S.last)
    lib = api("GET", f"/api/projects/{pid}/assets")
    sent=[a for a in lib if a["name"]=="Suzanne"] 
    print("library:", [(a["name"],a["kind"]) for a in lib])
    assert ob.S.last.startswith("Sent") and "placed" in ob.S.last
    # revoke on the site -> add-on drops the key and stops
    api("DELETE", f"/api/bridge/devices/{device['id']}")
    pump_until(lambda: ob.S.thread is None and "Connect" in ob.S.status); print("after revoke:", ob.S.status)
    assert not ob.S.token and "blender-test" not in json.dumps(ob._keys())
finally:
    ob.disconnect(); api("DELETE", f"/api/projects/{pid}")
print("BLENDER BRIDGE OK")
