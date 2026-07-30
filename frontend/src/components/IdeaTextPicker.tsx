import { useEffect, useState } from "react";
import { boardApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { IdeaText } from "../types";
import { cx } from "../utils";

/** "From ideas": pulls a text sticker's words into this node's prompt field.
 *
 * Two insert modes, and the difference between them is the whole design
 * (roadmap.md §1, bridge 2):
 *
 * - plain text (the default) copies the words in and freezes them. Editing the
 *   sticker afterwards changes nothing. Manual, no surprises.
 * - `{tag}` inserts a reference resolved at run time. Worth having when one
 *   description feeds a dozen nodes, but it IS the automatic behaviour the
 *   default avoids -- so it only ever appears alongside the resolved preview
 *   below, which shows exactly what will run.
 *
 * The preview is resolved by the server, by the same function the worker calls,
 * so what it shows and what generates cannot drift apart. Markdown stripping
 * happens there too (text_plain), for the same reason.
 */

interface Props {
  projectId: string;
  value: string;
  onInsert: (next: string) => void;
  onClose: () => void;
}

export function IdeaTextPicker({ projectId, value, onInsert, onClose }: Props) {
  const t = useT();
  const [texts, setTexts] = useState<IdeaText[]>([]);
  const [picked, setPicked] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    boardApi
      .ideaTexts(projectId)
      .then(setTexts)
      .finally(() => setLoading(false));
  }, [projectId]);

  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  // `item`, not `t`: `t` is the translate function in this scope now, and a
  // shadowing lambda parameter reads as a bug even where it compiles.
  const chosen = picked.map((id) => texts.find((item) => item.item_id === id)).filter((item): item is IdeaText => !!item);
  const appendTo = (addition: string) => `${value.trim() ? `${value.trim()}, ` : ""}${addition}`;

  const insertPlain = () => {
    onInsert(appendTo(chosen.map((item) => item.text_plain).join(", ")));
    onClose();
  };

  const insertMacros = () => {
    onInsert(appendTo(chosen.map((item) => `{${item.tag}}`).join(" ")));
    onClose();
  };

  const allTagged = chosen.length > 0 && chosen.every((item) => item.tag);

  return (
    <div className="idea-picker" onClick={(e) => e.stopPropagation()}>
      <div className="idea-picker-head">
        <strong>{t("ideas.title")}</strong>
        <button onClick={onClose} title={t("common.close")}>
          ×
        </button>
      </div>

      {loading ? (
        <p style={{ color: "var(--text-dim)" }}>{t("common.loading")}</p>
      ) : texts.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>{t("ideas.empty")}</p>
      ) : (
        <div className="idea-picker-list">
          {texts.map((item) => (
            <label key={item.item_id} className={cx("idea-picker-row", picked.includes(item.item_id) && "picked")}>
              <input type="checkbox" checked={picked.includes(item.item_id)} onChange={() => toggle(item.item_id)} />
              <span className="idea-picker-text">
                {item.tag && <code>{`{${item.tag}}`}</code>}
                {item.text_plain || <em style={{ color: "var(--text-dim)" }}>{t("ideas.emptyText")}</em>}
              </span>
            </label>
          ))}
        </div>
      )}

      <div className="idea-picker-actions">
        <button onClick={insertPlain} disabled={chosen.length === 0} title={t("ideas.insertTextTitle")}>
          {t("ideas.insertText")}
        </button>
        <button
          onClick={insertMacros}
          disabled={!allTagged}
          title={allTagged ? t("ideas.insertMacroTitle") : t("ideas.insertMacroDisabledTitle")}
        >
          {t("ideas.insertMacro")}
        </button>
      </div>
    </div>
  );
}

/** Shows what a field containing `{tag}` macros will actually look like when it
 * runs, resolved server-side. A macro must never be able to hide what runs --
 * that guardrail is the reason macros are offered at all. */
export function MacroPreview({ projectId, text }: { projectId: string; text: string }) {
  const t = useT();
  const [state, setState] = useState<{ resolved: string; unresolved: string[] } | null>(null);

  useEffect(() => {
    if (!/\{[A-Za-z0-9_-]+\}/.test(text)) {
      setState(null);
      return;
    }
    let cancelled = false;
    // Debounced: this fires while typing a prompt.
    const timer = window.setTimeout(() => {
      boardApi
        .resolveMacros(projectId, text)
        .then((r) => !cancelled && setState(r))
        .catch(() => undefined);
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [projectId, text]);

  if (!state) return null;
  return (
    <div className="macro-preview">
      <span className="macro-preview-label">{t("ideas.previewLabel")}</span>
      <span>{state.resolved}</span>
      {state.unresolved.length > 0 && (
        <span className="macro-preview-missing">
          {t("ideas.missingTags", { tags: state.unresolved.map((tag) => `{${tag}}`).join(", ") })}
        </span>
      )}
    </div>
  );
}
