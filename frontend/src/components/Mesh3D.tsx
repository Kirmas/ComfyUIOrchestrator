import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ElementType, PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import type { LineBasicMaterial, Material, Mesh, MeshNormalMaterial, Object3D } from "three";
import { assetsApi } from "../api/endpoints";
import { assetDownloadUrl, resolveAssetUrl } from "../api/client";
import { useT } from "../i18n";
import type { TFunc } from "../i18n";
import { orbitAttr, saveFront, useFront } from "../meshFronts";
import { acquireRenderSlot, dropStill, loadStill, releaseRenderSlot, saveStill } from "../meshStills";
import type { Asset, MeshViewInfo } from "../types";
import { cx } from "../utils";

/** Everything here renders a mesh asset (AssetKind.mesh, a .glb).
 *
 * `@google/model-viewer` bundles the whole of three.js -- roughly a megabyte of
 * the built JS, which used to sit in the main chunk because main.tsx imported
 * it unconditionally at startup. Every page load paid for it, and rollup had to
 * minify it as part of one giant chunk, which is what kept getting the build
 * OOM-killed on this ~2 GB box.
 *
 * So nothing here imports it statically: `useModelViewerReady` loads it the
 * first time a mesh is actually on screen, and a project with no meshes never
 * downloads it at all. The import is module-cached, so every mesh shares one
 * fetch. `three` itself is only ever pulled in by the wireframe/normals toggles,
 * for the same reason.
 */

/** The subset of a <model-viewer> element this file touches. Camera attributes
 * are strings; the getters return the same values as objects. `model` is
 * model-viewer's three.js scene root for the loaded file. */
export type ModelViewerElement = HTMLElement & {
  cameraOrbit: string;
  cameraTarget: string;
  getCameraOrbit(): { theta: number; phi: number; radius: number; toString(): string };
  getCameraTarget(): { x: number; y: number; z: number };
  /** The current frame as an image data URL. */
  toDataURL(type?: string, encoderOptions?: number): string;
  /** Snaps the camera to its goal and redraws. */
  jumpCameraToGoal(): void;
  model?: Object3D;
  /** Animation playback: seconds into the current clip, and its length. */
  currentTime: number;
  readonly duration: number;
  readonly paused: boolean;
  play(): void;
  pause(): void;
};

// React has no JSX types for <model-viewer>, and its settings are attributes
// anyway, so the one loose typing lives here instead of on every use.
const ModelViewer = "model-viewer" as unknown as ElementType;

