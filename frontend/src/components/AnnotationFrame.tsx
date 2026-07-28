import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import type { Annotation } from "../types";

interface Props {
  annotation: Annotation;
  // Bounding box in grid coordinates, derived by the caller from where the
  // member nodes currently sit -- nothing positional is stored on the
  // annotation itself.
  box: { minRow: number; maxRow: number; minCol: number; maxCol: number };
  onSave: (text: string) => Promise<void>;
  onDelete: () => Promise<void>;
}

export function AnnotationFrame({ annotation, box, onSave, onDelete }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(annotation.text);
  const [saving, setSaving] = useState(false);

  useEffect(() => setDraft(annotation.text), [annotation.text]);

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div
        // A grid item spanning its members' rows/columns, not an absolutely
        // positioned overlay: the grid itself then keeps the frame aligned at
        // any zoom level, with no coordinate math to drift.
        className={`annotation-frame annotation-${annotation.source}`}
        style={{
          gridRow: `${box.minRow + 1} / span ${box.maxRow - box.minRow + 1}`,
          gridColumn: `${box.minCol + 2} / span ${box.maxCol - box.minCol + 1}`,
        }}
        onDoubleClick={() => setEditing(true)}
        title={annotation.text || "Double-click to add a comment"}
      >
        {/* The frame body must not eat clicks meant for the cells inside it
            (pointer-events: none in CSS); only this label is interactive. */}
        <div className="annotation-label">
          <span className="annotation-text">{annotation.text || "(empty comment)"}</span>
          <span className="annotation-actions">
            <button onClick={() => setEditing(true)} title="Edit this comment">
              edit
            </button>
            <button onClick={onDelete} title="Delete this comment block">
              ×
            </button>
          </span>
        </div>
      </div>

      {editing &&
        // Portaled to the body: this sits inside the grid's zoom transform,
        // and any ancestor transform makes position: fixed resolve against
        // that ancestor instead of the viewport, which would open the dialog
        // far off-screen.
        createPortal(
          <div className="image-modal-backdrop" onClick={() => !saving && setEditing(false)}>
            <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
              <h3>Comment</h3>
              <textarea
                autoFocus
                rows={5}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="Note for this group of cells"
                style={{ width: "100%" }}
              />
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 8 }}>
                <button onClick={() => setEditing(false)} disabled={saving}>
                  Cancel
                </button>
                <button onClick={save} disabled={saving}>
                  {saving ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
