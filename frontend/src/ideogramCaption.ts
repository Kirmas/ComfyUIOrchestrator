/** Ideogram 4's structured JSON caption: compose / parse / validate.
 *
 * Ideogram 4 was trained on structured captions, not free text, so the prompt
 * is a JSON object rather than a sentence. We build that object ourselves and
 * feed it into the workflow's ordinary prompt text input (CLIPTextEncode), so
 * nothing here depends on KJNodes' `Ideogram4PromptBuilderKJ` being installed
 * on the ComfyUI side -- that node's only real output is this same string.
 *
 * The caption string in the node's text param is the ONE source of truth: the
 * editor parses it on open and re-composes it on save, and there is no
 * parallel structured copy anywhere. That's what makes "clear the text ->
 * boxes disappear" true, and what lets an agent write a caption by hand and
 * have it show up as draggable boxes with no import step.
 *
 * Schema per https://github.com/ideogram-oss/ideogram4 docs/prompting.md:
 *  - `compositional_deconstruction` is required; the other two are optional.
 *  - `style_description`, when present, needs aesthetics + lighting + medium
 *    AND exactly one of `photo` (photographic) / `art_style` (everything else).
 *  - KEY ORDER IS PART OF THE CONTRACT and is checked by their validator, so
 *    every object here is built by inserting keys in the documented order
 *    (JSON.stringify serializes string keys in insertion order) rather than
 *    spread from some other object.
 *  - bbox is [y_min, x_min, y_max, x_max] in a 0..1000 space, origin top-left,
 *    independent of the output resolution.
 *  - Serialization is compact with no spaces and non-ASCII left literal, which
 *    is exactly what JSON.stringify does by default.
 */
import type { TKey } from "./i18n";
import type { Box } from "./boxGeometry";

export type ElementType = "obj" | "text";

/** One caption element. Geometry is kept as 0..1 fractions of the canvas
 * (the unit the editor drags in) and only becomes Ideogram's 0..1000 integer
 * bbox at compose time. */
export interface CaptionElement extends Box {
  type: ElementType;
  /** The literal string to render. Only meaningful for type "text". */
  text: string;
  desc: string;
  /** Up to 5 uppercase #RRGGBB entries. */
  palette: string[];
}

export interface CaptionStyle {
  aesthetics: string;
  lighting: string;
  /** "" means: emit no style_description block at all (which is legal -- the
   * whole object is optional). Any non-empty value switches the block on and
   * makes aesthetics/lighting/detail required. */
  medium: string;
  /** Serialized as `photo` for a photographic medium and `art_style`
   * otherwise -- the spec's mutually-exclusive pair, picked from `medium` so
   * there's no second control asking the same question twice. */
  detail: string;
  /** Up to 16 uppercase #RRGGBB entries. */
  palette: string[];
}

export interface Caption {
  highLevel: string;
  style: CaptionStyle;
  background: string;
  elements: CaptionElement[];
}

export interface ValidationIssue {
  key: TKey;
  params?: Record<string, string | number>;
}

export const PHOTO_MEDIUM = "photograph";
export const MAX_STYLE_COLORS = 16;
export const MAX_ELEMENT_COLORS = 5;

export const EMPTY_STYLE: CaptionStyle = { aesthetics: "", lighting: "", medium: "", detail: "", palette: [] };
export const EMPTY_CAPTION: Caption = { highLevel: "", style: EMPTY_STYLE, background: "", elements: [] };

/** `photo` vs `art_style` is decided entirely by the medium, per the spec's
 * "photo for photographic subjects, art_style for everything else". */
export function usesPhotoKey(medium: string): boolean {
  return medium.trim().toLowerCase() === PHOTO_MEDIUM;
}

const HEX_RE = /^#[0-9A-F]{6}$/;

export function normalizeHex(value: string): string {
  return value.trim().toUpperCase();
}

// ---------------------------------------------------------------- compose

function toBbox(b: Box): number[] {
  const clamp = (v: number) => Math.max(0, Math.min(1000, Math.round(v * 1000)));
  return [clamp(b.y), clamp(b.x), clamp(b.y + b.height), clamp(b.x + b.width)];
}

function composeStyle(style: CaptionStyle): Record<string, unknown> | null {
  const medium = style.medium.trim();
  if (!medium) return null;
  const out: Record<string, unknown> = {};
  // Key order for photos: aesthetics, lighting, photo, medium, color_palette.
  // For everything else: aesthetics, lighting, medium, art_style, color_palette.
  out.aesthetics = style.aesthetics.trim();
  out.lighting = style.lighting.trim();
  if (usesPhotoKey(medium)) {
    out.photo = style.detail.trim();
    out.medium = medium;
  } else {
    out.medium = medium;
    out.art_style = style.detail.trim();
  }
  const palette = style.palette.map(normalizeHex).filter(Boolean);
  if (palette.length > 0) out.color_palette = palette;
  return out;
}