/** True once @google/model-viewer has registered its element. */
export function useModelViewerReady(): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let cancelled = false;
    import("@google/model-viewer")
      .then(() => {
        if (!cancelled) setReady(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return ready;
}

/** "12 345 vertices · 24 690 triangles", or null when the file's counts are
 * unknown (not a GLB, or nothing readable in it). */
function meshStats(asset: Asset, t: TFunc): string | null {
  if (asset.triangle_count == null) return null;
  const format = (asset.mesh_format ?? "glb").toUpperCase();
  // A format with no shared vertices (STL) reports triangles only.
  const counts =
    asset.vertex_count == null
      ? t("mesh.statsTris", { tris: asset.triangle_count.toLocaleString() })
      : t("mesh.stats", {
          verts: asset.vertex_count.toLocaleString(),
          tris: asset.triangle_count.toLocaleString(),
        });
  return `${format} · ${counts}`;
}

/** Key of a mesh's stored still. A converted format got its own key when STL
 * was oriented Z-up -> Y-up, so stills captured from the old (top-down) view
 * are not reused. */
function stillKey(asset: Asset, front: number[] | null): string {
  const base = asset.mesh_format && asset.mesh_format !== "glb" ? `${asset.id}#y-up3` : asset.id;
  // A person-chosen front is part of the picture, so it is part of the key.
  return front ? `${base}@${front.map((v) => v.toFixed(3)).join(",")}` : base;
}

/** What the 3D viewer loads: a GLB. The original (`url`) is only that when the
 * mesh is stored as GLB; otherwise the server hands out a converted rendition. */
export function meshViewUrl(asset: Asset): string {
  return resolveAssetUrl(asset.view_url ?? asset.url);
}

/** Grid thumbnail: the model's front as a still image, nothing to drag. A cell is
 * 118 CSS px, so orbit controls there only ever got in the way -- and the
 * interaction prompt (model-viewer's hand icon) was drawn over the picture.
 *
 * The still is rendered once per asset (meshStills.ts) and kept: a cell with
 * no stored still mounts a live viewer for its one capture, then drops it. The
 * whole cell is one tap target: double-click or 🔍 opens the viewer, ⇄ starts
 * a compare, just as for an image. */
export function MeshThumb({
  asset,
  onOpen,
  onCompare,
  bare = false,
}: {
  asset: Asset;
  onOpen: () => void;
  // Just the picture, no overlay buttons or stats: a container collage's tile
  // is a quarter of a cell, too small for them, and it's only a preview there.
  bare?: boolean;
  // Omitted where there is nothing to compare against (a board sticker, a chat
  // message) -- the ⇄ button is then not drawn at all.
  onCompare?: () => void;
}) {
  const t = useT();
  const ready = useModelViewerReady();
  const viewUrl = meshViewUrl(asset);
  const stats = meshStats(asset, t);
  const front = useFront(asset);
  const key = stillKey(asset, front);
  // undefined while the stored still is being looked up, null when there is
  // none (so one must be rendered), otherwise the still's object URL.
  const [still, setStill] = useState<string | null | undefined>(undefined);
  const [rendering, setRendering] = useState(false);
  const [failed, setFailed] = useState(false);
  const viewerRef = useRef<ModelViewerElement | null>(null);

  useEffect(() => {
    let alive = true;
    setStill(undefined);
    loadStill(key).then((found) => {
      if (alive) setStill(found);
    });
    return () => {
      alive = false;
    };
  }, [key]);

  // Wait for this cell's turn to render. Released by the cleanup below, which
  // runs once the still exists (or rendering failed), or on unmount.
  useEffect(() => {
    if (still !== null || !ready || failed) return;
    let alive = true;
    let held = false;
    acquireRenderSlot().then(() => {
      if (!alive) {
        releaseRenderSlot();
        return;
      }
      held = true;
      setRendering(true);
    });
    return () => {
      alive = false;
      if (held) releaseRenderSlot();
    };
  }, [still, ready, failed]);

  // The capture itself: once the model has loaded in this cell's viewer, its
  // current frame (the front, the viewer's own starting orbit) becomes the still.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!rendering || !viewer) return;
    let alive = true;
    const onLoad = async () => {
      try {
        // `load` means the file is parsed, not that this element has been drawn:
        // model-viewer frames and paints on its next frames, through one WebGL
        // canvas shared by every element. Capturing at once got whatever that
        // canvas last held (every thumbnail came out the same), so let two
        // frames pass first.
        await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        if (!alive) return;
        const dataUrl = viewer.toDataURL("image/webp", 0.9);
        const captured = await saveStill(key, dataUrl);
        if (alive) setStill(captured);
      } catch {
        if (alive) setFailed(true);
      } finally {
        if (alive) setRendering(false);
      }
    };
    const onError = () => {
      if (!alive) return;
      setRendering(false);
      setFailed(true);
    };
    viewer.addEventListener("load", onLoad);
    viewer.addEventListener("error", onError);
    return () => {
      alive = false;
      viewer.removeEventListener("load", onLoad);
      viewer.removeEventListener("error", onError);
    };
  }, [rendering, key]);

  return (
    <div className="output-thumb mesh-thumb" onDoubleClick={onOpen} title={t("mesh.doubleClickOpen")}>
      {still ? (
        <img src={still} alt="3D" draggable={false} />
      ) : rendering ? (
        <ModelViewer
          ref={viewerRef}
          src={viewUrl}
          camera-orbit={orbitAttr(front)}
          interaction-prompt="none"
          disable-zoom
          disable-pan
          disable-tap
          loading="eager"
          style={{ width: "100%", aspectRatio: "1" }}
        />
      ) : (
        <div className="model-thumb-loading" />
      )}
      {!bare && (
        <>
        <button
          type="button"
          className="zoom-button"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          title={t("cell.openFullSize")}
        >
          🔍
        </button>
        {onCompare && (
          <button
            type="button"
            className="zoom-button compare-button"
            onClick={(e) => {
              e.stopPropagation();
              onCompare();
            }}
            title={t("cell.compareWith")}
          >
            ⇄
          </button>
        )}
        <a
          className="zoom-button download-button"
          href={assetDownloadUrl(asset)}
          download
          onClick={(e) => e.stopPropagation()}
          title={t("cell.download")}
        >
          ⬇
        </a>
        <button
          type="button"
          className="zoom-button refresh-button"
          disabled={rendering}
          onClick={async (e) => {
            e.stopPropagation();
            await dropStill(key);
            setFailed(false);
            setStill(null);
          }}
          title={t("mesh.regenerateThumb")}
        >
          ↻
        </button>
        {stats && (
          <span className="asset-meta-tag">
            <span className="asset-meta-line">{stats}</span>
          </span>
        )}
        </>
      )}
    </div>
  );
}

