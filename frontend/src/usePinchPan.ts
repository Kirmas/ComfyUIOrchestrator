import { type RefObject, useEffect, useRef, useState } from "react";

export const MIN_ZOOM = 1;
export const MAX_ZOOM = 8;

export interface PinchPanOptions {
  minZoom?: number;
  maxZoom?: number;
  /** Whether dragging pans while at minimum zoom.
   *
   * False (the default, and what the image viewers want): at min zoom the
   * content fits the frame, so there is nothing to pan to and the view snaps
   * back to the origin. True is for a canvas that extends past its frame at
   * every zoom level -- the idea board, where panning at 100% is the primary
   * way to move around. */
  panAtMinZoom?: boolean;
}

export type View = { zoom: number; x: number; y: number };
type Pt = { x: number; y: number };

const dist = (a: Pt, b: Pt) => Math.hypot(a.x - b.x, a.y - b.y);
const mid = (a: Pt, b: Pt): Pt => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

/** Shared zoom + pan gesture logic for both a mouse (scroll to zoom, drag to
 * pan) and touch (pinch to zoom, one-finger drag to pan once zoomed), against
 * a container element. Used by ZoomableImage (single image viewer),
 * CompareModal (its zoom mode) and the idea board so they behave identically --
 * the pinch math lives in exactly one place.
 *
 * `enabled` gates whether gestures are handled at all: CompareModal turns it
 * off in slider mode so the same container can instead drive the compare line.
 *
 * The transform is `translate(x, y) scale(zoom)` with transform-origin at the
 * container's center; zooming is focal-stable (the content point under the
 * cursor / pinch midpoint stays put). */
export function usePinchPan(containerRef: RefObject<HTMLDivElement | null>, enabled = true, options: PinchPanOptions = {}) {
  const { minZoom = MIN_ZOOM, maxZoom = MAX_ZOOM, panAtMinZoom = false } = options;
  const clampZoom = (z: number): number => Math.min(maxZoom, Math.max(minZoom, z));
  const [view, setView] = useState<View>({ zoom: 1, x: 0, y: 0 });
  const [grabbing, setGrabbing] = useState(false);
  // Read by gesture handlers so they don't close over a stale view / enabled.
  const viewRef = useRef(view);
  viewRef.current = view;
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const pointersRef = useRef<Map<number, Pt>>(new Map());
  const gestureRef = useRef<
    | { type: "pan"; startX: number; startY: number; viewX: number; viewY: number }
    | { type: "pinch"; startDist: number; startZoom: number }
    | null
  >(null);

  const reset = () => setView({ zoom: 1, x: 0, y: 0 });

  const zoomToward = (nextZoomRaw: number, focalClientX: number, focalClientY: number) => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    setView((v) => {
      const z1 = clampZoom(nextZoomRaw);
      // Snapping home at min zoom only makes sense when min zoom means
      // "fits the frame"; a board keeps whatever pan the user had.
      if (z1 === minZoom && !panAtMinZoom) return { zoom: 1, x: 0, y: 0 };
      const fx = focalClientX - cx;
      const fy = focalClientY - cy;
      const ratio = z1 / v.zoom;
      return { zoom: z1, x: fx - (fx - v.x) * ratio, y: fy - (fy - v.y) * ratio };
    });
  };

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Native non-passive listener -- React's onWheel is passive by default, so
    // preventDefault() there does nothing and the page scrolls behind instead
    // of the content zooming.
    const onWheel = (e: WheelEvent) => {
      if (!enabledRef.current) return;
      e.preventDefault();
      zoomToward(viewRef.current.zoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2), e.clientX, e.clientY);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (!enabledRef.current) return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const pts = [...pointersRef.current.values()];
    if (pts.length >= 2) {
      gestureRef.current = { type: "pinch", startDist: dist(pts[0], pts[1]), startZoom: viewRef.current.zoom };
      setGrabbing(false);
    } else if (panAtMinZoom || viewRef.current.zoom > minZoom) {
      gestureRef.current = { type: "pan", startX: e.clientX, startY: e.clientY, viewX: viewRef.current.x, viewY: viewRef.current.y };
      setGrabbing(true);
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!pointersRef.current.has(e.pointerId)) return;
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gestureRef.current;
    if (!g) return;
    const pts = [...pointersRef.current.values()];
    if (g.type === "pinch" && pts.length >= 2) {
      const m = mid(pts[0], pts[1]);
      zoomToward(g.startZoom * (dist(pts[0], pts[1]) / g.startDist), m.x, m.y);
    } else if (g.type === "pan") {
      setView((v) => ({ ...v, x: g.viewX + (e.clientX - g.startX), y: g.viewY + (e.clientY - g.startY) }));
    }
  };

  const endPointer = (e: React.PointerEvent) => {
    pointersRef.current.delete(e.pointerId);
    const remaining = [...pointersRef.current.entries()];
    if (remaining.length === 1 && (panAtMinZoom || viewRef.current.zoom > minZoom)) {
      // Pinch just dropped to one finger -- hand off to a pan from where that
      // finger currently is, so the content doesn't jump.
      const [, p] = remaining[0];
      gestureRef.current = { type: "pan", startX: p.x, startY: p.y, viewX: viewRef.current.x, viewY: viewRef.current.y };
    } else if (remaining.length === 0) {
      gestureRef.current = null;
      setGrabbing(false);
    }
  };

  const centerFocal = (): [number, number] => {
    const el = containerRef.current;
    if (!el) return [0, 0];
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  };
  const zoomBy = (factor: number) => zoomToward(viewRef.current.zoom * factor, ...centerFocal());

  return {
    view,
    grabbing,
    reset,
    zoomBy,
    handlers: { onPointerDown, onPointerMove, onPointerUp: endPointer, onPointerCancel: endPointer },
  };
}