function composeElement(el: CaptionElement): Record<string, unknown> {
  // obj:  type, bbox, desc, color_palette
  // text: type, bbox, text, desc, color_palette
  const out: Record<string, unknown> = {};
  out.type = el.type;
  out.bbox = toBbox(el);
  if (el.type === "text") out.text = el.text;
  out.desc = el.desc;
  const palette = el.palette.map(normalizeHex).filter(Boolean);
  if (palette.length > 0) out.color_palette = palette;
  return out;
}

export function composeCaption(caption: Caption): string {
  const out: Record<string, unknown> = {};
  const highLevel = caption.highLevel.trim();
  if (highLevel) out.high_level_description = highLevel;
  const style = composeStyle(caption.style);
  if (style) out.style_description = style;
  // background must precede elements, and the whole object is required even
  // when it's empty -- an empty caption still round-trips through the editor.
  out.compositional_deconstruction = {
    background: caption.background.trim(),
    elements: caption.elements.map(composeElement),
  };
  return JSON.stringify(out);
}

// ------------------------------------------------------------------ parse

const str = (v: unknown): string => (typeof v === "string" ? v : "");

function parsePalette(v: unknown, max: number): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((c): c is string => typeof c === "string").map(normalizeHex).slice(0, max);
}

/** An element whose bbox is missing or malformed still has to become a
 * draggable rectangle (the editor is bbox-only on purpose -- describing a
 * position in words is handing control back to the model). Rather than drop
 * the element or invent geometry silently, it gets a visible centered
 * placeholder box and validateCaption() reports it. */
const PLACEHOLDER_BOX: Box = { x: 0.35, y: 0.35, width: 0.3, height: 0.3 };

function parseBox(v: unknown): Box | null {
  if (!Array.isArray(v) || v.length < 4) return null;
  const nums = v.map((n) => (typeof n === "number" ? n : Number(n)));
  if (nums.some((n) => !Number.isFinite(n))) return null;
  const [yMin, xMin, yMax, xMax] = nums;
  const box = { x: xMin / 1000, y: yMin / 1000, width: (xMax - xMin) / 1000, height: (yMax - yMin) / 1000 };
  if (box.width <= 0 || box.height <= 0) return null;
  return box;
}

function parseElement(raw: unknown): CaptionElement | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const type: ElementType = obj.type === "text" ? "text" : "obj";
  const box = parseBox(obj.bbox) ?? PLACEHOLDER_BOX;
  return {
    ...box,
    type,
    text: str(obj.text),
    desc: str(obj.desc),
    palette: parsePalette(obj.color_palette, MAX_ELEMENT_COLORS),
  };
}

/** Tolerant read of whatever is currently in the text param. Returns null only
 * when it isn't a JSON object at all -- everything below that (missing keys,
 * wrong types, absent bboxes) is repaired to something editable and surfaced
 * through validateCaption()/validateRaw() instead of refusing to open. */
