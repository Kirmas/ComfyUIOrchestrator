import { useEffect, useRef, useState } from "react";

const MIN_ZOOM = 1;
const MAX_ZOOM = 8;

function clampZoom(z: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

/** Full-size image view with scroll-to-zoom and drag-to-pan, for inspecting
 * fine detail in a generated image rather than just seeing it fit-to-screen.
 * maxWidth/maxHeight default to filling a single-image modal; override to
 * embed it in a smaller container. */
export function ZoomableImage({
  src,
  maxWidth = "calc(95vw - 24px)",
  maxHeight = "calc(95vh - 24px)",
}: {
  src: string;
  maxWidth?: string;
  maxHeight?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null);

  // Window-level listeners, not onPointerMove/onPointerUp on the container --
  // pointer capture redirects those back to the element regardless of where
  // the cursor physically is, which sounds like it should be enough, but in
  // practice the drag still stuttered/reset once the cursor left the
  // (transform doesn't grow the layout box, so still fit-to-screen-sized)
  // container. Binding to `window` sidesteps element-boundary behavior
  // entirely -- there's nothing for the cursor to "leave".
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: PointerEvent) => {
      const start = dragRef.current;
      if (!start) return;
      setPan({ x: start.panX + (e.clientX - start.startX), y: start.panY + (e.clientY - start.startY) });
    };
    const onEnd = () => {
      dragRef.current = null;
      setDragging(false);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
    };
  }, [dragging]);

  // A freshly opened image should always start fit-to-screen, not wherever
  // the previous one was left zoomed/panned to.
  useEffect(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, [src]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    // Native non-passive listener -- React's onWheel can't reliably
    // preventDefault (passive by default), and without that the page
    // scrolls behind the modal instead of the image zooming.
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
      setZoom((z) => {
        const next = clampZoom(z * factor);
        if (next === MIN_ZOOM) setPan({ x: 0, y: 0 });
        return next;
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const zoomBy = (factor: number) =>
    setZoom((z) => {
      const next = clampZoom(z * factor);
      if (next === MIN_ZOOM) setPan({ x: 0, y: 0 });
      return next;
    });
  const resetZoom = () => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (zoom <= MIN_ZOOM) return;
    dragRef.current = { startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y };
    setDragging(true);
  };

  return (
    <div
      ref={containerRef}
      className="zoomable-image"
      onPointerDown={onPointerDown}
      onDoubleClick={() => (zoom > MIN_ZOOM ? resetZoom() : zoomBy(2))}
    >
      <img
        src={src}
        alt="full size"
        draggable={false}
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          cursor: zoom > MIN_ZOOM ? (dragging ? "grabbing" : "grab") : "zoom-in",
          maxWidth,
          maxHeight,
        }}
      />
      <div className="zoom-controls" onClick={(e) => e.stopPropagation()}>
        <button type="button" onClick={() => zoomBy(1 / 1.4)} title="Zoom out">
          −
        </button>
        <button type="button" onClick={resetZoom} title="Reset zoom">
          {Math.round(zoom * 100)}%
        </button>
        <button type="button" onClick={() => zoomBy(1.4)} title="Zoom in">
          +
        </button>
      </div>
    </div>
  );
}
