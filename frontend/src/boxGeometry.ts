/** Rectangle move/resize/clamp math, shared by every draggable-box overlay.
 *
 * Deliberately unit-agnostic: CropPreview works in source-image pixels,
 * CaptionBoxEditor works in 0..1 fractions of the canvas. Both do the exact
 * same arithmetic, so it lives here once rather than being re-derived per
 * component (the two drifting apart is precisely what the duplication rule in
 * CLAUDE.md is about). Callers supply their own bounds and minimum size in
 * whatever unit they're using.
 */

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Which corner is being dragged. Spelled as compass directions so the
 * substring tests below ("n"/"s"/"w"/"e") read directly off the mode. */
export type ResizeMode = "nw" | "ne" | "sw" | "se";

/** Fits a box inside 0,0..maxW,maxH. Deliberately axis-independent -- it does
 * NOT preserve aspect ratio; callers that need a ratio kept do their own
 * ratio-aware fit first and rely on this only as the final "don't go outside"
 * safety net. */
export function clampBox(b: Box, maxW: number, maxH: number, minSize: number): Box {
  const width = Math.max(minSize, Math.min(b.width, maxW));
  const height = Math.max(minSize, Math.min(b.height, maxH));
  const x = Math.max(0, Math.min(b.x, maxW - width));
  const y = Math.max(0, Math.min(b.y, maxH - height));
  return { x, y, width, height };
}

export function moveBox(b: Box, dx: number, dy: number): Box {
  return { ...b, x: b.x + dx, y: b.y + dy };
}

/** Free (unconstrained) corner resize: the dragged corner follows the pointer
 * and the opposite one stays put. Can produce a negative width/height when
 * dragged past the anchor -- normalizeBox() below is what fixes that up, so a
 * drag through the opposite corner flips the box instead of collapsing it. */
export function resizeBox(b: Box, mode: ResizeMode, dx: number, dy: number): Box {
  let { x, y, width, height } = b;
  if (mode.includes("n")) {
    y = b.y + dy;
    height = b.height - dy;
  }
  if (mode.includes("s")) {
    height = b.height + dy;
  }
  if (mode.includes("w")) {
    x = b.x + dx;
    width = b.width - dx;
  }
  if (mode.includes("e")) {
    width = b.width + dx;
  }
  return { x, y, width, height };
}

/** Turns a box with negative extents (drawn right-to-left / bottom-to-top, or
 * resized past its own anchor corner) into the equivalent positive one. */
export function normalizeBox(b: Box): Box {
  return {
    x: b.width < 0 ? b.x + b.width : b.x,
    y: b.height < 0 ? b.y + b.height : b.y,
    width: Math.abs(b.width),
    height: Math.abs(b.height),
  };
}

export function boxContains(b: Box, px: number, py: number): boolean {
  return px >= b.x && px <= b.x + b.width && py >= b.y && py <= b.y + b.height;
}

export function boxArea(b: Box): number {
  return Math.abs(b.width * b.height);
}
