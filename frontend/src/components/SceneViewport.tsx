import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { scenesApi } from "../api/endpoints";
import { useT } from "../i18n";
import { useAgentDock } from "../state/agentDock";
import type { SceneAnimation, SceneInfo, SceneSummary } from "../types";

const DRAG_SIZE = 320; // small while the camera moves, sharp once it stops
const REST_SIZE = 800;
// Not things a person picks: an armature rides along with the meshes it deforms (backend view_render).
const HIDDEN_TYPES = new Set(["CAMERA", "LIGHT", "LIGHT_PROBE", "SPEAKER", "EMPTY", "ARMATURE", "LATTICE"]);

/** A look into a live scene without any 3D viewer or file conversion: the page
 * keeps an orbit camera, the backend renders each pose with Workbench inside
 * the already-open Blender (GET /api/scenes/{id}/view -> JPEG). Latency is a
 * render, not a frame rate, so requests are "latest wins": a new pose is asked
 * for only once the previous picture arrived. Picked objects are framed and
 * drawn orange; "Send to agent" attaches the picture, the camera and the
 * picked names to the project chat (state/agentDock.ts). */
export function SceneViewport({ scene, onClose }: { scene: SceneSummary; onClose: () => void }) {
  const t = useT();
  const [info, setInfo] = useState<SceneInfo | null>(null);
  // Only what is ticked is drawn. Meshes start ticked and hair starts off (a groom is the slow part).
  const [error, setError] = useState<string | null>(null);
  // A free camera: a world point it looks at and a distance, fixed until the person moves them. It is NOT
  // re-fitted to the model's bounding box on every frame, which made the picture jump whenever an
  // animation changed the model's size. `camReady` holds the first picture back until it is placed.
  const [cam, setCam] = useState<{ az: number; el: number; dist: number; target: [number, number, number] }>({
    az: 25,
    el: 15,
    dist: 3,
    target: [0, 0, 0],
  });
  const [camReady, setCamReady] = useState(false);
  const [panMode, setPanMode] = useState(false);
  const stage = useRef<HTMLDivElement>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [hair, setHair] = useState(0.1);
  const [textures, setTextures] = useState(true);
  const [frame, setFrame] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  // Animations: which action each armature shows (the scene itself is never changed -- the backend puts it
  // on for one picture and takes it off), and a plain "play" that just asks for frame after frame.
  const [anims, setAnims] = useState<Record<string, string>>({});
  const [animList, setAnimList] = useState<SceneAnimation[]>([]);
  const [playing, setPlaying] = useState(false);
  const clock = useRef({ t0: 0, f0: 0 });
  // The scene changed under us (an agent ran code in it): redraw, and say so while it is busy.
  const [sceneStamp, setSceneStamp] = useState(0);
  const [agentBusy, setAgentBusy] = useState(false);
  const [filter, setFilter] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const [rendering, setRendering] = useState(true);
  const want = useRef<string | null>(null);
  const shownRef = useRef<string | null>(null);
  const busy = useRef(false);
  const drag = useRef<{ x: number; y: number; pan: boolean } | null>(null);

  useEffect(() => {
    scenesApi
      .info(scene.id)
      .then(setInfo)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [scene.id]);

  useEffect(() => {
    let last: string | null = null;
    let alive = true;
    const poll = () =>
      scenesApi
        .get(scene.id)
        .then((sc) => {
          if (!alive) return;
          const sig = `${sc.runs}:${sc.renders}`;
          if (last !== null && sig !== last) setSceneStamp((n) => n + 1);
          last = sig;
          setAgentBusy(!!sc.busy && !busy.current);
        })
        .catch(() => undefined);
    poll();
    const id = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [scene.id]);

  const pump = useCallback(() => {
    if (busy.current || !want.current || want.current === shownRef.current) {
      if (!busy.current) setRendering(false);
      return;
    }
    busy.current = true;
    setRendering(true);
    const url = want.current;
    const img = new Image();
    img.onload = () => {
      shownRef.current = url;
      setShown(url);
      busy.current = false;
      pump();
    };
    img.onerror = () => {
      busy.current = false;
      setError(`view failed (${scene.id})`);
      setRendering(false);
    };
    img.src = url;
  }, [scene.id]);

  const light = dragging || playing; // small, unsmoothed frames while the picture is moving anyway
  useEffect(() => {
    if (!camReady) return;
    want.current = scenesApi.viewUrl(scene.id, {
      ...cam,
      select: picked,
      only: true,
      frame,
      hair,
      textures,
      anim: anims,
      nonce: sceneStamp,
      fast: light,
      size: light ? DRAG_SIZE : REST_SIZE,
    });
    pump();
  }, [scene.id, cam, picked, frame, hair, textures, anims, sceneStamp, light, camReady, pump]);

  const objects = useMemo(
    () => (info?.blender.objects ?? []).filter((o) => !HIDDEN_TYPES.has(o.type)),
    [info],
  );
  const defaults = useMemo(() => objects.filter((o) => o.type !== "CURVES").map((o) => o.name), [objects]);
  useEffect(() => {
    if (!info) return;
    setPicked(defaults);
    reframe(defaults, {}, [])
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setCamReady(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [info, defaults]);
  const q = filter.trim().toLowerCase();
  const listed = objects.filter((o) => !q || o.name.toLowerCase().includes(q));
  const [sceneFrom, sceneTo] = info?.blender.frame_range ?? [0, 0];
  const rangeOf = (a: Record<string, string>) =>
    animList.flatMap((l) => {
      const x = l.actions.find((c) => c.name === a[l.armature]);
      return x ? [x] : [];
    });
  const chosen = rangeOf(anims);
  const from = chosen.length ? Math.min(...chosen.map((a) => a.start)) : sceneFrom;
  const to = chosen.length ? Math.max(...chosen.map((a) => a.end)) : sceneTo;

  // Put the camera where it sees `pick` however it moves over `frames` (the union of its boxes), keeping the angle.
  const reframe = async (pick: string[], actions: Record<string, string>, frames: number[]) => {
    const r = await scenesApi.frame(scene.id, pick, actions, frames);
    setCam((c) => ({ ...c, target: [r.center[0], r.center[1], r.center[2]], dist: r.dist }));
  };
  const sampleFrames = (r: { start: number; end: number }[]) => {
    if (!r.length) return [];
    const a = Math.min(...r.map((x) => x.start));
    const b = Math.max(...r.map((x) => x.end));
    const n = Math.min(8, b - a + 1);
    return Array.from({ length: n }, (_, i) => Math.round(a + ((b - a) * i) / Math.max(1, n - 1)));
  };

  // The actions that fit the armatures of what is ticked (re-read when the ticks change).
  useEffect(() => {
    if (!info) return;
    let alive = true;
    const id = setTimeout(() => {
      scenesApi
        .animations(scene.id, picked)
        .then((list) => {
          if (!alive) return;
          setAnimList(list);
          setAnims((a) =>
            Object.fromEntries(Object.entries(a).filter(([arm, act]) => list.some((l) => l.armature === arm && l.actions.some((x) => x.name === act)))),
          );
        })
        .catch(() => undefined);
    }, 400);
    return () => {
      alive = false;
      clearTimeout(id);
    };
  }, [scene.id, picked, info]);

  // Play = ask for the next frame as soon as the last picture arrived, taking the frame the wall clock says
  // (so the speed is right and a slow scene just skips frames); never the same frame twice.
  useEffect(() => {
    if (!playing || !shown) return;
    const len = to - from + 1;
    if (len < 2) {
      setPlaying(false);
      return;
    }
    const fps = info?.blender.fps || 24;
    const cur = frame ?? from;
    const elapsed = Math.floor(((performance.now() - clock.current.t0) / 1000) * fps);
    let next = from + ((((clock.current.f0 - from + elapsed) % len) + len) % len);
    if (next === cur) next = from + ((cur - from + 1) % len);
    setFrame(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, playing]);

  const togglePlay = () => {
    if (!playing) clock.current = { t0: performance.now(), f0: frame ?? from };
    setPlaying(!playing);
  };
  const pickAction = (armature: string, action: string) => {
    const next = { ...anims };
    if (action) next[armature] = action;
    else delete next[armature];
    setAnims(next);
    const r = rangeOf(next);
    setFrame(r.length ? Math.min(...r.map((a) => a.start)) : null);
    setPlaying(false);
    reframe(picked, next, sampleFrames(r)).catch(() => undefined);
  };

  const toggle = (name: string) => setPicked((p) => (p.includes(name) ? p.filter((n) => n !== name) : [...p, name]));

  const send = async () => {
    try {
      const url = scenesApi.viewUrl(scene.id, { ...cam, select: picked, only: true, frame, hair, textures, anim: anims, size: 1024 });
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(await resp.text());
      useAgentDock.getState().setSceneView({
        sceneId: scene.id,
        sceneName: scene.name,
        ...cam,
        only: true,
        frame,
        actions: anims,
        objects: picked,
        image: await resp.blob(),
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="image-modal-content viewport-modal" onClick={(e) => e.stopPropagation()}>
        <div className="viewport-main">
          <div
            ref={stage}
            className="viewport-stage"
            onContextMenu={(e) => e.preventDefault()}
            onPointerDown={(e) => {
              (e.target as HTMLElement).setPointerCapture(e.pointerId);
              // Right/middle button, Shift, or the ✥ toggle (a phone has none of the others) pans; plain drag orbits.
              drag.current = { x: e.clientX, y: e.clientY, pan: panMode || e.shiftKey || e.button === 1 || e.button === 2 };
              setDragging(true);
            }}
            onPointerMove={(e) => {
              const d = drag.current;
              if (!d) return;
              const dx = e.clientX - d.x;
              const dy = e.clientY - d.y;
              drag.current = { ...d, x: e.clientX, y: e.clientY };
              if (!d.pan) {
                setCam((c) => ({ ...c, az: c.az - dx * 0.5, el: Math.max(-89, Math.min(89, c.el + dy * 0.5)) }));
                return;
              }
              // Slide the target along the picture's own right/up axes: the model follows the finger.
              const k = (0.72 * cam.dist) / Math.max(1, stage.current?.clientHeight ?? 400);
              const a = (cam.az * Math.PI) / 180;
              const el = (cam.el * Math.PI) / 180;
              const right = [Math.cos(a), Math.sin(a), 0];
              const up = [-Math.sin(a) * Math.sin(el), Math.cos(a) * Math.sin(el), Math.cos(el)];
              setCam((c) => ({
                ...c,
                target: [0, 1, 2].map((i) => c.target[i] - right[i] * dx * k + up[i] * dy * k) as [number, number, number],
              }));
            }}
            onPointerUp={() => {
              drag.current = null;
              setDragging(false);
            }}
            onPointerCancel={() => {
              drag.current = null;
              setDragging(false);
            }}
            onWheel={(e) => setCam((c) => ({ ...c, dist: Math.max(0.02, Math.min(500, c.dist * Math.exp(e.deltaY * 0.001))) }))}
          >
            {shown && <img src={shown} alt={scene.name} draggable={false} />}
            {(rendering || agentBusy) && (
              <span className="viewport-busy">{rendering ? t("viewport.rendering") : t("viewport.agentBusy")}</span>
            )}
          </div>
          <div className="viewport-bar">
            <span className="scenes-note">{t("viewport.hint")}</span>
            <button className={panMode ? "active" : undefined} onClick={() => setPanMode(!panMode)} title={t("viewport.panTitle")}>
              ✥
            </button>
            {to > from && (
              <button onClick={togglePlay} title={t("viewport.play")}>
                {playing ? "⏸" : "▶"}
              </button>
            )}
            {to > from && (
              <label className="viewport-frame">
                {t("viewport.frame", { n: frame ?? from })}
                <input
                  type="range"
                  min={from}
                  max={to}
                  value={frame ?? from}
                  onChange={(e) => {
                    setPlaying(false);
                    setFrame(Number(e.target.value));
                  }}
                />
              </label>
            )}
            <button className="primary" onClick={send} title={t("viewport.sendTitle")}>
              {t("viewport.send")}
            </button>
            <button onClick={onClose}>{t("common.close")}</button>
          </div>
          {error && <div className="error-text">{error}</div>}
        </div>
        <div className="viewport-side">
          <strong>{t("viewport.objects")}</strong>
          <input placeholder={t("viewport.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="viewport-side-actions">
            <button onClick={() => setPicked(defaults)}>{t("viewport.clear")}</button>
          </div>
          <div className="viewport-side-actions">
            <button
              onClick={() => reframe(picked, anims, sampleFrames(rangeOf(anims))).catch((err) => setError(String(err.message ?? err)))}
              title={t("viewport.frameAllTitle")}
            >
              {t("viewport.frameAll")}
            </button>
            <button onClick={() => setCam((c) => ({ ...c, target: [0, 0, 0] }))} title={t("viewport.originTitle")}>
              0,0,0
            </button>
          </div>
          <div className="scenes-note" title={t("viewport.camTitle")}>
            {t("viewport.cam", { x: cam.target[0].toFixed(2), y: cam.target[1].toFixed(2), z: cam.target[2].toFixed(2), d: cam.dist.toFixed(2) })}
          </div>
          <label className="viewport-side-actions" title={t("viewport.texturesTitle")}>
            <span>
              <input type="checkbox" checked={textures} onChange={(e) => setTextures(e.target.checked)} /> {t("viewport.textures")}
            </span>
          </label>
          <label className="viewport-side-actions" title={t("viewport.hairTitle")}>
            <span>
              {t("viewport.hair")}{" "}
              <select value={hair} onChange={(e) => setHair(Number(e.target.value))}>
                <option value={0}>{t("viewport.hair.off")}</option>
                <option value={0.1}>10%</option>
                <option value={0.25}>25%</option>
                <option value={1}>{t("viewport.hair.full")}</option>
              </select>
            </span>
          </label>
          {animList.length > 0 && (
            <div className="viewport-anims">
              <strong>{t("viewport.animations")}</strong>
              {animList.map((l) => (
                <label key={l.armature} title={l.meshes.join(", ") || l.armature}>
                  <span className="viewport-obj-name">
                    {(l.meshes.length ? l.meshes : [l.armature]).slice(0, 2).join(", ")}
                    {l.meshes.length > 2 ? ` +${l.meshes.length - 2}` : ""}
                  </span>
                  <select value={anims[l.armature] ?? ""} onChange={(e) => pickAction(l.armature, e.target.value)}>
                    <option value="">{t("viewport.anim.asIs")}</option>
                    {l.actions.map((a) => (
                      <option key={a.name} value={a.name}>
                        {a.name} ({a.start === a.end ? t("viewport.anim.pose") : `${a.start}–${a.end}`})
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          )}
          <div className="viewport-objects">
            {!info && !error && <span className="dim">{t("viewport.loadingInfo")}</span>}
            {listed.map((o) => (
              <label key={o.name} className={picked.includes(o.name) ? "picked" : undefined}>
                <input type="checkbox" checked={picked.includes(o.name)} onChange={() => toggle(o.name)} />
                <span className="viewport-obj-name">{o.name}</span>
                <span className="dim">{o.type}</span>
              </label>
            ))}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}
