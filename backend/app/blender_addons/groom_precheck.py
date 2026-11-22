# Groom Exporter -- pre-export check (from GroomExporter_Guide.md section 9; the
# checks themselves are the guide's own groom_precheck.py, unchanged).
# Standalone: select the Curves objects and run in the Text Editor, or
#   blender -b file.blend --python groom_precheck.py -- Obj1 Obj2
# As a module (the scene server's groom_check/export_to): report(objects) -> list of
# {"object", "ok", "info": [...], "issues": [...]}. Changes nothing in the scene.
import bpy, sys
import numpy as np

DEFAULTS = {
    "att_groom_root_uv": "surface_uv_coordinate",
    "att_groom_guide": "groom_is_guide",
    "att_groom_id": "groom_guide_id",
    "att_groom_closest_guides": "groom_guide_closest",
    "att_groom_guide_weights": "groom_guide_weights",
    "att_groom_width": "radius",
    "att_groom_color": "color",
}

def mapping(ob):
    gp = getattr(ob, "GroomProperty", None)
    return {k: (getattr(gp, k, "") if gp else "") or v for k, v in DEFAULTS.items()}

def read(attr, field, width):
    n = len(attr.data)
    buf = np.empty(n * width, dtype=np.float64 if field != "value" or attr.data_type.startswith("FLOAT") else np.int64)
    attr.data.foreach_get(field, buf)
    return buf.reshape(n, width) if width > 1 else buf

def check(ob, dg):
    issues, info = [], []
    if any(abs(v) > 1e-6 for v in ob.rotation_euler) or any(abs(v - 1) > 1e-6 for v in ob.scale):
        issues.append(f"rotation/scale не нульові {tuple(ob.rotation_euler)} {tuple(ob.scale)} — експортер їх ігнорує")
    if not ob.data.surface:
        issues.append("не задано Data > Surface")
    elif not ob.data.surface_uv_map:
        issues.append("не задано Data > Surface UV Map")
    if ob.parent is None:
        issues.append("Curves-об'єкт без parent (має бути припаркований до скальпа/голови)")

    c = ob.evaluated_get(dg).data
    A, m = c.attributes, mapping(ob)
    off = np.empty(len(c.curves) + 1, dtype=np.int64)
    c.curve_offset_data.foreach_get("value", off)
    sizes = np.diff(off)
    info.append(f"кривих {len(sizes)}, точок {int(sizes.sum())}, точок/криву {sizes.min()}..{sizes.max()}")
    if (sizes < 2).any():
        issues.append(f"{int((sizes < 2).sum())} кривих мають <2 точок — Unreal падає на таких")

    def get(key, domain, types):
        name = m[key]
        a = A.get(name)
        if a is None:
            issues.append(f"немає атрибута '{name}' ({key})")
            return None
        if a.domain != domain or a.data_type not in types:
            issues.append(f"'{name}': {a.domain}/{a.data_type}, очікується {domain}/{'|'.join(types)} — експортер мовчки пропустить")
            return None
        return a

    uv = get("att_groom_root_uv", "CURVE", ("FLOAT2", "FLOAT_VECTOR"))
    if uv is not None:
        v = read(uv, "vector", 2 if uv.data_type == "FLOAT2" else 3)[:, :2]
        info.append(f"root UV {v.min():.4f}..{v.max():.4f} ({uv.data_type})")
        if v.min() < 0 or v.max() > 1:
            issues.append("root UV виходить за 0..1")
    rad = get("att_groom_width", "POINT", ("FLOAT",))
    if rad is not None:
        r = read(rad, "value", 1)
        info.append(f"radius {r.min():.6g}..{r.max():.6g} (одиниці сцени)")
    col = A.get(m["att_groom_color"])
    if col is not None and (col.domain != "POINT" or col.data_type not in ("FLOAT_COLOR", "FLOAT_VECTOR")):
        issues.append(f"color '{col.name}': {col.domain}/{col.data_type}, потрібно POINT/FLOAT_COLOR")

    g = get("att_groom_guide", "CURVE", ("INT", "INT8"))
    gid = get("att_groom_id", "CURVE", ("INT", "INT8"))
    if g is None or gid is None:
        return issues, info
    is_guide = read(g, "value", 1).astype(bool)
    ids = read(gid, "value", 1).astype(np.int64)
    G = int(is_guide.sum())
    info.append(f"гайдів {G}, стрендів {int((~is_guide).sum())}")
    if G == 0:
        issues.append("жодного гайда (groom_is_guide=1)")
        return issues, info
    gids = ids[is_guide]
    if len(np.unique(gids)) != G or gids.min() != 0 or gids.max() != G - 1:
        issues.append(f"id гайдів не 0..{G - 1} без пропусків (є {gids.min()}..{gids.max()}, унікальних {len(np.unique(gids))})")
    if np.intersect1d(gids, ids[~is_guide]).size:
        info.append("увага: id стрендів перетинаються з id гайдів (так у авторських нод; у Ешлін стренди йдуть після гайдів)")

    cl = get("att_groom_closest_guides", "CURVE", ("FLOAT_COLOR", "BYTE_COLOR"))
    w = get("att_groom_guide_weights", "CURVE", ("FLOAT_VECTOR",))
    if cl is not None and w is not None:
        s = ~is_guide
        k = np.rint(read(cl, "color", 4)[s, :3]).astype(np.int64)
        ww = read(w, "vector", 3)[s]
        bad = (k < 0) | (k >= G)
        if bad.any():
            issues.append(f"{int(bad.any(1).sum())} стрендів посилаються на неіснуючий гайд")
        ws = ww.sum(1)
        info.append(f"сума ваг {ws.min():.3f}..{ws.max():.3f}, мін. вага {ww.min():.3f}")
        if ww.min() < 0:
            issues.append("є від'ємні ваги гайдів")
        elif abs(ws - 1).max() > 0.01:
            info.append("увага: сума ваг ≠ 1 (авторські ноди дають 2; у Ешлін нормовано до 1)")
        # відстань від кореня стренда до кореня найближчого гайда
        pos = np.empty(len(c.points) * 3); c.position_data.foreach_get("vector", pos); pos = pos.reshape(-1, 3)
        roots = pos[off[:-1]]
        groot = np.zeros((G, 3)); groot[gids] = roots[is_guide]
        d = np.linalg.norm(roots[s] - groot[np.clip(k[:, 0], 0, G - 1)], axis=1)
        info.append(f"корінь→корінь 1-го гайда: медіана {np.median(d):.4f}, 99% {np.percentile(d, 99):.4f}, макс {d.max():.4f} (одиниці сцени)")
    return issues, info

def report(obs, dg=None):
    dg = dg or bpy.context.evaluated_depsgraph_get()
    out = []
    for ob in obs:
        if ob.type != "CURVES":
            continue
        issues, info = check(ob, dg)
        out.append({"object": ob.name, "ok": not issues, "info": info, "issues": issues})
    return out


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    obs = [bpy.data.objects[n] for n in argv] if argv else list(bpy.context.selected_objects)
    results = report(obs)
    if not results:
        print("Немає виділених Curves-об'єктів")
    us = bpy.context.scene.unit_settings
    print(f"Сцена: unit scale {us.scale_length}, length {us.length_unit}; режим {bpy.context.mode}")
    for r in results:
        print(f"\n=== {r['object']}: {'OK' if r['ok'] else 'ПРОБЛЕМИ'}")
        for line in r["info"]:
            print("   ", line)
        for line in r["issues"]:
            print("  !", line)


if __name__ == "__main__":
    main()
