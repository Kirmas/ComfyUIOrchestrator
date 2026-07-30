import { useState } from "react";
import { nodeTemplatesApi } from "../api/endpoints";
import { useT, type TKey } from "../i18n";
import type { NodeTemplate } from "../types";

const SOURCE_HINT: Record<string, TKey> = {
  auto: "desc.sourceAuto",
  manual: "desc.sourceManual",
  agent: "desc.sourceAgent",
};

export function NodeTypeDescription({ template, onChanged }: { template: NodeTemplate; onChanged: () => void }) {
  const t = useT();
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
            placeholder={t("desc.placeholder")}
            style={{ width: "100%" }}
          />
          <div className="description-actions">
            <button className="primary" onClick={save} disabled={busy}>
              {busy ? t("common.saving") : t("common.save")}
            </button>
            <button onClick={() => { setDraft(template.description ?? ""); setEditing(false); }} disabled={busy}>
              {t("common.cancel")}
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="description-text">{template.description || t("desc.none")}</div>
          <div className="description-actions">
            <span className="status-pill" title={SOURCE_HINT[source] ? t(SOURCE_HINT[source]) : undefined}>
              {source}
            </span>
            <button onClick={() => { setDraft(template.description ?? ""); setEditing(true); }}>{t("common.edit")}</button>
            {source !== "auto" && (
              <button onClick={resetToAuto} disabled={busy} title={t("desc.resetTitle")}>
                {t("desc.resetToAuto")}
              </button>
            )}
            {fingerprint.length > 0 && (
              <details className="description-facts">
                <summary>{t("desc.facts")}</summary>
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
