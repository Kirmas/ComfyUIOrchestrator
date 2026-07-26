import { useState } from "react";
import {
  AZIMUTHS,
  ELEVATIONS,
  DISTANCES,
  composeAnglePrompt,
  parseAngleSelection,
  upsertAnglePhrase,
  type AngleSelection,
} from "../multiAngleLora";

/** Structured constructor for the Multiple-Angles LoRA's prompt grammar
 *  (`<sks> <azimuth> <elevation> <distance>`). Composes the exact token string
 *  from a few controls and writes it into the surrounding prompt field via
 *  `onChange`. Rendered only when the field's capability actually loads that
 *  LoRA (see capabilityUsesMultiAngleLora). Collapsed by default so it doesn't
 *  crowd the prompt editor. */
export function MultiAngleBuilder({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  const [open, setOpen] = useState(false);
  // Pre-fill from whatever the field already holds (e.g. `<sks> back view`), so
  // the controls reflect the current prompt rather than a generic default.
  // Falls back to a neutral default only when there's no angle phrase to parse.
  const [sel, setSel] = useState<AngleSelection>(
    () =>
      parseAngleSelection(value) ?? {
        trigger: true,
        azimuth: AZIMUTHS[0].value,
        elevation: ELEVATIONS[1].value, // eye-level -- the neutral default
        distance: DISTANCES[1].value, // medium shot
      },
  );
  const phrase = composeAnglePrompt(sel);

  return (
    <div className="angle-builder">
      <button type="button" className="angle-builder-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? "▾" : "▸"} Multiple-Angles builder
      </button>
      {open && (
        <div className="angle-builder-body">
          <label className="angle-trigger">
            <input
              type="checkbox"
              checked={sel.trigger}
              onChange={(e) => setSel((s) => ({ ...s, trigger: e.target.checked }))}
            />
            <code>&lt;sks&gt;</code> trigger
          </label>

          <div className="angle-azimuths">
            {AZIMUTHS.map((a) => (
              <label key={a.value} className={sel.azimuth === a.value ? "sel" : ""}>
                <input
                  type="radio"
                  name="angle-azimuth"
                  checked={sel.azimuth === a.value}
                  onChange={() => setSel((s) => ({ ...s, azimuth: a.value }))}
                />
                {a.label}
              </label>
            ))}
          </div>

          <div className="angle-selects">
            {/* value="" == omit this slot from the composed phrase (composeAnglePrompt
                filters out empties); the regex treats both as optional too. */}
            <select value={sel.elevation} onChange={(e) => setSel((s) => ({ ...s, elevation: e.target.value }))}>
              <option value="">— no elevation —</option>
              {ELEVATIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <select value={sel.distance} onChange={(e) => setSel((s) => ({ ...s, distance: e.target.value }))}>
              <option value="">— no distance —</option>
              {DISTANCES.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <code className="angle-preview">{phrase}</code>

          <div className="angle-actions">
            <button
              type="button"
              onClick={() => onChange(upsertAnglePhrase(value, phrase))}
              title="Insert this phrase — replaces an existing angle phrase in the text, otherwise appends it"
            >
              Insert
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