// Shared by every MeshViewer: three.js is fetched on the first toggle, and the
// normal material is one instance for the whole page.
let normalMaterial: Promise<MeshNormalMaterial> | null = null;
function sharedNormalMaterial(): Promise<MeshNormalMaterial> {
  normalMaterial ??= import("three").then(({ MeshNormalMaterial }) => new MeshNormalMaterial());
  return normalMaterial;
}

/** The three.js root of the loaded file. model-viewer's public `model` is only
 * a material-variant wrapper with no geometry, so the scene is reached through
 * the private symbol it stores under. This is tied to @google/model-viewer 4.x
 * (pinned in the lockfile); if that symbol goes missing, shading toggles do
 * nothing rather than throwing. */
function threeSceneOf(viewer: ModelViewerElement): Object3D | null {
  const key = Object.getOwnPropertySymbols(viewer).find((s) => s.description === "scene");
  const scene = key ? (viewer as unknown as Record<symbol, { model?: Object3D } | undefined>)[key] : undefined;
  return scene?.model ?? null;
}

/** Puts each mesh's authored material back, or swaps in the normal material,
 * and sets wireframe on whichever one is showing. */
function applyShading(model: Object3D, normal: MeshNormalMaterial, wireframe: boolean, normals: boolean) {
  normal.wireframe = wireframe;
  // Many exports carry no NORMAL attribute at all (three then falls back to flat
  // shading). The normal material reads those as zero vectors and paints the
  // whole model black, so they're computed here once, for the normals view.
  normal.flatShading = false;
  model.traverse((node) => {
    const mesh = node as Mesh;
    if (!mesh.isMesh) return;
    if (normals && !mesh.geometry.attributes.normal) mesh.geometry.computeVertexNormals();
    // The first pass records what the file authored, so switching back is exact.
    mesh.userData.authoredMaterial ??= mesh.material;
    const authored = mesh.userData.authoredMaterial as Material | Material[];
    mesh.material = normals ? normal : authored;
    for (const m of ([authored].flat() as (Material & { wireframe?: boolean })[])) m.wireframe = wireframe;
  });
}

/** three.js's loader strips spaces/dots from node names; the names from the file
 * are cleaned the same way to find their nodes. */
const threeName = (n: string) => n.replace(/\s/g, "_").replace(/[[\].:/]/g, "");

/** The root bone of the skeleton that skins the mesh node called `name` (a mesh's
 * own node, or a group holding its primitives), or null if it isn't skinned. */
function skeletonRootOf(model: Object3D, name: string): Object3D | null {
  let root: Object3D | null = null;
  model.traverse((node) => {
    if (root || node.name !== threeName(name)) return;
    node.traverse((inner) => {
      const bone = (inner as { skeleton?: { bones: Object3D[] } }).skeleton?.bones[0];
      if (root || !bone) return;
      let top: Object3D = bone;
      while (top.parent && (top.parent as { isBone?: boolean }).isBone) top = top.parent;
      root = top;
    });
  });
  return root;
}

/** Animation of a converted mesh (an FBX): its clips come from the server's
 * `view-info`, the viewer plays one at a time. Everything here is inert for a
 * mesh without clips, so the static case is unchanged. */
