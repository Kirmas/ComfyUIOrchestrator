import type { BoardItem } from "./types";

/** Geometry helpers for drawn marks on the idea board.
 *
 * A freehand stroke is stored as an SVG path in board coordinates (`M x y L x y
 * …`), which is all it needed while strokes could only be drawn and erased.
 * Giving them the same move/resize grips a circle has means the stroke needs a
 * box too -- and the trick is that it gets one *without* ever rewriting the
 * path: `x/y/w/h` say where the path's own bounding box should be placed, and
 * rendering maps one onto the other with a transform. Scaling a stroke is then
 * exact and repeatable, with no coordinate rewriting to accumulate error.
 *
 * Strokes drawn before this existed have `w`/`h` of 0. They're filled in from
 * their own bounding box on load (locally, no request), which makes the
 * transform an identity -- so an old stroke renders exactly where it always
 * did, and only starts storing a box once it's actually moved.
 */

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Never divide by a zero-width box: a perfectly straight horizontal or
 * vertical stroke has one, and it would scale to Infinity. */
const MIN_EXTENT = 1;

export function pathBounds(path: string | null): Box | null {
  if (!path) return null;
  const numbers = path.match(/-?\d+(\.\d+)?/g);
  if (!numbers || numbers.length < 4) return null;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  // Our paths only ever use M and L, so the numbers are strictly x,y pairs.
  for (let i = 0; i + 1 < numbers.length; i += 2) {
    const x = Number(numbers[i]);
    const y = Number(numbers[i + 1]);
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  if (!Number.isFinite(minX) || !Number.isFinite(minY)) return null;
  return { x: minX, y: minY, w: Math.max(MIN_EXTENT, maxX - minX), h: Math.max(MIN_EXTENT, maxY - minY) };
}

/** The transform that puts a stroke's own bounding box at the box its item
 * claims. Identity for a freshly drawn stroke, since the two start out equal. */
export function inkTransform(item: BoardItem): string | undefined {
  const natural = pathBounds(item.path);
  if (!natural) return undefined;
  const sx = item.w / natural.w;
  const sy = item.h / natural.h;
  if (item.x === natural.x && item.y === natural.y && sx === 1 && sy === 1) return undefined;
  return `translate(${item.x} ${item.y}) scale(${sx} ${sy}) translate(${-natural.x} ${-natural.y})`;
}

/** Fills in the box of any stroke that predates having one, so everything
 * downstream (grips, dragging, the transform above) can read `x/y/w/h`
 * uniformly. Pure -- nothing is written back to the server until the user
 * actually moves or resizes the stroke. */
export function withInkBoxes(items: BoardItem[]): BoardItem[] {
  return items.map((item) => {
    if (item.kind !== "ink" || (item.w > 0 && item.h > 0)) return item;
    const natural = pathBounds(item.path);
    return natural ? { ...item, ...natural } : item;
  });
}
