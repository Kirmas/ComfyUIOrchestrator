import type { TKey } from "./i18n";
import type { Capability } from "./types";

// The fal Qwen-Image-Edit-2511 "Multiple-Angles" LoRA
// (qwen-image-edit-2511-multiple-angles-lora.safetensors) drives camera angle
// via a rigid prompt grammar -- `<sks> <azimuth> <elevation> <distance>`, with
// a fixed vocabulary for each slot. Typing the exact tokens by hand is
// error-prone, so we surface a builder wherever a capability's workflow is
// wired to that LoRA file. Vocabulary + grammar per the fal HF card and the
// 2511 usage guide.
const LORA_NAME_MATCH = /multiple-angles/i;

/** True if this capability's ComfyUI workflow loads the Multiple-Angles LoRA:
 *  any node whose `inputs.lora_name` filename matches (covers LoraLoader /
 *  LoraLoaderModelOnly / any loader variant). The workflow graph lives in
 *  `config.workflow_json` (a ComfyUI API-format prompt dict). */
export function capabilityUsesMultiAngleLora(capability: Capability | undefined | null): boolean {
  const wf = capability?.config?.["workflow_json"];
  if (!wf || typeof wf !== "object") return false;
  for (const node of Object.values(wf as Record<string, unknown>)) {
    if (!node || typeof node !== "object") continue;
    const inputs = (node as { inputs?: unknown }).inputs;
    if (!inputs || typeof inputs !== "object") continue;
    const loraName = (inputs as { lora_name?: unknown }).lora_name;
    if (typeof loraName === "string" && LORA_NAME_MATCH.test(loraName)) return true;
  }
  return false;
}

export interface AngleOption {
  value: string;
  /** i18n key for the UI caption -- the `value` above is the literal LoRA
   * token and must never be translated. */
  labelKey: TKey;
}

// The eight azimuths, in clockwise order starting from the camera facing the
// subject head-on. `value` is the exact LoRA token; `label` is the UI caption.
export const AZIMUTHS: AngleOption[] = [
  { value: "front view", labelKey: "angle.front" },
  { value: "front-right quarter view", labelKey: "angle.frontRight" },
  { value: "right side view", labelKey: "angle.right" },
  { value: "back-right quarter view", labelKey: "angle.backRight" },
  { value: "back view", labelKey: "angle.back" },
  { value: "back-left quarter view", labelKey: "angle.backLeft" },
  { value: "left side view", labelKey: "angle.left" },
  { value: "front-left quarter view", labelKey: "angle.frontLeft" },
];

export const ELEVATIONS: AngleOption[] = [
  { value: "low-angle shot", labelKey: "angle.low" },
  { value: "eye-level shot", labelKey: "angle.eye" },
  { value: "elevated shot", labelKey: "angle.elevated" },
  { value: "high-angle shot", labelKey: "angle.high" },
];

export const DISTANCES: AngleOption[] = [
  { value: "close-up", labelKey: "angle.closeUp" },
  { value: "medium shot", labelKey: "angle.medium" },
  { value: "wide shot", labelKey: "angle.wide" },
];

export const TRIGGER = "<sks>";

export interface AngleSelection {
  trigger: boolean;
  azimuth: string;
  elevation: string;
  distance: string;
}

export function composeAnglePrompt({ trigger, azimuth, elevation, distance }: AngleSelection): string {
  return [trigger ? TRIGGER : "", azimuth, elevation, distance].filter(Boolean).join(" ").trim();
}

const escRe = (s: string) => s.replace(/[-/\\^$*+?.()|[\]{}]/g, "\\$&");
const altRe = (opts: AngleOption[]) => opts.map((o) => escRe(o.value)).join("|");
// Map a matched token back to its canonical option value (case-insensitive),
// or "" if absent/unrecognized.
const canonical = (opts: AngleOption[], v: string | undefined) =>
  v ? opts.find((o) => o.value.toLowerCase() === v.toLowerCase())?.value ?? "" : "";

// Recognizes a phrase this builder previously composed, so "Insert" can swap it
// in place rather than stack a second one. Derived from the vocab so the two
// never drift. RegExp special chars in the tokens (`-`, `/`) are escaped; the
// leading `<sks>` is optional. Azimuth is required (the phrase's anchor);
// elevation and distance are each optional, since they can be set to "none".
function anglePhraseRegex(): RegExp {
  return new RegExp(
    `(?:${escRe(TRIGGER)}\\s*)?(?:${altRe(AZIMUTHS)})(?:\\s+(?:${altRe(ELEVATIONS)}))?(?:\\s+(?:${altRe(DISTANCES)}))?`,
    "i",
  );
}

/** Parse an existing prompt string back into a selection, so the builder can
 *  open pre-filled from whatever is already in the field (e.g. `<sks> back
 *  view` -> trigger on, azimuth "back view", no elevation/distance). Returns
 *  null when no angle phrase is present, letting the caller fall back to its
 *  own defaults. Same grammar as anglePhraseRegex, but with capture groups. */
export function parseAngleSelection(text: string): AngleSelection | null {
  const re = new RegExp(
    `(${escRe(TRIGGER)})?\\s*(${altRe(AZIMUTHS)})(?:\\s+(${altRe(ELEVATIONS)}))?(?:\\s+(${altRe(DISTANCES)}))?`,
    "i",
  );
  const m = re.exec(text);
  const azimuth = canonical(AZIMUTHS, m?.[2]);
  if (!m || !azimuth) return null;
  return {
    trigger: Boolean(m[1]),
    azimuth,
    elevation: canonical(ELEVATIONS, m[3]),
    distance: canonical(DISTANCES, m[4]),
  };
}

/** Insert the composed phrase into a field's text: if the field already holds a
 *  composed angle phrase, replace it in place; otherwise append after the
 *  existing text. Keeps any surrounding prompt text intact. */
export function upsertAnglePhrase(current: string, phrase: string): string {
  const re = anglePhraseRegex();
  if (re.test(current)) return current.replace(re, phrase);
  const base = current.trim();
  return base ? `${base} ${phrase}` : phrase;
}