function useMeshAnimation(
  asset: Asset,
  url: string,
  viewerRef: { current: ModelViewerElement | null },
  loaded: boolean,
) {
  const [info, setInfo] = useState<MeshViewInfo | null>(null);
  const [clipName, setClipName] = useState<string | null>(null);
  const [inPlace, setInPlace] = useState(false);
  const [part, setPart] = useState<string>("all");
  const [paused, setPaused] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const wantsInfo = asset.mesh_format === "fbx";

  useEffect(() => {
    if (!wantsInfo) return;
    let alive = true;
    assetsApi.viewInfo(asset.id).then((i) => alive && setInfo(i), () => undefined);
    return () => {
      alive = false;
    };
  }, [asset.id, wantsInfo]);

  // One frame is a pose, not an animation (a T-pose export is exactly that).
  const clips = (info?.clips ?? []).filter((c) => c.frames > 1);
  const clip = clipName && clips.some((c) => c.name === clipName) ? clipName : clips[0]?.name;
  const src = inPlace ? `${url}${url.includes("?") ? "&" : "?"}in_place=true` : url;

  // <model-viewer> starts paused unless `autoplay` was already on when the model
  // loaded, and the clips are only known after the load (view-info is a separate
  // request) -- so start it here, and again when the clip or rendition changes.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!loaded || !viewer || !clips.length) return;
    viewer.play();
    setPaused(false);
  }, [loaded, clips.length, clip, src, viewerRef]);

  // Follows the playhead while it runs (the element has no time event).
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!loaded || !viewer || !clips.length) return;
    let raf = 0;
    const tick = () => {
      setTime(viewer.currentTime);
      setDuration(viewer.duration);
      setPaused(viewer.paused);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [loaded, clips.length, viewerRef]);

  const toggle = () => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    if (viewer.paused) viewer.play();
    else viewer.pause();
    setPaused(viewer.paused);
  };
  const seek = (seconds: number) => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    viewer.pause();
    viewer.currentTime = seconds;
    setPaused(true);
    setTime(seconds);
  };

  return {
    info, part, setPart, clips, clip, setClip: setClipName, inPlace, setInPlace, src, paused, time, duration, toggle, seek,
    hasClips: clips.length > 0,
  };
}

/** "Armature|Armature|Unreal Take|baselayer" -> "Unreal Take": the exporter
 * stacks the object names in front and a layer name behind the take's own. */
function clipLabel(name: string): string {
  const parts = name.split("|");
  if (parts.length > 1 && parts[parts.length - 1] === "baselayer") parts.pop();
  return parts[parts.length - 1] || name;
}

function MeshAnimationBar({ anim }: { anim: ReturnType<typeof useMeshAnimation> }) {
  const t = useT();
  const { info } = anim;
  if (!info) return null;
  return (
    <>
      {(info.meshes?.length ?? 0) > 1 && (
        <select className="mesh-tool" value={anim.part} onChange={(e) => anim.setPart(e.target.value)}>
          <option value="all">{t("mesh.partAll", { n: info.meshes?.length ?? 0 })}</option>
          {info.meshes?.map((m) => (
            <option key={m.name} value={m.name}>
              {m.name} · {m.vertices.toLocaleString()}
            </option>
          ))}
        </select>
      )}
      {info.has_mesh === false && <span className="mesh-viewer-stats">{t("mesh.skeletonOnly")}</span>}
      {anim.hasClips && (
        <>
          <button type="button" className="mesh-tool" onClick={anim.toggle}>
            {anim.paused ? `▶ ${t("mesh.play")}` : `⏸ ${t("mesh.pause")}`}
          </button>
          <input
            type="range"
            className="mesh-anim-slider"
            min={0}
            max={anim.duration || 1}
            step={0.01}
            value={Math.min(anim.time, anim.duration || 1)}
            onChange={(e) => anim.seek(Number(e.target.value))}
          />
          {anim.clips.length > 1 && (
            <select className="mesh-tool" value={anim.clip} onChange={(e) => anim.setClip(e.target.value)}>
              {anim.clips.map((c) => (
                <option key={c.name} value={c.name}>
                  {clipLabel(c.name)} · {c.seconds}s
                </option>
              ))}
            </select>
          )}
          {info.root_motion && (
            <button
              type="button"
              className={cx("mesh-tool", anim.inPlace && "active")}
              aria-pressed={anim.inPlace}
              title={t("mesh.inPlaceHint")}
              onClick={() => anim.setInPlace((v) => !v)}
            >
              {t("mesh.inPlace")}
            </button>
          )}
          <span className="mesh-viewer-stats">
            {t("mesh.clipsInfo", { clips: anim.clips.length, bones: info.bones ?? 0, fps: info.fps ?? 0 })}
          </span>
        </>
      )}
    </>
  );
}


