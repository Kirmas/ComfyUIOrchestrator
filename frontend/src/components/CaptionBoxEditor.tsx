import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useT } from "../i18n";
import {
  boxArea,
  boxContains,
  clampBox,
  moveBox,
  normalizeBox,
  resizeBox,
  type Box,
  type ResizeMode,
} from "../boxGeometry";
import {
  composeCaption,
  parseCaption,
  validateCaption,
  validateRaw,
  usesPhotoKey,
  EMPTY_CAPTION,
  EMPTY_STYLE,
  MAX_ELEMENT_COLORS,
  MAX_STYLE_COLORS,
  type Caption,
  type CaptionElement,
  type ValidationIssue,
} from "../ideogramCaption";
import { AESTHETIC_CHIPS, MEDIA, hasChip, lightingChipsFor, styleDetailChipsFor, toggleChip } from "../ideogramVocab";
import { parseAspectRatio } from "../ideogram4";

export interface CaptionBgOption {
  id: string;
  url: string;
  label: string;
}

/** Smallest box a drag can create, as a fraction of the canvas -- below this
 * a drag is treated as a click on empty space (deselect) rather than a
 * one-pixel element nobody can grab again. */
const MIN_BOX = 0.02;
const CORNERS: ResizeMode[] = ["nw", "ne", "sw", "se"];

/** Visual editor for an Ideogram 4 JSON caption: draw the composition as
 * boxes over a grey canvas (or over the node's own last output, once there is
 * one), fill each box's description, and the assembled caption goes back into
 * the node's prompt field.
 *
 * The prompt text is the only store -- this opens by parsing it and closes by
 * re-composing it, with no structured copy kept on the side. Clearing the text
 * really does clear the boxes, and a caption written by hand (or by an agent)
 * opens here as boxes with no import step.
 *
 * Rendered through a portal to document.body: a params modal opened from a
 * grid cell sits inside Grid's pan/zoom transform, which would otherwise
 * re-anchor its position:fixed backdrop (see NodeCell.tsx). The backdrop keeps
 * the `.image-modal-backdrop` class for the same reason -- that's the selector
 * Grid's onBackgroundPointerDown excludes, without which every pointerdown in
 * here would also pan the grid underneath. */
