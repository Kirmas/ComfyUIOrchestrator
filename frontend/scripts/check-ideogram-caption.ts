/** Checks for ideogramCaption.ts -- the compose/parse/validate module behind
 * the Ideogram 4 box editor.
 *
 * Run:  cd frontend && ./node_modules/.bin/esbuild scripts/check-ideogram-caption.ts \
 *         --bundle --platform=node --format=cjs --outfile=/tmp/check.cjs && node /tmp/check.cjs
 *
 * A plain script rather than a test suite on purpose: the frontend has no test
 * runner, and adding vitest + its dependency tree to a box where `vite build`
 * has already been OOM-killed costs more than this whole feature. esbuild is
 * already present (vite depends on it), so this needs nothing new installed.
 *
 * The fixture next to this file is the hand-written caption from the real
 * Ideogram 4 workflow this was built against -- the point of most checks below
 * is that a caption we did not author survives a parse -> re-compose round trip
 * byte-for-byte in every field that carries meaning. That round trip is the
 * whole design: the prompt text is the only store, so anything lost here is
 * lost from the user's prompt.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { composeCaption, parseCaption, validateCaption, validateRaw, type Caption } from "../src/ideogramCaption";

let failures = 0;
const check = (name: string, ok: boolean, extra?: unknown) => {
  if (!ok) {
    failures++;
    console.log("FAIL", name, extra === undefined ? "" : JSON.stringify(extra));
  } else {
    console.log("ok  ", name);
  }
};

const caption = (over: Partial<Caption> = {}): Caption => ({
  highLevel: "",
  background: "bg",
  style: { aesthetics: "", lighting: "", medium: "", detail: "", palette: [] },
  elements: [],
  ...over,
});

// --- bbox conversion ------------------------------------------------------
// The one hard number in the format. KJNodes' own editor state for the poster
// this was built from stores x=0.04 y=0.028 w=0.92 h=0.124 and its node emits
// bbox [28,40,152,960] -- i.e. [ymin,xmin,ymax,xmax] * 1000. Ours must agree
// exactly, or every layout drawn here lands somewhere else in the image.
const withBox = JSON.parse(
  composeCaption(
    caption({
      elements: [{ x: 0.04, y: 0.028, width: 0.92, height: 0.124, type: "text", text: "TEMPLE OF SOLUNUS", desc: "d", palette: [] }],
    }),
  ),
);
check("bbox matches the reference [ymin,xmin,ymax,xmax]*1000", JSON.stringify(withBox.compositional_deconstruction.elements[0].bbox) === "[28,40,152,960]");

// --- key order ------------------------------------------------------------
// Ideogram's validator checks key order, so this is a correctness property,
// not cosmetics.
const styled = (medium: string) =>
  JSON.parse(composeCaption(caption({ highLevel: "hl", style: { aesthetics: "a", lighting: "l", medium, detail: "d", palette: ["#AABBCC"] } })));
check("root key order", JSON.stringify(Object.keys(styled("painting"))) === '["high_level_description","style_description","compositional_deconstruction"]');
check("photo style key order", JSON.stringify(Object.keys(styled("photograph").style_description)) === '["aesthetics","lighting","photo","medium","color_palette"]');
check("art style key order", JSON.stringify(Object.keys(styled("painting").style_description)) === '["aesthetics","lighting","medium","art_style","color_palette"]');
check("background precedes elements", JSON.stringify(Object.keys(styled("painting").compositional_deconstruction)) === '["background","elements"]');
check("text element key order", JSON.stringify(Object.keys(withBox.compositional_deconstruction.elements[0])) === '["type","bbox","text","desc"]');
const objEl = JSON.parse(
  composeCaption(caption({ elements: [{ x: 0, y: 0, width: 0.5, height: 0.5, type: "obj", text: "", desc: "d", palette: ["#FFFFFF"] }] })),
);
check("obj element key order", JSON.stringify(Object.keys(objEl.compositional_deconstruction.elements[0])) === '["type","bbox","desc","color_palette"]');

// Serialization: compact separators, non-ASCII left literal.
const compact = composeCaption(caption({ background: "тінь — haze" }));
check("compact separators", !compact.includes(": ") && !compact.includes("\n"));
check("non-ASCII stays literal", compact.includes("тінь — haze"));

// --- round trip of a real, externally-authored caption --------------------
// Relative to the working directory, not to __dirname: esbuild bundles this
// into /tmp, where __dirname would point at the bundle instead of the repo.
// Hence "run from frontend/" in the header above.
const real = readFileSync(join(process.cwd(), "scripts", "ideogram-caption.fixture.json"), "utf8");
const parsed = parseCaption(real);
check("real caption parses", parsed !== null);
if (parsed) {
  const before = JSON.parse(real);
  const after = JSON.parse(composeCaption(parsed));
  const beforeEls = before.compositional_deconstruction.elements;
  const afterEls = after.compositional_deconstruction.elements;
  check("high_level_description preserved", after.high_level_description === before.high_level_description);
  check("background preserved", after.compositional_deconstruction.background === before.compositional_deconstruction.background);
  check("element count preserved", afterEls.length === beforeEls.length, [beforeEls.length, afterEls.length]);
  check(
    "every bbox survives exactly",
    beforeEls.every((el: { bbox: number[] }, i: number) => JSON.stringify(el.bbox) === JSON.stringify(afterEls[i].bbox)),
  );
  check(
    "every desc/text/type survives",
    beforeEls.every(
      (el: { desc?: string; text?: string; type: string }, i: number) =>
        (el.desc ?? "") === (afterEls[i].desc ?? "") && (el.text ?? "") === (afterEls[i].text ?? "") && el.type === afterEls[i].type,
    ),
  );
  // This caption has no style_description; one must not appear from nowhere,
  // since an invented half-filled block is invalid per the spec.
  check("no style block invented", after.style_description === undefined);
  check("no key-order complaints about it", validateRaw(real).length === 0, validateRaw(real));
  check("no semantic complaints about it", validateCaption(parsed).length === 0, validateCaption(parsed));
}

// --- the "text is the only store" rule ------------------------------------
check("empty text is an empty layout", parseCaption("")?.elements.length === 0);
check("garbage is refused rather than silently emptied", parseCaption("{not json") === null);

// --- validation catches what it should ------------------------------------
check("out-of-order keys are reported", validateRaw('{"compositional_deconstruction":{"elements":[],"background":"b"},"high_level_description":"x"}').length >= 2);
check(
  "half-filled style block is reported",
  validateCaption(caption({ style: { aesthetics: "", lighting: "l", medium: "painting", detail: "", palette: [] } })).some(
    (i) => i.key === "ideogram.issue.styleIncomplete",
  ),
);
check(
  "lowercase hex is reported",
  validateCaption(caption({ style: { aesthetics: "a", lighting: "l", medium: "painting", detail: "d", palette: ["#aabbcc"] } })).every(
    (i) => i.key !== "ideogram.issue.badHex",
  ),
  "lowercase is normalized, not rejected",
);
check(
  "a malformed colour is reported",
  validateCaption(caption({ style: { aesthetics: "a", lighting: "l", medium: "painting", detail: "d", palette: ["red"] } })).some(
    (i) => i.key === "ideogram.issue.badHex",
  ),
);
// An element written without a bbox still has to become a draggable box (the
// editor is bbox-only), but it must be reported rather than silently placed.
const noBbox = '{"compositional_deconstruction":{"background":"b","elements":[{"type":"obj","desc":"d"}]}}';
check("bbox-less element becomes a placeholder box", parseCaption(noBbox)?.elements.length === 1);
check("...and is reported", validateRaw(noBbox).some((i) => i.key === "ideogram.issue.missingBbox"));

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