/** The fullscreen body for a mesh asset -- FullSizeModal renders this in place
 * of the zoomable picture, so it keeps the same backdrop, close button and
 * candidate actions. Orbit, zoom and pan are model-viewer's own controls. */
export function MeshViewer({ url, asset }: { url: string; asset: Asset }) {
  const t = useT();
  const ready = useModelViewerReady();
  const viewerRef = useRef<ModelViewerElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [wireframe, setWireframe] = useState(false);
  const [normals, setNormals] = useState(false);
  const [bones, setBones] = useState(false);
  const [hasBones, setHasBones] = useState(false);
  const stats = meshStats(asset, t);
  const front = useFront(asset);
  const [saving, setSaving] = useState(false);
  const anim = useMeshAnimation(asset, url, viewerRef, loaded);

  const goToFront = () => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    viewer.cameraOrbit = orbitAttr(front);
    viewer.cameraTarget = "auto auto auto";
  };
  // Saves the view as it is on screen right now: theta and phi (the two ways
  // the model is turned) and the distance it is looked at from.
  const saveFrontAs = async (orbit: number[] | null) => {
    setSaving(true);
    try {
      await saveFront(asset, orbit);
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };
  const makeFront = () => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const o = viewer.getCameraOrbit();
    void saveFrontAs([o.theta, o.phi, o.radius]);
  };

  // A new src (the in-place rendition) is a new model: it has to load, and be
  // shaded, again.
  useEffect(() => setLoaded(false), [anim.src]);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const onLoad = () => setLoaded(true);
    viewer.addEventListener("load", onLoad);
    return () => viewer.removeEventListener("load", onLoad);
  }, [ready]);

  useEffect(() => {
    const viewer = viewerRef.current;
    const model = viewer ? threeSceneOf(viewer) : null;
    if (!loaded || !model) return;
    let cancelled = false;
    sharedNormalMaterial().then((normal) => {
      if (cancelled || !viewer) return;
      applyShading(model, normal, wireframe, normals);
      // model-viewer only redraws when its camera moves, and swapping a material
      // doesn't move it: without this the canvas keeps showing the old shading.
      viewer.jumpCameraToGoal();
    });
    return () => {
      cancelled = true;
    };
  }, [loaded, wireframe, normals]);

  // The skeleton as lines drawn over the model (three's SkeletonHelper, fetched
  // with three itself on first use). It lives in the top scene object, not under
  // the model: the helper carries the model's own world matrix, so a parent with
  // a transform would apply it twice.
  useEffect(() => {
    const viewer = viewerRef.current;
    const model = viewer ? threeSceneOf(viewer) : null;
    if (!loaded || !viewer || !model) return;
    let found = false;
    model.traverse((o) => {
      if ((o as { isBone?: boolean }).isBone) found = true;
    });
    setHasBones(found);
    if (!bones || !found) return;
    let helper: import("three").SkeletonHelper | null = null;
    let cancelled = false;
    import("three").then(({ SkeletonHelper }) => {
      if (cancelled) return;
      // One mesh picked out of several: only its own skeleton is drawn.
      const only = anim.part === "all" ? null : skeletonRootOf(model, anim.part);
      helper = new SkeletonHelper(only ?? model);
      const material = helper.material as LineBasicMaterial;
      material.depthTest = false; // visible through the mesh
      material.depthWrite = false;
      material.color.set(0xffd54a);
      helper.renderOrder = 999;
      let top: Object3D = model;
      while (top.parent) top = top.parent;
      top.add(helper);
      viewer.jumpCameraToGoal();
    });
    return () => {
      cancelled = true;
      if (helper) {
        helper.removeFromParent();
        helper.dispose();
        viewer.jumpCameraToGoal();
      }
    };
  }, [loaded, bones, anim.src, anim.part]);

  // Which of the file's meshes is shown.
  useEffect(() => {
    const viewer = viewerRef.current;
    const model = viewer ? threeSceneOf(viewer) : null;
    const meshes = anim.info?.meshes ?? [];
    if (!loaded || !viewer || !model || meshes.length < 2) return;
    const names = new Set(meshes.map((m) => threeName(m.name)));
    const shown = anim.part === "all" ? null : threeName(anim.part);
    model.traverse((obj) => {
      if (names.has(obj.name)) obj.visible = shown === null || obj.name === shown;
    });
    viewer.jumpCameraToGoal();
  }, [loaded, anim.part, anim.info]);

  return (
    <div className="mesh-viewer">
      {ready && (
        <ModelViewer
          ref={viewerRef}
          src={anim.src}
          autoplay={anim.hasClips || undefined}
          animation-name={anim.clip}
          camera-controls
          camera-orbit={orbitAttr(front)}
          environment-image="neutral"
          style={{ width: "100%", height: "100%" }}
        />
      )}
      <div className="mesh-viewer-toolbar" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          className={cx("mesh-tool", wireframe && "active")}
          aria-pressed={wireframe}
          onClick={() => setWireframe((w) => !w)}
        >
          {t("mesh.wireframe")}
        </button>
        <button
          type="button"
          className={cx("mesh-tool", normals && "active")}
          aria-pressed={normals}
          onClick={() => setNormals((n) => !n)}
        >
          {t("mesh.normals")}
        </button>
        {hasBones && (
          <button
            type="button"
            className={cx("mesh-tool", bones && "active")}
            aria-pressed={bones}
            onClick={() => setBones((b) => !b)}
          >
            {t("mesh.bones")}
          </button>
        )}
        <button
          type="button"
          className="mesh-tool"
          onClick={goToFront}
        >
          {t("mesh.frontView")}
        </button>
        <button type="button" className="mesh-tool" disabled={!loaded || saving} onClick={makeFront} title={t("mesh.makeFrontHint")}>
          {t("mesh.makeFront")}
        </button>
        {front && (
          <button type="button" className="mesh-tool" disabled={saving} onClick={() => void saveFrontAs(null)}>
            {t("mesh.resetFront")}
          </button>
        )}
        {stats && <span className="mesh-viewer-stats">{stats}</span>}
        <MeshAnimationBar anim={anim} />
      </div>
      {!loaded && <div className="mesh-viewer-loading">{t("mesh.loading")}</div>}
    </div>
  );
}