export function parseCaption(rawText: string): Caption | null {
  const trimmed = rawText.trim();
  if (!trimmed) return { ...EMPTY_CAPTION, style: { ...EMPTY_STYLE }, elements: [] };
  let data: unknown;
  try {
    data = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const root = data as Record<string, unknown>;

  const styleRaw = (root.style_description ?? {}) as Record<string, unknown>;
  const hasStyle = Boolean(root.style_description) && typeof root.style_description === "object";
  const style: CaptionStyle = {
    aesthetics: str(styleRaw.aesthetics),
    lighting: str(styleRaw.lighting),
    medium: hasStyle ? str(styleRaw.medium) : "",
    detail: str(styleRaw.photo) || str(styleRaw.art_style),
    palette: parsePalette(styleRaw.color_palette, MAX_STYLE_COLORS),
  };

  const comp = (root.compositional_deconstruction ?? {}) as Record<string, unknown>;
  const elementsRaw = Array.isArray(comp.elements) ? comp.elements : [];

  return {
    highLevel: str(root.high_level_description),
    style,
    background: str(comp.background),
    elements: elementsRaw.map(parseElement).filter((el): el is CaptionElement => el !== null),
  };
}

// --------------------------------------------------------------- validate

const STYLE_ORDER_PHOTO = ["aesthetics", "lighting", "photo", "medium", "color_palette"];
const STYLE_ORDER_ART = ["aesthetics", "lighting", "medium", "art_style", "color_palette"];
const ELEMENT_ORDER_OBJ = ["type", "bbox", "desc", "color_palette"];
const ELEMENT_ORDER_TEXT = ["type", "bbox", "text", "desc", "color_palette"];
const ROOT_ORDER = ["high_level_description", "style_description", "compositional_deconstruction"];

/** True when `keys` appear in the same relative order as `expected`
 * (extra/absent keys are ignored -- only the ordering of the ones that ARE
 * there is the contract). */
function inOrder(keys: string[], expected: string[]): boolean {
  const positions = keys.filter((k) => expected.includes(k)).map((k) => expected.indexOf(k));
  return positions.every((p, i) => i === 0 || p > positions[i - 1]);
}

/** Checks the raw text as written -- key order and JSON validity -- which is
 * the part that can only be wrong when the caption came from somewhere other
 * than this editor (hand-typed, or written by an agent). Anything the editor
 * saves is canonical by construction, so these issues are shown once on open
 * and disappear as soon as it's saved. */
export function validateRaw(rawText: string): ValidationIssue[] {
  const trimmed = rawText.trim();
  if (!trimmed) return [];
  let data: unknown;
  try {
    data = JSON.parse(trimmed);
  } catch {
    return [{ key: "ideogram.issue.badJson" }];
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return [{ key: "ideogram.issue.notObject" }];
  const root = data as Record<string, unknown>;
  const issues: ValidationIssue[] = [];

  if (!inOrder(Object.keys(root), ROOT_ORDER)) issues.push({ key: "ideogram.issue.keyOrder", params: { where: "caption" } });

  const style = root.style_description;
  if (style && typeof style === "object") {
    const keys = Object.keys(style as Record<string, unknown>);
    const expected = "photo" in (style as Record<string, unknown>) ? STYLE_ORDER_PHOTO : STYLE_ORDER_ART;
    if (!inOrder(keys, expected)) issues.push({ key: "ideogram.issue.keyOrder", params: { where: "style_description" } });
  }

  const comp = root.compositional_deconstruction;
  if (comp && typeof comp === "object") {
    const compKeys = Object.keys(comp as Record<string, unknown>);
    if (!inOrder(compKeys, ["background", "elements"])) {
      issues.push({ key: "ideogram.issue.keyOrder", params: { where: "compositional_deconstruction" } });
    }
    const elements = (comp as Record<string, unknown>).elements;
    if (Array.isArray(elements)) {
      elements.forEach((el, i) => {
        if (!el || typeof el !== "object") return;
        const keys = Object.keys(el as Record<string, unknown>);
        const expected = (el as Record<string, unknown>).type === "text" ? ELEMENT_ORDER_TEXT : ELEMENT_ORDER_OBJ;
        if (!inOrder(keys, expected)) issues.push({ key: "ideogram.issue.keyOrder", params: { where: `elements[${i}]` } });
        if (!parseBox((el as Record<string, unknown>).bbox)) issues.push({ key: "ideogram.issue.missingBbox", params: { n: i + 1 } });
      });
    }
  } else {
    issues.push({ key: "ideogram.issue.noComposition" });
  }
  return issues;
}

/** Checks the caption the editor currently holds -- the rules a human can
 * break while filling it in. Runs live under the canvas. */
export function validateCaption(caption: Caption): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const { style } = caption;

  if (style.medium.trim()) {
    // All-or-nothing: a style_description missing any of these is not "a bit
    // of style", it's an invalid block.
    const missing: string[] = [];
    if (!style.aesthetics.trim()) missing.push("aesthetics");
    if (!style.lighting.trim()) missing.push("lighting");
    if (!style.detail.trim()) missing.push(usesPhotoKey(style.medium) ? "photo" : "art_style");
    if (missing.length > 0) issues.push({ key: "ideogram.issue.styleIncomplete", params: { fields: missing.join(", ") } });
  }

  for (const color of style.palette) {
    if (!HEX_RE.test(normalizeHex(color))) issues.push({ key: "ideogram.issue.badHex", params: { color } });
  }
  if (style.palette.length > MAX_STYLE_COLORS) {
    issues.push({ key: "ideogram.issue.tooManyColors", params: { max: MAX_STYLE_COLORS } });
  }

  if (!caption.background.trim()) issues.push({ key: "ideogram.issue.emptyBackground" });
  if (caption.elements.length === 0) issues.push({ key: "ideogram.issue.noElements" });

  caption.elements.forEach((el, i) => {
    const n = i + 1;
    if (!el.desc.trim()) issues.push({ key: "ideogram.issue.emptyDesc", params: { n } });
    if (el.type === "text" && !el.text.trim()) issues.push({ key: "ideogram.issue.emptyText", params: { n } });
    if (el.palette.length > MAX_ELEMENT_COLORS) issues.push({ key: "ideogram.issue.tooManyElementColors", params: { n, max: MAX_ELEMENT_COLORS } });
    for (const color of el.palette) {
      if (!HEX_RE.test(normalizeHex(color))) issues.push({ key: "ideogram.issue.badHex", params: { color } });
    }
  });

  return issues;
}
