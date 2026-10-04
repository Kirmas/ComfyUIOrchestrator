import { useEffect, useRef, useState } from "react";
import type { ElementType, PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import type { Material, Mesh, MeshNormalMaterial, Object3D } from "three";
import { resolveAssetUrl } from "../api/client";
import { useT } from "../i18n";
import type { TFunc } from "../i18n";
import { acquireRenderSlot, dropStill, loadStill, releaseRenderSlot, saveStill } from "../meshStills";
import type { Asset } from "../types";
import { cx, extensionForMimeType } from "../utils";

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
};

// React has no JSX types for <model-viewer>, and its settings are attributes
// anyway, so the one loose typing lives here instead of on every use.
const ModelViewer = "model-viewer" as unknown as ElementType;

/** Where every mesh view starts: theta 0 is the model's +Z, which is the front
 * a glTF export is authored toward, at model-viewer's own default elevation and
 * framing distance. */
export const FRONT_ORBIT = "0deg 75deg 105%";

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
  if (asset.vertex_count == null || asset.triangle_count == null) return null;
  return t("mesh.stats", {
    verts: asset.vertex_count.toLocaleString(),
    tris: asset.triangle_count.toLocaleString(),
  });
}

/** The name a mesh downloads as, shared by every download control for one (the
 * overlay ⬇ on the thumbnail and the text button in the node's action row).
 *
 * The asset URL ends in `/file`, so without a name the browser saves an
 * extension-less "file", and the stored mime is application/octet-stream, so
 * nothing else says what it is. The stem is the dashboard's name when this
 * cell is that dashboard's result, otherwise a readable creation timestamp
 * (never the asset id). Always .glb: the kind is mesh, the bytes are glTF. */
export function meshDownloadName(asset: Asset, downloadName?: string): string {
  // Callers build downloadName as "<dashboard name>.<extension for the mime>"
  // (here that's ".bin"). Strip exactly that suffix -- a dashboard name can
  // carry dots of its own ("Head v1.2"), so cutting at the last dot would eat them.
  const suffix = `.${extensionForMimeType(asset.mime_type)}`;
  const stem = downloadName
    ? downloadName.endsWith(suffix)
      ? downloadName.slice(0, -suffix.length)
      : downloadName
    : `model_${new Date(asset.created_at).toISOString().slice(0, 19).replace(/[:T]/g, "-")}`;
  return `${stem}.glb`;
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
  downloadName,
}: {
  asset: Asset;
  onOpen: () => void;
  onCompare: () => void;
  downloadName?: string;
}) {
  const t = useT();
  const ready = useModelViewerReady();
  const url = resolveAssetUrl(asset.url);
  const stats = meshStats(asset, t);
  // undefined while the stored still is being looked up, null when there is
  // none (so one must be rendered), otherwise the still's object URL.
  const [still, setStill] = useState<string | null | undefined>(undefined);
  const [rendering, setRendering] = useState(false);
  const [failed, setFailed] = useState(false);
  const viewerRef = useRef<ModelViewerElement | null>(null);

  useEffect(() => {
    let alive = true;
    loadStill(asset.id).then((found) => {
      if (alive) setStill(found);
    });
    return () => {
      alive = false;
    };
  }, [asset.id]);

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
        const dataUrl = viewer.toDataURL("image/webp", 0.9);
        const captured = await saveStill(asset.id, dataUrl);
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
  }, [rendering, asset.id]);

  return (
    <div className="output-thumb mesh-thumb" onDoubleClick={onOpen} title={t("mesh.doubleClickOpen")}>
      {still ? (
        <img src={still} alt="3D" draggable={false} />
      ) : rendering ? (
        <ModelViewer
          ref={viewerRef}
          src={url}
          camera-orbit={FRONT_ORBIT}
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
      <a
        className="zoom-button download-button"
        href={url}
        download={meshDownloadName(asset, downloadName)}
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
          await dropStill(asset.id);
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
  const stats = meshStats(asset, t);

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

  return (
    <div className="mesh-viewer">
      {ready && (
        <ModelViewer
          ref={viewerRef}
          src={url}
          camera-controls
          camera-orbit={FRONT_ORBIT}
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
        <button
          type="button"
          className="mesh-tool"
          onClick={() => {
            const viewer = viewerRef.current;
            if (!viewer) return;
            viewer.cameraOrbit = FRONT_ORBIT;
            viewer.cameraTarget = "auto auto auto";
          }}
        >
          {t("mesh.frontView")}
        </button>
        {stats && <span className="mesh-viewer-stats">{stats}</span>}
      </div>
      {!loaded && <div className="mesh-viewer-loading">{t("mesh.loading")}</div>}
    </div>
  );
}

const targetOf = (viewer: ModelViewerElement): string => {
  const p = viewer.getCameraTarget();
  return `${p.x}m ${p.y}m ${p.z}m`;
};

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
      to.cameraOrbit = from.getCameraOrbit().toString();
      to.cameraTarget = targetOf(from);
    };
    const onLeft = mirror(a, b);
    const onRight = mirror(b, a);
    // Each side that finishes loading snaps the other to the reference view, so
    // a model that arrives second doesn't keep its own default framing.
    const alignToLeft = () => {
      b.cameraOrbit = a.getCameraOrbit().toString();
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
      viewer.cameraOrbit = FRONT_ORBIT;
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
                className="mesh-compare-layer"
                src={resolveAssetUrl(left.url)}
                camera-controls
                camera-orbit={FRONT_ORBIT}
                environment-image="neutral"
                style={{ clipPath: `inset(0 ${(1 - split) * 100}% 0 0)` }}
              />
              <ModelViewer
                ref={rightRef}
                className="mesh-compare-layer"
                src={resolveAssetUrl(right.url)}
                camera-controls
                camera-orbit={FRONT_ORBIT}
                environment-image="neutral"
                style={{ clipPath: `inset(0 0 0 ${split * 100}%)` }}
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