export function CaptionBoxEditor({
  value,
  aspectHint,
  bgOptions,
  onCommit,
  onClose,
}: {
  value: string;
  /** A FluxResolutionNode-style aspect string from the node's own params, used
   * for the canvas shape before any output exists to measure. */
  aspectHint?: unknown;
  bgOptions: CaptionBgOption[];
  onCommit: (next: string) => void;
  onClose: () => void;
}) {
  const t = useT();
  const parsed = useMemo(() => parseCaption(value), [value]);
  const [caption, setCaption] = useState<Caption>(() => parsed ?? { ...EMPTY_CAPTION, style: { ...EMPTY_STYLE }, elements: [] });
  const [unparsable, setUnparsable] = useState(parsed === null);
  // Key-order / malformed-bbox problems in the text as it was written. Shown
  // once on open and gone after a save, since what this editor writes is
  // canonical by construction.
  const rawIssues = useMemo(() => validateRaw(value), [value]);
  const issues = validateCaption(caption);

  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState<Box | null>(null);
  const draftRef = useRef<Box | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  const [bgId, setBgId] = useState<string | null>(bgOptions[0]?.id ?? null);
  const [bgOpacity, setBgOpacity] = useState(60);
  const bg = bgOptions.find((o) => o.id === bgId) ?? null;

  // Canvas shape: measured from the background once there is one, otherwise
  // taken from the node's aspect_ratio param, otherwise square. Editable
  // either way -- it's local view state, never part of the caption.
  const [ratio, setRatio] = useState<{ w: number; h: number }>(() => {
    const hint = parseAspectRatio(aspectHint);
    return hint ? { w: Math.round(hint * 1000), h: 1000 } : { w: 1, h: 1 };
  });

  // The canvas is sized in pixels rather than left to CSS `aspect-ratio`:
  // with a definite height, aspect-ratio derives the width and `max-width`
  // then clamps it without giving the height back, which silently squashes a
  // wide canvas. Fitting the ratio into the measured area by hand is exact,
  // and the pointer math reads the same box either way.
  const fitRef = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    const host = fitRef.current;
    if (!host) return;
    const measure = () => {
      const { width, height } = host.getBoundingClientRect();
      if (width <= 0 || height <= 0) return;
      const scale = Math.min(width / ratio.w, height / ratio.h);
      setFit({ width: ratio.w * scale, height: ratio.h * scale });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, [ratio.w, ratio.h, unparsable]);

  const [mediumMode, setMediumMode] = useState<"none" | "preset" | "custom">(() => {
    if (!caption.style.medium.trim()) return "none";
    return MEDIA.some((m) => m.value === caption.style.medium) ? "preset" : "custom";
  });

  const dirty = composeCaption(caption) !== value.trim();

  const updateStyle = (patch: Partial<Caption["style"]>) => setCaption((c) => ({ ...c, style: { ...c.style, ...patch } }));
  const updateElement = (index: number, patch: Partial<CaptionElement>) =>
    setCaption((c) => ({ ...c, elements: c.elements.map((el, i) => (i === index ? { ...el, ...patch } : el)) }));
  const removeElement = (index: number) => {
    setCaption((c) => ({ ...c, elements: c.elements.filter((_, i) => i !== index) }));
    setSelected((s) => (s === null ? null : s === index ? null : s > index ? s - 1 : s));
  };

  const closeGuarded = () => {
    if (dirty && !confirm(t("ideogram.confirmDiscard"))) return;
    onClose();
  };

  // Delete/Backspace removes the selected box, but only when the focus isn't
  // in a field -- otherwise it would eat every backspace typed into a desc.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (document.activeElement?.tagName ?? "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if ((e.key === "Delete" || e.key === "Backspace") && selected !== null) {
        e.preventDefault();
        removeElement(selected);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const pointAt = (e: { clientX: number; clientY: number }): { x: number; y: number } => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
    return { x: (e.clientX - rect.left) / rect.width, y: (e.clientY - rect.top) / rect.height };
  };

  /** Fraction-per-pixel on each axis. Unlike CropPreview (which works in
   * source-image pixels, uniformly scaled on both axes) the two differ here,
   * because the canvas is measured in fractions of its own width and height. */
  const scales = () => {
    const rect = canvasRef.current?.getBoundingClientRect();
    return { sx: rect && rect.width ? 1 / rect.width : 0, sy: rect && rect.height ? 1 / rect.height : 0 };
  };

  const startCreate = (e: React.PointerEvent) => {
    e.preventDefault();
    const origin = pointAt(e);
    draftRef.current = { x: origin.x, y: origin.y, width: 0, height: 0 };
    setDraft(draftRef.current);

    const onMove = (ev: PointerEvent) => {
      const p = pointAt(ev);
      const next = clampBox(normalizeBox({ x: origin.x, y: origin.y, width: p.x - origin.x, height: p.y - origin.y }), 1, 1, 0);
      draftRef.current = next;
      setDraft(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      const box = draftRef.current;
      draftRef.current = null;
      setDraft(null);
      if (box && boxArea(box) >= MIN_BOX * MIN_BOX) {
        const fitted = clampBox(box, 1, 1, MIN_BOX);
        setCaption((c) => ({
          ...c,
          elements: [...c.elements, { ...fitted, type: "obj", text: "", desc: "", palette: [] }],
        }));
        setSelected(caption.elements.length);
      } else {
        setSelected(null);
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const startBoxDrag = (index: number, mode: ResizeMode | "move") => (e: React.PointerEvent) => {
    // Ctrl/Cmd forces a brand-new box even when the drag starts on top of an
    // existing one -- otherwise a box covering most of the canvas would make
    // the area under it undrawable.
    if (e.ctrlKey || e.metaKey) {
      e.stopPropagation();
      startCreate(e);
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    if (e.altKey) {
      cycleAt(pointAt(e), index);
      return;
    }
    setSelected(index);
    const start = caption.elements[index];
    const startX = e.clientX;
    const startY = e.clientY;
    const { sx, sy } = scales();

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      const dx = (ev.clientX - startX) * sx;
      const dy = (ev.clientY - startY) * sy;
      const moved = mode === "move" ? moveBox(start, dx, dy) : normalizeBox(resizeBox(start, mode, dx, dy));
      updateElement(index, clampBox(moved, 1, 1, MIN_BOX));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
    // Window-level for the same reason as CropPreview: a fast drag leaves the
    // handle (or the canvas) long before the gesture ends.
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  /** Alt-click steps through every box under the pointer, so a small element
   * buried under a big one is still reachable. */
  const cycleAt = (p: { x: number; y: number }, clicked: number) => {
    const under = caption.elements.map((el, i) => ({ el, i })).filter(({ el }) => boxContains(el, p.x, p.y));
    if (under.length === 0) return;
    const current = under.findIndex(({ i }) => i === (selected ?? clicked));
    setSelected(under[(current + 1) % under.length].i);
  };

  const pct = (v: number) => `${v * 100}%`;
  const selectedEl = selected !== null ? caption.elements[selected] : null;

  const issueText = (issue: ValidationIssue) => t(issue.key, issue.params);

  const chipRow = (chips: string[], value: string, onChange: (next: string) => void) => (
    <div className="caption-chips">
      {chips.map((chip) => (
        <button
          key={chip}
          type="button"
          className={hasChip(value, chip) ? "caption-chip active" : "caption-chip"}
          onClick={() => onChange(toggleChip(value, chip))}
        >
          {chip}
        </button>
      ))}
    </div>
  );

  return createPortal(
    <div className="image-modal-backdrop" onClick={closeGuarded}>
      <div className="params-modal-content caption-editor" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="image-modal-close" onClick={closeGuarded} title={t("common.close")}>
          ×
        </button>
        <div className="node-cell-header">
          <span>{t("ideogram.title")}</span>
        </div>

        {unparsable ? (
          <div className="caption-unparsable">
            <p>{t("ideogram.unparsable")}</p>
            <button
              type="button"
              className="primary"
              onClick={() => {
                setCaption({ ...EMPTY_CAPTION, style: { ...EMPTY_STYLE }, elements: [] });
                setMediumMode("none");
                setUnparsable(false);
              }}
            >
              {t("ideogram.startEmpty")}
            </button>
          </div>
        ) : (
          <div className="caption-editor-body">
            <div className="caption-canvas-col">
              <div className="caption-toolbar">
                <label>
                  {t("ideogram.bg")}
                  <select value={bgId ?? ""} onChange={(e) => setBgId(e.target.value || null)} disabled={bgOptions.length === 0}>
                    <option value="">{bgOptions.length === 0 ? t("ideogram.noOutputs") : t("ideogram.bgNone")}</option>
                    {bgOptions.map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </label>
                {bg && (
                  <label className="caption-opacity">
                    {t("ideogram.bgOpacity")}
                    <input type="range" min={0} max={100} value={bgOpacity} onChange={(e) => setBgOpacity(Number(e.target.value))} />
                  </label>
                )}
                <label className="caption-ratio">
                  {t("ideogram.ratio")}
                  <input
                    type="number"
                    min={1}
                    value={ratio.w}
                    onChange={(e) => setRatio((r) => ({ ...r, w: Math.max(1, Number(e.target.value) || 1) }))}
                  />
                  <span>:</span>
                  <input
                    type="number"
                    min={1}
                    value={ratio.h}
                    onChange={(e) => setRatio((r) => ({ ...r, h: Math.max(1, Number(e.target.value) || 1) }))}
                  />
                </label>
              </div>

              <div ref={fitRef} className="caption-canvas-fit">
                <div
                  ref={canvasRef}
                  className="caption-canvas"
                  style={fit ? { width: fit.width, height: fit.height } : { visibility: "hidden" }}
                  onPointerDown={startCreate}
                >
                  {bg && (
                    <img
                      src={bg.url}
                      alt=""
                      className="caption-canvas-bg"
                      style={{ opacity: bgOpacity / 100 }}
                      draggable={false}
                      onLoad={(e) => setRatio({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
                    />
                  )}
                  {caption.elements.length === 0 && !draft && <div className="caption-canvas-hint">{t("ideogram.canvasHint")}</div>}
                  {caption.elements.map((el, i) => (
                    <div
                      key={i}
                      className={`caption-box${i === selected ? " selected" : ""}${el.type === "text" ? " is-text" : ""}`}
                      style={{ left: pct(el.x), top: pct(el.y), width: pct(el.width), height: pct(el.height), zIndex: i === selected ? 20 : 10 }}
                      onPointerDown={startBoxDrag(i, "move")}
                      title={el.desc || el.text}
                    >
                      <span className="caption-box-num">{i + 1}</span>
                      <span className="caption-box-label">{el.type === "text" ? el.text || el.desc : el.desc}</span>
                      {i === selected &&
                        CORNERS.map((corner) => (
                          <div
                            key={corner}
                            className={`caption-handle ${corner}`}
                            style={{ cursor: `${corner}-resize` }}
                            onPointerDown={startBoxDrag(i, corner)}
                          />
                        ))}
                    </div>
                  ))}
                  {draft && (
                    <div
                      className="caption-box draft"
                      style={{ left: pct(draft.x), top: pct(draft.y), width: pct(draft.width), height: pct(draft.height) }}
                    />
                  )}
                </div>
              </div>
              <div className="caption-canvas-help">{t("ideogram.canvasHelp")}</div>
            </div>

            <div className="caption-side">
              <label className="caption-field">
                <span>{t("ideogram.highLevel")}</span>
                <textarea rows={3} value={caption.highLevel} onChange={(e) => setCaption((c) => ({ ...c, highLevel: e.target.value }))} />
              </label>
              <label className="caption-field">
                <span>{t("ideogram.background")}</span>
                <textarea rows={3} value={caption.background} onChange={(e) => setCaption((c) => ({ ...c, background: e.target.value }))} />
              </label>

              <div className="caption-group">
                <div className="caption-group-title">{t("ideogram.style")}</div>
                <label className="caption-field">
                  <span>{t("ideogram.medium")}</span>
                  <select
                    value={mediumMode === "custom" ? "__custom__" : caption.style.medium}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v === "__custom__") {
                        setMediumMode("custom");
                        updateStyle({ medium: "" });
                      } else {
                        setMediumMode(v ? "preset" : "none");
                        updateStyle({ medium: v });
                      }
                    }}
                  >
                    <option value="">{t("ideogram.mediumNone")}</option>
                    {MEDIA.map((m) => (
                      <option key={m.value} value={m.value}>
                        {t(m.labelKey)} — {m.value}
                      </option>
                    ))}
                    <option value="__custom__">{t("ideogram.mediumCustom")}</option>
                  </select>
                </label>
                {mediumMode === "custom" && (
                  <input
                    type="text"
                    className="caption-custom-medium"
                    value={caption.style.medium}
                    placeholder={t("ideogram.mediumCustomPlaceholder")}
                    onChange={(e) => updateStyle({ medium: e.target.value })}
                  />
                )}

                {!caption.style.medium.trim() ? (
                  <div className="caption-hint">{t("ideogram.styleOffHint")}</div>
                ) : (
                  <>
                    <label className="caption-field">
                      <span>{t("ideogram.aesthetics")}</span>
                      <input type="text" value={caption.style.aesthetics} onChange={(e) => updateStyle({ aesthetics: e.target.value })} />
                    </label>
                    {chipRow(AESTHETIC_CHIPS, caption.style.aesthetics, (next) => updateStyle({ aesthetics: next }))}

                    <label className="caption-field">
                      <span>{t("ideogram.lighting")}</span>
                      <input type="text" value={caption.style.lighting} onChange={(e) => updateStyle({ lighting: e.target.value })} />
                    </label>
                    {chipRow(lightingChipsFor(caption.style.medium), caption.style.lighting, (next) => updateStyle({ lighting: next }))}

                    <label className="caption-field">
                      <span>{usesPhotoKey(caption.style.medium) ? t("ideogram.detailPhoto") : t("ideogram.detailArt")}</span>
                      <input type="text" value={caption.style.detail} onChange={(e) => updateStyle({ detail: e.target.value })} />
                    </label>
                    {chipRow(styleDetailChipsFor(caption.style.medium), caption.style.detail, (next) => updateStyle({ detail: next }))}

                    <PaletteEditor
                      label={t("ideogram.stylePalette")}
                      colors={caption.style.palette}
                      max={MAX_STYLE_COLORS}
                      onChange={(palette) => updateStyle({ palette })}
                    />
                  </>
                )}
              </div>

              <div className="caption-group">
                <div className="caption-group-title">
                  {t("ideogram.elements")} ({caption.elements.length})
                </div>
                <div className="caption-element-list">
                  {caption.elements.map((el, i) => (
                    <div
                      key={i}
                      className={`caption-element-row${i === selected ? " selected" : ""}`}
                      onClick={() => setSelected(i)}
                    >
                      <span className="caption-element-num">{i + 1}</span>
                      <span className={`caption-element-type ${el.type}`}>{el.type}</span>
                      <span className="caption-element-desc">{el.type === "text" ? el.text || el.desc : el.desc || "—"}</span>
                      <button
                        type="button"
                        className="caption-element-del"
                        title={t("common.delete")}
                        onClick={(e) => {
                          e.stopPropagation();
                          removeElement(i);
                        }}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>

                {selectedEl && selected !== null && (
                  <div className="caption-element-editor">
                    <div className="caption-type-switch">
                      {(["obj", "text"] as const).map((type) => (
                        <label key={type} className={selectedEl.type === type ? "sel" : ""}>
                          <input
                            type="radio"
                            name="caption-el-type"
                            checked={selectedEl.type === type}
                            onChange={() => updateElement(selected, { type })}
                          />
                          {type === "obj" ? t("ideogram.typeObj") : t("ideogram.typeText")}
                        </label>
                      ))}
                    </div>
                    {selectedEl.type === "text" && (
                      <label className="caption-field">
                        <span>{t("ideogram.text")}</span>
                        <textarea rows={2} value={selectedEl.text} onChange={(e) => updateElement(selected, { text: e.target.value })} />
                      </label>
                    )}
                    <label className="caption-field">
                      <span>{t("ideogram.desc")}</span>
                      <textarea rows={5} value={selectedEl.desc} onChange={(e) => updateElement(selected, { desc: e.target.value })} />
                    </label>
                    <PaletteEditor
                      label={t("ideogram.elementPalette")}
                      colors={selectedEl.palette}
                      max={MAX_ELEMENT_COLORS}
                      onChange={(palette) => updateElement(selected, { palette })}
                    />
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        <div className="caption-footer">
          <div className="caption-issues">
            {rawIssues.length > 0 && (
              <div className="caption-issue raw">
                {t("ideogram.importIssues")} {rawIssues.map(issueText).join("; ")}
              </div>
            )}
            {issues.map((issue, i) => (
              <div key={i} className="caption-issue">
                {issueText(issue)}
              </div>
            ))}
          </div>
          <div className="caption-actions">
            <button type="button" onClick={closeGuarded}>
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => {
                onCommit(composeCaption(caption));
                onClose();
              }}
            >
              {t("common.save")}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

function PaletteEditor({
  label,
  colors,
  max,
  onChange,
}: {
  label: string;
  colors: string[];
  max: number;
  onChange: (colors: string[]) => void;
}) {
  const t = useT();
  return (
    <div className="caption-palette">
      <span className="caption-palette-label">
        {label} ({colors.length}/{max})
      </span>
      <div className="caption-palette-swatches">
        {colors.map((color, i) => (
          <button
            key={`${color}-${i}`}
            type="button"
            className="caption-swatch"
            style={{ background: color }}
            title={`${color} — ${t("common.remove")}`}
            onClick={() => onChange(colors.filter((_, j) => j !== i))}
          />
        ))}
        {colors.length < max && (
          <input
            type="color"
            className="caption-swatch-add"
            title={t("ideogram.addColor")}
            // Committing on change (not input) keeps a single entry per pick
            // instead of one per pixel dragged around the OS colour wheel.
            onChange={(e) => onChange([...colors, e.target.value.toUpperCase()])}
          />
        )}
      </div>
    </div>
  );
}
