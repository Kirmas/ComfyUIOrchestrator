import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useT } from "../i18n";
import type { Annotation, AnnotationMessage } from "../types";
import { cx } from "../utils";

/** Where a thread stands -- the one thing its frame on the grid and this
 * dialog both show:
 * - "resolved": done; nothing is waiting on anyone;
 * - "agent": an agent spoke last, so it's the person's turn to look;
 * - "user": the person spoke last -- a note, or something for the agent. */
export type ThreadState = "resolved" | "agent" | "user";

export function threadState(annotation: Annotation): ThreadState {
  if (annotation.resolved) return "resolved";
  return annotation.messages[annotation.messages.length - 1]?.source ?? "user";
}

interface Props {
  // null while writing the first message of a thread that doesn't exist yet.
  annotation: Annotation | null;
  onSend: (text: string) => Promise<void>;
  onSetResolved: (resolved: boolean) => Promise<void>;
  onEditMessage: (message: AnnotationMessage, text: string) => Promise<void>;
  onRemoveMessage: (message: AnnotationMessage) => Promise<void>;
  onRemove: () => Promise<void>;
  onClose: () => void;
}

/** A comment thread as a small chat: the person and an agent working through
 * MCP write in the same one, each under their own name. Nothing here puts
 * words in the other side's mouth -- only your own messages can be edited;
 * anyone's can be deleted, since tidying up is the person's call. */
export function AnnotationThread({ annotation, onSend, onSetResolved, onEditMessage, onRemoveMessage, onRemove, onClose }: Props) {
  const t = useT();
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const messages = annotation?.messages ?? [];

  // Newest at the bottom, as in any chat -- including a message an agent
  // writes while this is open (it arrives over the project websocket).
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const send = () => {
    const text = draft.trim();
    if (!text || busy) return;
    void run(async () => {
      await onSend(text);
      setDraft("");
    });
  };

  const saveEdit = (message: AnnotationMessage) => {
    const text = editing?.text.trim();
    if (!text || busy) return;
    void run(async () => {
      if (text !== message.text) await onEditMessage(message, text);
      setEditing(null);
    });
  };

  const author = (source: AnnotationMessage["source"]) => (source === "agent" ? t("annotation.agent") : t("annotation.you"));

  return createPortal(
    // Portaled to the body: the frame this opens from sits inside the grid's
    // pan/zoom transform, and any ancestor transform makes position: fixed
    // resolve against that ancestor instead of the viewport, which would open
    // the dialog far off-screen.
    <div className="image-modal-backdrop" onClick={() => !busy && onClose()}>
      <div className="params-modal-content annotation-thread" onClick={(e) => e.stopPropagation()}>
        <div className="annotation-thread-head">
          <h3>{t("annotation.title")}</h3>
          {annotation?.resolved && (
            <span className="annotation-thread-status">
              {annotation.resolved_by === "agent" ? t("annotation.resolvedByAgent") : t("annotation.resolvedByYou")}
            </span>
          )}
        </div>

        <div className="annotation-thread-messages" ref={listRef}>
          {messages.length === 0 && <p className="annotation-thread-empty">{t("annotation.noMessages")}</p>}
          {messages.map((m) => (
            <div key={m.id} className={cx("annotation-message", `from-${m.source}`)}>
              <div className="annotation-message-meta">
                <span className="annotation-message-author">{author(m.source)}</span>
                <span title={m.updated_at !== m.created_at ? t("annotation.editedAt", { when: formatWhen(m.updated_at) }) : undefined}>
                  {formatWhen(m.created_at)}
                  {m.updated_at !== m.created_at && ` · ${t("annotation.edited")}`}
                </span>
                <span className="annotation-message-actions">
                  {m.source === "user" && editing?.id !== m.id && (
                    <button onClick={() => setEditing({ id: m.id, text: m.text })} disabled={busy} title={t("annotation.editTitle")}>
                      {t("common.edit")}
                    </button>
                  )}
                  <button onClick={() => void run(() => onRemoveMessage(m))} disabled={busy} title={t("annotation.deleteMessageTitle")}>
                    ×
                  </button>
                </span>
              </div>
              {editing?.id === m.id ? (
                <div className="annotation-message-edit">
                  <textarea
                    autoFocus
                    rows={3}
                    value={editing.text}
                    onChange={(e) => setEditing({ id: m.id, text: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) saveEdit(m);
                      if (e.key === "Escape") setEditing(null);
                    }}
                  />
                  <div className="annotation-thread-buttons">
                    <button onClick={() => setEditing(null)} disabled={busy}>
                      {t("common.cancel")}
                    </button>
                    <button onClick={() => saveEdit(m)} disabled={busy || !editing.text.trim()}>
                      {t("common.save")}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="annotation-message-text">{m.text}</div>
              )}
            </div>
          ))}
        </div>

        <textarea
          className="annotation-thread-draft"
          autoFocus
          rows={3}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
          }}
          placeholder={annotation ? t("annotation.replyPlaceholder") : t("annotation.placeholder")}
        />

        <div className="annotation-thread-buttons">
          {annotation && (
            <button
              className="annotation-thread-delete"
              onClick={() => {
                if (window.confirm(t("annotation.deleteThreadConfirm"))) void run(onRemove);
              }}
              disabled={busy}
              title={t("annotation.deleteTitle")}
            >
              {t("annotation.deleteThread")}
            </button>
          )}
          <span style={{ flex: 1 }} />
          <button onClick={onClose} disabled={busy}>
            {t("common.close")}
          </button>
          {annotation &&
            (annotation.resolved ? (
              <button onClick={() => void run(() => onSetResolved(false))} disabled={busy} title={t("annotation.reopenTitle")}>
                {t("annotation.reopen")}
              </button>
            ) : (
              <button onClick={() => void run(() => onSetResolved(true))} disabled={busy} title={t("annotation.resolveTitle")}>
                {t("annotation.resolve")}
              </button>
            ))}
          <button className="primary" onClick={send} disabled={busy || !draft.trim()} title={t("annotation.sendTitle")}>
            {t("annotation.send")}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Time of day for today's messages, the date too for anything older -- a
 * thread can run for days, and "14:02" alone would be ambiguous. */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === new Date().toDateString() ? time : `${d.toLocaleDateString()} ${time}`;
}
