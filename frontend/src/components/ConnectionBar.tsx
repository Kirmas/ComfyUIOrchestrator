import { useState } from "react";
import { ApiError, getApiBaseUrl, getApiToken, setApiBaseUrl, setApiToken } from "../api/client";
import { projectsApi } from "../api/endpoints";
import { useT } from "../i18n";

export function ConnectionBar({ forceOpen = false }: { forceOpen?: boolean } = {}) {
  const t = useT();
  const [baseUrl, setBaseUrlState] = useState(getApiBaseUrl());
  const [token, setTokenState] = useState(getApiToken());
  // forceOpen is for the pre-auth gate in App.tsx: a *saved* token that
  // turns out to be wrong still needs the form, not just the collapsed
  // "Connection" button that !getApiToken() alone would show.
  const [open, setOpen] = useState(forceOpen || !getApiToken());
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A wrong token (or base URL) must not look like success: closing this
  // panel and reloading unconditionally, as this used to do, left every
  // other screen just showing empty lists with no error (ProjectPicker
  // swallows load failures, Settings.tsx's loaders have no .catch at all)
  // -- indistinguishable from "you have no projects yet". Probing with a
  // real authenticated request here, and rolling back on failure, means a
  // bad token surfaces immediately instead of as a silent empty app.
  const save = async () => {
    setChecking(true);
    setError(null);
    const prevBaseUrl = getApiBaseUrl();
    const prevToken = getApiToken();
    setApiBaseUrl(baseUrl);
    setApiToken(token);
    try {
      await projectsApi.list();
      location.reload();
    } catch (err) {
      setApiBaseUrl(prevBaseUrl);
      setApiToken(prevToken);
      setError(
        err instanceof ApiError && err.status === 401
          ? t("conn.invalidToken")
          : err instanceof Error
            ? err.message
            : t("conn.unreachable"),
      );
      setChecking(false);
    }
  };

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} title={baseUrl || t("conn.sameOrigin")}>
        {t("conn.button")}
      </button>
    );
  }

  return (
    <div className="inline-form" style={{ flexWrap: "wrap" }}>
      <input
        placeholder={t("conn.baseUrlPlaceholder")}
        value={baseUrl}
        onChange={(e) => setBaseUrlState(e.target.value)}
        style={{ width: 260 }}
      />
      <input
        placeholder={t("conn.tokenPlaceholder")}
        type="password"
        value={token}
        onChange={(e) => setTokenState(e.target.value)}
        style={{ width: 180 }}
      />
      <button className="primary" onClick={save} disabled={checking}>
        {checking ? t("conn.checking") : t("conn.saveAndReload")}
      </button>
      {error && <span className="error-text">{error}</span>}
    </div>
  );
}
