import { useState, useEffect } from "react";
import { nodeTemplatesApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { NodeTemplate } from "../types";

export function NodeTypePromptInstruction({
  template,
  onChanged,
}: {
  template: NodeTemplate;
  onChanged: () => void;
}) {
  const t = useT();
  const current = template.prompt_instruction ?? "";
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(current);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    setDraft(template.prompt_instruction ?? "");
  }, [template.prompt_instruction]);

  const save = async () => {
    setBusy(true);
    try {
      await nodeTemplatesApi.setPromptInstruction(template.node_type_slug, draft.trim() || null);
      setEditing(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    try {
      await nodeTemplatesApi.setPromptInstruction(template.node_type_slug, null);
      setEditing(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="node-type-prompt-instruction"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 6,
        padding: "6px 8px",
        borderRadius: 6,
        background: "var(--bg)",
        border: "1px solid var(--border)",
        fontSize: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontWeight: 600, color: "var(--text-dim)", fontSize: 11, textTransform: "uppercase" }}>
          {t("promptInstruction.title")}
        </span>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          {!editing && (
            <>
              {current && (
                <button
                  style={{ fontSize: 10, padding: "1px 6px" }}
                  onClick={() => setExpanded(!expanded)}
                >
                  {expanded ? t("promptInstruction.collapse") : t("promptInstruction.expand")}
                </button>
              )}
              <button
                style={{ fontSize: 10, padding: "1px 6px" }}
                onClick={() => {
                  setDraft(current);
                  setEditing(true);
                  setExpanded(true);
                }}
              >
                {current ? t("common.edit") : t("promptInstruction.add")}
              </button>
              {current && (
                <button
                  style={{ fontSize: 10, padding: "1px 6px", color: "var(--danger)" }}
                  onClick={clear}
                  disabled={busy}
                >
                  {t("common.clearLower")}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {editing ? (
        <>
          <textarea
            autoFocus
            rows={12}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t("promptInstruction.placeholder")}
            style={{
              width: "100%",
              fontFamily: "monospace",
              fontSize: 11,
              lineHeight: 1.4,
              resize: "vertical",
            }}
          />
          <div style={{ display: "flex", gap: 6 }}>
            <button className="primary" onClick={save} disabled={busy} style={{ fontSize: 11 }}>
              {busy ? t("common.saving") : t("common.save")}
            </button>
            <button
              onClick={() => {
                setDraft(current);
                setEditing(false);
              }}
              disabled={busy}
              style={{ fontSize: 11 }}
            >
              {t("common.cancel")}
            </button>
          </div>
        </>
      ) : current ? (
        <div
          style={{
            maxHeight: expanded ? "none" : 80,
            overflow: "hidden",
            whiteSpace: "pre-wrap",
            fontFamily: "monospace",
            fontSize: 11,
            color: "var(--text)",
            lineHeight: 1.4,
            position: "relative",
          }}
        >
          {current}
          {!expanded && (
            <div
              style={{
                position: "absolute",
                bottom: 0,
                left: 0,
                right: 0,
                height: 30,
                background: "linear-gradient(transparent, var(--bg))",
                pointerEvents: "none",
              }}
            />
          )}
        </div>
      ) : (
        <span style={{ color: "var(--text-dim)", fontStyle: "italic", fontSize: 11 }}>
          {t("promptInstruction.none")}
        </span>
      )}
    </div>
  );
}
