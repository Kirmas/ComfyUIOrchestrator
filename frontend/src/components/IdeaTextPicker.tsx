import { useEffect, useState } from "react";
import { boardApi } from "../api/endpoints";
import type { IdeaText } from "../types";
import { cx } from "../utils";

/** "Взяти з ідей": pulls a text sticker's words into this node's prompt field.
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

  const chosen = picked.map((id) => texts.find((t) => t.item_id === id)).filter((t): t is IdeaText => !!t);
  const appendTo = (addition: string) => `${value.trim() ? `${value.trim()}, ` : ""}${addition}`;

  const insertPlain = () => {
    onInsert(appendTo(chosen.map((t) => t.text_plain).join(", ")));
    onClose();
  };

  const insertMacros = () => {
    onInsert(appendTo(chosen.map((t) => `{${t.tag}}`).join(" ")));
    onClose();
  };

  const allTagged = chosen.length > 0 && chosen.every((t) => t.tag);

  return (
    <div className="idea-picker" onClick={(e) => e.stopPropagation()}>
      <div className="idea-picker-head">
        <strong>З ідей</strong>
        <button onClick={onClose} title="Close">
          ×
        </button>
      </div>

      {loading ? (
        <p style={{ color: "var(--text-dim)" }}>Завантаження…</p>
      ) : texts.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>На дошці ідей ще немає текстових стікерів.</p>
      ) : (
        <div className="idea-picker-list">
          {texts.map((t) => (
            <label key={t.item_id} className={cx("idea-picker-row", picked.includes(t.item_id) && "picked")}>
              <input type="checkbox" checked={picked.includes(t.item_id)} onChange={() => toggle(t.item_id)} />
              <span className="idea-picker-text">
                {t.tag && <code>{`{${t.tag}}`}</code>}
                {t.text_plain || <em style={{ color: "var(--text-dim)" }}>(порожньо)</em>}
              </span>
            </label>
          ))}
        </div>
      )}

      <div className="idea-picker-actions">
        <button onClick={insertPlain} disabled={chosen.length === 0} title="Вставити текст — він застигне, зміни стікера на нього не вплинуть">
          вставити текст
        </button>
        <button
          onClick={insertMacros}
          disabled={!allTagged}
          title={
            allTagged
              ? "Вставити посилання {tag} — розкриється під час запуску, тобто правки стікера вплинуть на наступну генерацію"
              : "Тільки для стікерів із тегом"
          }
        >
          вставити як {"{tag}"}
        </button>
      </div>
    </div>
  );
}

/** Shows what a field containing `{tag}` macros will actually look like when it
 * runs, resolved server-side. A macro must never be able to hide what runs --
 * that guardrail is the reason macros are offered at all. */
export function MacroPreview({ projectId, text }: { projectId: string; text: string }) {
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
      <span className="macro-preview-label">що піде в генерацію:</span>
      <span>{state.resolved}</span>
      {state.unresolved.length > 0 && (
        <span className="macro-preview-missing">
          немає стікера з тегом: {state.unresolved.map((t) => `{${t}}`).join(", ")} — залишиться як є
        </span>
      )}
    </div>
  );
}
