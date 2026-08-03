/** Phrase vocabulary for Ideogram 4's `style_description` block.
 *
 * Same idea (and same rules) as multiAngleLora.ts: the strings here are
 * literal prompt content the model reads, so they are never translated -- the
 * chip shows the exact phrase that will land in the JSON, and only the group
 * headings and the medium names go through i18n. What's translated is a label
 * ABOUT the value, never the value.
 *
 * `medium` is the odd one out: the docs treat it as a small closed set of
 * categories rather than free prose, and it's also what decides `photo` vs
 * `art_style` (and therefore the key order) -- so it's a real <select> while
 * everything else is comma-joined descriptor chips.
 *
 * Provenance, because it matters when the model was trained on these: the
 * medium list and every phrase for `photograph`, `illustration` and
 * `graphic_design` are lifted verbatim from the examples in
 * ideogram-oss/ideogram4 docs/prompting.md and Ideogram's own JSON-prompting
 * guide. The docs carry no worked examples for `painting` or `3d_render`, so
 * those two lists are ordinary art/render terminology written to match the
 * shape of the documented ones -- treat them as a starting point, not as
 * vocabulary the model was demonstrably trained on.
 */
import type { TKey } from "./i18n";

export interface MediumOption {
  /** The literal value written into style_description.medium. */
  value: string;
  labelKey: TKey;
}

/** The documented set. Not necessarily exhaustive -- the editor also offers a
 * free-text medium, so a value the docs don't list isn't a dead end. */
export const MEDIA: MediumOption[] = [
  { value: "photograph", labelKey: "ideogram.medium.photograph" },
  { value: "illustration", labelKey: "ideogram.medium.illustration" },
  { value: "3d_render", labelKey: "ideogram.medium.render3d" },
  { value: "painting", labelKey: "ideogram.medium.painting" },
  { value: "graphic_design", labelKey: "ideogram.medium.graphicDesign" },
];

/** Mood/tone words. Shared across media -- these read the same whether the
 * output is a photo or a poster. */
export const AESTHETIC_CHIPS: string[] = [
  "moody",
  "cinematic",
  "desaturated",
  "warm",
  "playful",
  "vibrant",
  "serene",
  "minimal",
  "professional",
  "geometric",
  "retro",
  "sophisticated",
  "bold contrast",
  "grain texture",
  "saturated primary colors",
  "rule of thirds",
  "joyful and triumphant",
  "golden hour",
];

const PAINTERLY_LIGHTING = [
  "dramatic",
  "deep shadows",
  "warm spotlight glow",
  "flat even lighting",
  "rim light",
  "chiaroscuro",
  "soft ambient glow",
  "warm stage lighting with amber tones",
];

/** Lighting phrases, narrowed by medium: camera-lighting language for photos,
 * painterly language for the rest. The docs' examples are comma-joined runs
 * ("golden hour, rim light, dramatic shadows"); they're split into single
 * phrases here so chips can be mixed rather than only picked whole. */
export const LIGHTING_CHIPS: Record<string, string[]> = {
  photograph: [
    "bright afternoon sunlight",
    "long soft shadows",
    "golden hour",
    "rim light",
    "dramatic shadows",
    "golden hour backlighting",
    "warm atmospheric haze",
    "diffuse studio lighting",
    "overcast daylight",
    "soft subtle shadows",
    "low-key",
    "deep shadows",
  ],
  illustration: PAINTERLY_LIGHTING,
  painting: PAINTERLY_LIGHTING,
  graphic_design: PAINTERLY_LIGHTING,
  "3d_render": ["soft studio HDRI", "three-point lighting", "sharp key light", "soft fill", "soft global illumination", "rim light"],
};

/** The `photo` / `art_style` value, narrowed by medium -- lens and camera
 * language when the medium is photographic, style language otherwise. */
export const STYLE_DETAIL_CHIPS: Record<string, string[]> = {
  photograph: [
    "shallow depth of field",
    "sharp focus",
    "eye-level",
    "85mm lens",
    "35mm",
    "f/1.4",
    "f/8",
    "bokeh",
    "telephoto",
    "wide angle",
    "long exposure",
  ],
  illustration: [
    "flat vector illustration",
    "bold outlines",
    "flat vector design",
    "generous whitespace",
    "sans-serif typography",
    "line art",
  ],
  painting: [
    "visible brushstrokes",
    "oil impasto",
    "gouache",
    "watercolor washes",
    "painted fantasy key art",
    "aged printed-poster paper grain",
  ],
  graphic_design: [
    "vintage poster design",
    "textured paper",
    "bold typography",
    "muted color palette with warm accents",
    "screen-print texture",
    "minimal grid layout",
  ],
  "3d_render": [
    "clay render",
    "subsurface scattering",
    "physically based materials",
    "isometric camera",
    "soft studio HDRI",
  ],
};

const FALLBACK_DETAIL = ["visible brushstrokes", "bold outlines", "textured paper"];

export function lightingChipsFor(medium: string): string[] {
  return LIGHTING_CHIPS[medium.trim()] ?? PAINTERLY_LIGHTING;
}

export function styleDetailChipsFor(medium: string): string[] {
  return STYLE_DETAIL_CHIPS[medium.trim()] ?? FALLBACK_DETAIL;
}

const split = (value: string): string[] =>
  value
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean);

/** Index of `needle` as a contiguous run inside `parts`, or -1. No chip above
 * contains a comma today, but a multi-phrase one added later would otherwise
 * be un-toggleable (it would append forever and never match), so the run is
 * matched rather than a single part. */
function runIndex(parts: string[], needle: string[]): number {
  if (needle.length === 0) return -1;
  const lower = parts.map((p) => p.toLowerCase());
  const want = needle.map((p) => p.toLowerCase());
  for (let i = 0; i + want.length <= lower.length; i++) {
    if (want.every((w, j) => lower[i + j] === w)) return i;
  }
  return -1;
}

/** Adds a chip to a comma-separated descriptor list, or removes it if it's
 * already there -- chips toggle, so clicking the same one twice undoes it
 * instead of duplicating the phrase. Comparison is on trimmed, case-folded
 * phrases, so a hand-typed entry matches its chip. */
export function toggleChip(value: string, chip: string): string {
  const parts = split(value);
  const needle = split(chip);
  const at = runIndex(parts, needle);
  if (at >= 0) parts.splice(at, needle.length);
  else parts.push(...needle);
  return parts.join(", ");
}

export function hasChip(value: string, chip: string): boolean {
  return runIndex(split(value), split(chip)) >= 0;
}
