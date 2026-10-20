import { useEffect, useRef, useState } from "react";
import { capabilitiesApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Capability, CapabilityModels, ModelFileInfo } from "../types";

const KEY_LABELS: Record<string, string> = { unet_name: "UNET", ckpt_name: "Checkpoint", clip_name: "CLIP", vae_name: "VAE" };

const label = (m: ModelFileInfo) => [m.name, m.size, m.quant].filter(Boolean).join(" · ");

/** A swap is legal only inside one family + variant (size and quantization
 * are free) -- same rule as backend/app/core/model_overrides.py, which
 * enforces it for real; this only keeps the dropdown to legal choices. */
const compatible = (a: ModelFileInfo, b: ModelFileInfo) => a.family === b.family && a.variant === b.variant;

/** For one (PC, workflow): which model files the workflow names, and what to
 * run instead on this PC. Every change saves immediately. */
export function ModelOverridesModal({
  capability,
  onClose,
  onSaved,
}: {
  capability: Capability;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [models, setModels] = useState<CapabilityModels | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>(
    (capability.config.model_overrides as Record<string, string> | undefined) ?? {},
  );
  const mouseDownOnBackdrop = useRef(false);

  useEffect(() => {
    capabilitiesApi
      .models(capability.id)
      .then(setModels)
      .catch(() => setError(t("models.unreachable")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capability.id]);

  const set = async (source: string, target: string) => {
    const next = { ...overrides };
    if (target) next[source] = target;
    else delete next[source];
    setError(null);
    try {
      await capabilitiesApi.setModelOverrides(capability.id, next);
      setOverrides(next);
      onSaved();
    } catch (e) {
      setError(String((e as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? e));
    }
  };

  // Not portaled, unlike NodeCell's modals: this one lives in Settings, which has
  // no pan/zoom transform. (.image-modal-content is a light-background image
  // frame -- the dark text-on-panel look is .params-modal-content.)
  return (
    <div
      className="image-modal-backdrop"
      onMouseDown={(e) => {
        mouseDownOnBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={() => {
        if (mouseDownOnBackdrop.current) onClose();
      }}
    >
      <div
        className="params-modal-content"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 760, height: "auto" }}
      >
        <div className="node-cell-header">
          <span>{t("models.title")}</span>
          <button className="image-modal-close" onClick={onClose} title={t("common.close")}>
            ×
          </button>
        </div>
        <div className="node-cell-hint">{t("models.hint")}</div>
        {!models && !error && <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{t("common.loading")}</div>}
        {models?.used.length === 0 && <div style={{ fontSize: 12 }}>{t("models.empty")}</div>}
        {models && (
          <table style={{ marginTop: 8 }}>
            <tbody>
              {models.used.map((src) => {
                const options = (models.installed[src.key] ?? []).filter((m) => compatible(src, m) && m.name !== src.name);
                const current = overrides[src.name] ?? "";
                return (
                  <tr key={`${src.key}:${src.name}`}>
                    <td>
                      {KEY_LABELS[src.key] ?? src.key}: {label(src)}
                    </td>
                    <td>{t("models.use")}</td>
                    <td>
                      <select value={current} onChange={(e) => set(src.name, e.target.value)}>
                        <option value="">{t("models.none")}</option>
                        {current && !options.some((o) => o.name === current) && <option value={current}>{current}</option>}
                        {options.map((o) => (
                          <option key={o.name} value={o.name}>
                            {label(o)}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {error && <div className="error-text">{error}</div>}
      </div>
    </div>
  );
}
