import { type CSSProperties, useEffect, useRef } from "react";
import { MIN_ZOOM, usePinchPan } from "../usePinchPan";

/** Full-size image view with zoom + pan, for inspecting fine detail rather
 * than just seeing it fit-to-screen. Works with both a mouse (scroll to zoom,
 * drag to pan, double-click to toggle) and touch (pinch to zoom, one-finger
 * drag to pan once zoomed) -- see usePinchPan, shared with CompareModal's zoom
 * mode. maxWidth/maxHeight default to filling a single-image modal; override
 * to embed it in a smaller container. */
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
  const { view, grabbing, reset, zoomBy, handlers } = usePinchPan(containerRef);

  // A freshly opened image should always start fit-to-screen, not wherever the
  // previous one was left zoomed/panned to.
  useEffect(() => {
    reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);

  return (
    <div
      ref={containerRef}
      className="zoomable-image"
      {...handlers}
      onDoubleClick={() => (view.zoom > MIN_ZOOM ? reset() : zoomBy(2))}
      // Fed to the img's max-width/height via CSS var rather than an inline
      // style on the img itself -- an inline style would win over the
      // fullscreen media query that wants the image to fill the whole phone
      // screen (see .image-modal-fullscreen in index.css).
      style={{ ["--zoom-max-w" as string]: maxWidth, ["--zoom-max-h" as string]: maxHeight } as CSSProperties}
    >
      <img
        src={src}
        alt="full size"
        draggable={false}
        style={{
          transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`,
          cursor: view.zoom > MIN_ZOOM ? (grabbing ? "grabbing" : "grab") : "zoom-in",
        }}
      />
      <div className="zoom-controls" onClick={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()}>
        <button type="button" onClick={() => zoomBy(1 / 1.4)} title="Zoom out">
          −
        </button>
        <button type="button" onClick={reset} title="Reset zoom">
          {Math.round(view.zoom * 100)}%
        </button>
        <button type="button" onClick={() => zoomBy(1.4)} title="Zoom in">
          +
        </button>
      </div>
    </div>
  );
}