// Attribute strings for the camera, written as fixed decimals. A plain
// Number#toString() writes tiny values as exponents ("1e-7m"), which
// <model-viewer>'s attribute parser doesn't read.
const orbitOf = (viewer: ModelViewerElement): string => {
  const o = viewer.getCameraOrbit();
  return `${o.theta.toFixed(6)}rad ${o.phi.toFixed(6)}rad ${o.radius.toFixed(6)}m`;
};
const targetOf = (viewer: ModelViewerElement): string => {
  const p = viewer.getCameraTarget();
  return `${p.x.toFixed(6)}m ${p.y.toFixed(6)}m ${p.z.toFixed(6)}m`;
};

// Inline on purpose, not a class: React 18 writes className on a custom element
// as a `classname` attribute, so the stylesheet rule for .mesh-compare-layer never
// applied. Without absolute positioning the two layers stacked in normal flow.
const LAYER_STYLE: CSSProperties = { position: "absolute", inset: 0, width: "100%", height: "100%" };

/** Side-by-side compare of two meshes, split by a divider the user drags. Both
 * models share one camera: orbiting, zooming or panning either side moves the
 * pair, so the seam always shows the same angle of each. The left model is the
 * reference -- the pair opens aligned to it. */
export function MeshCompareModal({ left, right, onClose }: { left: Asset; right: Asset; onClose: () => void }) {
  const t = useT();
  const ready = useModelViewerReady();
  const leftRef = useRef<ModelViewerElement | null>(null);
  const rightRef = useRef<ModelViewerElement | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  // The pair opens on the reference (left) model's front, both models alike.
  const leftFront = orbitAttr(useFront(left));
  // Fraction of the stage width that shows the left model.
  const [split, setSplit] = useState(0.5);

  useEffect(() => {
    const a = leftRef.current;
    const b = rightRef.current;
    if (!ready || !a || !b) return;
    // Only a person's own gesture is mirrored. Setting the other side's
    // attributes below reports back as source "none", which is what stops a
    // pair of listeners from chasing each other forever.
    const mirror = (from: ModelViewerElement, to: ModelViewerElement) => (e: Event) => {
      if ((e as CustomEvent<{ source: string }>).detail?.source !== "user-interaction") return;
      to.cameraOrbit = orbitOf(from);
      to.cameraTarget = targetOf(from);
    };
    const onLeft = mirror(a, b);
    const onRight = mirror(b, a);
    // Each side that finishes loading snaps the other to the reference view, so
    // a model that arrives second doesn't keep its own default framing.
    const alignToLeft = () => {
      b.cameraOrbit = orbitOf(a);
      b.cameraTarget = targetOf(a);
    };
    a.addEventListener("camera-change", onLeft);
    b.addEventListener("camera-change", onRight);
    a.addEventListener("load", alignToLeft);
    b.addEventListener("load", alignToLeft);
    return () => {
      a.removeEventListener("camera-change", onLeft);
      b.removeEventListener("camera-change", onRight);
      a.removeEventListener("load", alignToLeft);
      b.removeEventListener("load", alignToLeft);
    };
  }, [ready]);

  const resetView = () => {
    for (const viewer of [leftRef.current, rightRef.current]) {
      if (!viewer) continue;
      viewer.cameraOrbit = leftFront;
      viewer.cameraTarget = "auto auto auto";
    }
  };

  // The divider follows the pointer while its own pointer capture is held, so a
  // drag that leaves the handle (fast moves, a finger off the line) keeps going.
  const onDividerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    const rect = stageRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    setSplit(Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)));
  };

  // Portalled to document.body for the same reason CompareModal and
  // FullSizeModal are: the grid's transform would otherwise anchor the fixed
  // backdrop to the cell instead of the viewport (CLAUDE.md, 2026-07-21).
  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="image-modal-content image-modal-fullscreen mesh-compare" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="image-modal-close" onClick={onClose} title={t("cell.closeFullSize")}>
          ×
        </button>
        <div className="mesh-compare-stage" ref={stageRef}>
          {ready && (
            <>
              <ModelViewer
                ref={leftRef}
                src={meshViewUrl(left)}
                camera-controls
                camera-orbit={leftFront}
                environment-image="neutral"
                style={{ ...LAYER_STYLE, clipPath: `inset(0 ${(1 - split) * 100}% 0 0)` }}
              />
              <ModelViewer
                ref={rightRef}
                src={meshViewUrl(right)}
                camera-controls
                camera-orbit={leftFront}
                environment-image="neutral"
                style={{ ...LAYER_STYLE, clipPath: `inset(0 0 0 ${split * 100}%)` }}
              />
            </>
          )}
          <div
            className="mesh-compare-divider"
            style={{ left: `${split * 100}%` }}
            onPointerDown={(e) => e.currentTarget.setPointerCapture(e.pointerId)}
            onPointerMove={onDividerMove}
            onPointerUp={(e) => e.currentTarget.releasePointerCapture(e.pointerId)}
            onPointerCancel={(e) => e.currentTarget.releasePointerCapture(e.pointerId)}
          />
        </div>
        <div className="mesh-viewer-toolbar" onClick={(e) => e.stopPropagation()}>
          <button type="button" className="mesh-tool" onClick={resetView}>
            {t("mesh.frontView")}
          </button>
          <span className="mesh-viewer-stats">{t("mesh.compareHint")}</span>
        </div>
        {!ready && <div className="mesh-viewer-loading">{t("mesh.loading")}</div>}
      </div>
    </div>,
    document.body,
  );
}
