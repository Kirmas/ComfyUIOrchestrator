import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { assetsApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Asset } from "../types";

/** Rename an asset. "Container name" fills the field with the server's
 * suggestion ("<dashboard> N", numbered unique in that dashboard) for the
 * person to keep or edit -- nothing is saved until Save. Portaled to the body
 * for the same reason as every modal opened from a grid cell (CLAUDE.md). */
export function AssetNameDialog({ asset, onClose, onSaved }: { asset: Asset; onClose: () => void; onSaved: () => void }) {
  const t = useT();
  const [name, setName] = useState(asset.name ?? "");
  const [suggestion, setSuggestion] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    assetsApi
      .containerName(asset.id)
      .then((r) => !cancelled && setSuggestion(r.name))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [asset.id]);

  const save = async () => {
    setBusy(true);
    try {
      await assetsApi.rename(asset.id, name);
      onSaved();
      onClose();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div className="image-modal-backdrop" onClick={() => !busy && onClose()}>
      <div className="params-modal-content" style={{ maxWidth: 420 }} onClick={(e) => e.stopPropagation()}>
        <h3 style={{ marginTop: 0 }}>{t("cell.renameAssetTitle")}</h3>
        <input
          autoFocus
          type="text"
          value={name}
          placeholder={t("cell.renameAssetPlaceholder")}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void save();
            if (e.key === "Escape") onClose();
          }}
          style={{ width: "100%", boxSizing: "border-box" }}
        />
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          {suggestion && (
            <button type="button" onClick={() => setName(suggestion)} disabled={busy} title={suggestion}>
              {t("cell.containerNameButton")}
            </button>
          )}
          <span style={{ flex: 1 }} />
          <button type="button" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </button>
          <button type="button" className="primary" onClick={() => void save()} disabled={busy}>
            {t("common.save")}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
