import { useState } from "react";
import { nodeTemplatesApi } from "../api/endpoints";
import type { NodeTemplate } from "../types";

const SOURCE_HINT: Record<string, string> = {
  auto: "Derived from this node type's workflows. Updates by itself when they change.",
  manual: "Written by hand. It stays as-is until reset.",
  agent: "Written by an agent. Replaced by the auto text once the workflows change.",
};

export function NodeTypeDescription({ template, onChanged }: { template: NodeTemplate; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(template.description ?? "");
  const [busy, setBusy] = useState(false);
  const source = template.description_source ?? "auto";
  const fingerprint = Object.entries(template.fingerprint ?? {});

  const save = async () => {
    setBusy(true);
    try {
      await nodeTemplatesApi.setDescription(template.node_type_slug, draft);
      setEditing(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const resetToAuto = async () => {
    setBusy(true);
    try {
      await nodeTemplatesApi.resetDescription(template.node_type_slug);
      setEditing(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="node-type-description">
      {editing ? (
        <>
          <textarea
            autoFocus
            rows={3}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="What does this node type do?"
            style={{ width: "100%" }}
          />
          <div className="description-actions">
            <button className="primary" onClick={save} disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button onClick={() => { setDraft(template.description ?? ""); setEditing(false); }} disabled={busy}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="description-text">{template.description || "(no description)"}</div>
          <div className="description-actions">
            <span className="status-pill" title={SOURCE_HINT[source]}>
              {source}
            </span>
            <button onClick={() => { setDraft(template.description ?? ""); setEditing(true); }}>edit</button>
            {source !== "auto" && (
              <button onClick={resetToAuto} disabled={busy} title="Go back to the description derived from the workflows">
                reset to auto
              </button>
            )}
            {fingerprint.length > 0 && (
              <details className="description-facts">
                <summary>facts</summary>
                <ul>
                  {fingerprint.map(([key, value]) => (
                    <li key={key}>
                      <b>{key}:</b> {value}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </>
      )}
    </div>
  );
}
