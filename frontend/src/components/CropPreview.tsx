import { useEffect, useMemo, useRef, useState } from "react";

export interface CropBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

type DragMode = "move" | "nw" | "ne" | "sw" | "se";
type AspectPreset = "free" | "1:1" | "16:9" | "custom";

const ASPECT_PRESETS: { key: AspectPreset; label: string }[] = [
  { key: "free", label: "Free" },
  { key: "1:1", label: "1:1" },
  { key: "16:9", label: "16:9" },
  { key: "custom", label: "Custom…" },
];

/** Interactive crop-box overlay: drag the body to move it, drag a corner to
 * resize. Coordinates are tracked in source-image pixels (not display
 * pixels) throughout, converted via the ratio between the <img>'s rendered
 * width and its naturalWidth -- that ratio is the same on both axes since
 * the image is never displayed cropped/stretched, only uniformly scaled.
 *
 * An aspect preset (mirroring ComfyUI's own crop-node UI) constrains corner
 * drags to keep width/height at that ratio -- move is never affected, only
 * resize. It's local interaction state, not sent anywhere: the backend only
 * ever sees the resulting x/y/width/height, same as free-form dragging. */
export function CropPreview({ imageUrl, box, onCommit }: { imageUrl: string; box: CropBox; onCommit: (box: CropBox) => void }) {
  const imgRef = useRef<HTMLImageElement>(null);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [liveBox, setLiveBox] = useState<CropBox>(box);
  const dragging = useRef(false);
  const [aspectPreset, setAspectPreset] = useState<AspectPreset>("free");
  const [customRatio, setCustomRatio] = useState({ w: 4, h: 5 });

  // Only re-sync from the committed `box` prop when nothing is being
  // dragged right now -- without this guard, the onCommit round-trip
  // (parent re-renders with the server's echoed params mid-drag) would snap
  // liveBox back to the last-committed value and fight the user's own
  // in-progress drag.
  useEffect(() => {
    if (!dragging.current) setLiveBox(box);
  }, [box.x, box.y, box.width, box.height]);

  const ratio = useMemo(() => {
    if (aspectPreset === "1:1") return 1;
    if (aspectPreset === "16:9") return 16 / 9;
    if (aspectPreset === "custom") return customRatio.w > 0 && customRatio.h > 0 ? customRatio.w / customRatio.h : null;
    return null;
  }, [aspectPreset, customRatio]);

  // Deliberately axis-independent -- it does NOT preserve aspect ratio.
  // Callers that need a ratio kept (applyRatio, and the fixed-ratio branch
  // of onMove below) do their own ratio-aware fit *before* calling this, and
  // rely on it only as the final "don't go outside the image" safety net.
  const clamp = (b: CropBox, w: number, h: number): CropBox => {
    const width = Math.max(4, Math.min(b.width, w));
    const height = Math.max(4, Math.min(b.height, h));
    const x = Math.max(0, Math.min(b.x, w - width));
    const y = Math.max(0, Math.min(b.y, h - height));
    return { x, y, width, height };
  };

  // Snaps the current box to a newly-picked ratio right away (centered on
  // its current middle), instead of waiting for the next drag -- picking
  // "1:1" should visibly do something immediately, same as ComfyUI's widget.
  const applyRatio = (r: number | null) => {
    if (!r || !natural) return;
    const centerX = liveBox.x + liveBox.width / 2;
    const centerY = liveBox.y + liveBox.height / 2;
    let width = liveBox.width;
    let height = width / r;
    if (height > natural.h) {
      height = natural.h;
      width = height * r;
    }
    if (width > natural.w) {
      width = natural.w;
      height = width / r;
    }
    const next = clamp({ x: centerX - width / 2, y: centerY - height / 2, width, height }, natural.w, natural.h);
    setLiveBox(next);
    onCommit(next);
  };

  // Pointer Events (not mouse events) so this drags with a finger on a phone
  // just as well as a mouse -- one code path for both, no separate touch
  // handlers to keep in sync.
  const startDrag = (mode: DragMode) => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!natural || !imgRef.current) return;
    dragging.current = true;
    const startX = e.clientX;
    const startY = e.clientY;
    const startBox = liveBox;
    const rectWidth = imgRef.current.getBoundingClientRect().width;
    const scale = natural.w / rectWidth;

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      const dx = (ev.clientX - startX) * scale;
      const dy = (ev.clientY - startY) * scale;
      let next: CropBox;
      if (mode === "move") {
        next = { ...startBox, x: startBox.x + dx, y: startBox.y + dy };
      } else if (ratio) {
        // Fixed ratio: drive the resize off horizontal movement and derive
        // height from it, anchored at whichever corner is opposite the one
        // being dragged (so that corner stays put, like a normal crop tool).
        const width = Math.max(4, mode.includes("w") ? startBox.width - dx : startBox.width + dx);
        const height = width / ratio;
        const x = mode.includes("w") ? startBox.x + startBox.width - width : startBox.x;
        const y = mode.includes("n") ? startBox.y + startBox.height - height : startBox.y;
        next = { x, y, width, height };
      } else {
        let { x, y, width, height } = startBox;
        if (mode.includes("n")) {
          y = startBox.y + dy;
          height = startBox.height - dy;
        }
        if (mode.includes("s")) {
          height = startBox.height + dy;
        }
        if (mode.includes("w")) {
          x = startBox.x + dx;
          width = startBox.width - dx;
        }
        if (mode.includes("e")) {
          width = startBox.width + dx;
        }
        next = { x, y, width, height };
      }
      setLiveBox(clamp(next, natural.w, natural.h));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      dragging.current = false;
      setLiveBox((current) => {
        onCommit(current);
        return current;
      });
    };
    // Listen on window, not the handle/box element itself: a fast drag
    // routinely moves the pointer outside the small handle (or even outside
    // the image) mid-gesture, and only window-level listeners keep receiving
    // events once that happens. "pointercancel" (browser takes over the
    // gesture, e.g. a system back-swipe) is handled the same as pointerup so
    // a drag can't get stuck thinking it's still active.
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const pct = (v: number, total: number) => `${total > 0 ? (v / total) * 100 : 0}%`;
  const corners: DragMode[] = ["nw", "ne", "sw", "se"];

  return (
    <div onClick={(e) => e.stopPropagation()}>
      <div style={{ display: "flex", gap: 4, marginBottom: 4, flexWrap: "wrap" }}>
        {ASPECT_PRESETS.map((preset) => (
          <button
            key={preset.key}
            className={aspectPreset === preset.key ? "active" : ""}
            style={{ fontSize: 10, padding: "1px 6px" }}
            onClick={() => {
              setAspectPreset(preset.key);
              if (preset.key !== "custom") applyRatio(preset.key === "1:1" ? 1 : 16 / 9);
              else applyRatio(customRatio.w / customRatio.h);
            }}
          >
            {preset.label}
          </button>
        ))}
        {aspectPreset === "custom" && (
          <span style={{ display: "flex", gap: 2, alignItems: "center" }}>
            <input
              type="number"
              min={1}
              value={customRatio.w}
              onChange={(e) => {
                const w = Number(e.target.value) || 1;
                setCustomRatio((r) => ({ ...r, w }));
                applyRatio(w / customRatio.h);
              }}
              style={{ width: 40, fontSize: 10, padding: "1px 4px" }}
            />
            <span style={{ fontSize: 10 }}>:</span>
            <input
              type="number"
              min={1}
              value={customRatio.h}
              onChange={(e) => {
                const h = Number(e.target.value) || 1;
                setCustomRatio((r) => ({ ...r, h }));
                applyRatio(customRatio.w / h);
              }}
              style={{ width: 40, fontSize: 10, padding: "1px 4px" }}
            />
          </span>
        )}
      </div>
      <div style={{ position: "relative", userSelect: "none", lineHeight: 0 }}>
        <img
          ref={imgRef}
          src={imageUrl}
          alt="crop source"
          style={{ width: "100%", display: "block", borderRadius: 4 }}
          onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
          draggable={false}
        />
        {natural && (
          <div
            onPointerDown={startDrag("move")}
            style={{
              position: "absolute",
              left: pct(liveBox.x, natural.w),
              top: pct(liveBox.y, natural.h),
              width: pct(liveBox.width, natural.w),
              height: pct(liveBox.height, natural.h),
              border: "2px solid var(--accent)",
              background: "rgba(59, 111, 224, 0.18)",
              cursor: "move",
              boxSizing: "border-box",
              touchAction: "none",
            }}
          >
            {corners.map((corner) => (
              <div
                key={corner}
                onPointerDown={startDrag(corner)}
                style={{
                  position: "absolute",
                  width: 18,
                  height: 18,
                  background: "var(--accent)",
                  borderRadius: 2,
                  cursor: `${corner}-resize`,
                  touchAction: "none",
                  top: corner.includes("n") ? -9 : undefined,
                  bottom: corner.includes("s") ? -9 : undefined,
                  left: corner.includes("w") ? -9 : undefined,
                  right: corner.includes("e") ? -9 : undefined,
                }}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
