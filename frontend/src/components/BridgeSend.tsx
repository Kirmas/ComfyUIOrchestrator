import { useEffect, useState } from "react";
import { bridgeApi } from "../api/endpoints";
import { useT } from "../i18n";
import type { Asset, BridgeClient, BridgeDevice, BridgePairRequest } from "../types";

const APP_LABEL: Record<BridgeClient["app"], string> = { unreal: "Unreal", blender: "Blender" };

function clientLabel(c: BridgeClient): string {
  return `${APP_LABEL[c.app] ?? c.app} — ${c.name}`;
}

/** "Send to Unreal / Blender" in the full-size view. Lists only the apps that
 * are connected right now: there is deliberately no queue (core/bridge.py),
 * so a closed app is simply not offered, and a send that loses it midway
 * comes back as an error. */
export function BridgeSendButton({ asset }: { asset: Asset }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [clients, setClients] = useState<BridgeClient[] | null>(null);
  const [status, setStatus] = useState<{ text: string; error?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    setStatus(null);
    setClients(null);
    bridgeApi
      .clients()
      .then(setClients)
      .catch((e) => {
        setClients([]);
        setStatus({ text: e instanceof Error ? e.message : String(e), error: true });
      });
  };

  const send = async (c: BridgeClient) => {
    const name = clientLabel(c);
    setBusy(true);
    setStatus({ text: t("bridge.sending", { name }) });
    try {
      const r = await bridgeApi.send(asset.id, c.id);
      if (r.pending) setStatus({ text: t("bridge.pending", { name }) });
      else setStatus({ text: `${name}: ${r.message}`, error: !r.ok });
    } catch (e) {
      setStatus({ text: t("bridge.failed", { name, error: e instanceof Error ? e.message : String(e) }), error: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bridge-send" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="bridge-send-toggle" onClick={toggle} title={t("bridge.send")}>
        ⇪
      </button>
      {open && (
        <div className="bridge-send-menu">
          <div className="bridge-send-title">{t("bridge.send")}</div>
          {clients === null && <div className="bridge-send-note">{t("bridge.loading")}</div>}
          {clients !== null && clients.length === 0 && !status && <div className="bridge-send-note">{t("bridge.none")}</div>}
          {clients?.map((c) => (
            <button key={c.id} type="button" disabled={busy} onClick={() => send(c)}>
              {clientLabel(c)}
            </button>
          ))}
          {status && <div className={status.error ? "bridge-send-note error-text" : "bridge-send-note"}>{status.text}</div>}
        </div>
      )}
    </div>
  );
}

/** Settings: the plugins, pairing approvals, and the paired apps. Polls while
 * open, so a request made in Unreal/Blender shows up here within seconds. */
export function BridgeSection() {
  const t = useT();
  const [requests, setRequests] = useState<BridgePairRequest[]>([]);
  const [devices, setDevices] = useState<BridgeDevice[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    Promise.all([bridgeApi.pairing(), bridgeApi.devices()])
      .then(([r, d]) => {
        setRequests(r);
        setDevices(d);
      })
      .catch(() => undefined);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, []);

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
      load();
    }
  };

  const now = Date.now();
  return (
    <div className="settings-section">
      <h2>{t("bridge.title")}</h2>
      <p style={{ fontSize: 12, color: "var(--text-dim)", marginTop: 0 }}>{t("bridge.hint")}</p>
      <div className="inline-form">
        <a className="button-link" href={bridgeApi.pluginUrl("blender")} download>
          ⬇ {t("bridge.downloadBlender")}
        </a>
        <a className="button-link" href={bridgeApi.pluginUrl("unreal")} download>
          ⬇ {t("bridge.downloadUnreal")}
        </a>
      </div>

      {requests.length > 0 && (
        <div className="bridge-pair-requests">
          <h3>{t("bridge.pairRequests")}</h3>
          {requests.map((r) => (
            <div key={r.id} className="bridge-row">
              <span>
                <b>{APP_LABEL[r.app] ?? r.app}</b> — {r.name}{" "}
                <span style={{ color: "var(--text-dim)" }}>({r.remote})</span>
              </span>
              <span className="bridge-code">{t("bridge.pairCode", { code: r.code })}</span>
              <span className="bridge-row-actions">
                <button className="primary" disabled={busy !== null} onClick={() => act(r.id, () => bridgeApi.approve(r.id))}>
                  {t("bridge.approve")}
                </button>
                <button disabled={busy !== null} onClick={() => act(r.id, () => bridgeApi.deny(r.id))}>
                  {t("bridge.deny")}
                </button>
              </span>
            </div>
          ))}
        </div>
      )}

      <h3>{t("bridge.devices")}</h3>
      {devices && devices.length > 0 ? (
        devices.map((d) => {
          const expired = new Date(d.expires_at).getTime() <= now;
          return (
            <div key={d.id} className="bridge-row">
              <span>
                <span className={d.online ? "bridge-dot online" : "bridge-dot"} title={t(d.online ? "bridge.online" : "bridge.offline")} />
                <b>{APP_LABEL[d.app] ?? d.app}</b> — {d.name}
              </span>
              <span style={{ fontSize: 12, color: expired ? "var(--danger, #e66)" : "var(--text-dim)" }}>
                {expired ? t("bridge.expired") : t("bridge.validUntil", { date: new Date(d.expires_at).toLocaleDateString() })}
              </span>
              <span className="bridge-row-actions">
                <button
                  disabled={busy !== null}
                  onClick={() => {
                    if (confirm(t("bridge.revokeConfirm", { name: d.name }))) void act(d.id, () => bridgeApi.revoke(d.id));
                  }}
                >
                  {t("bridge.revoke")}
                </button>
              </span>
            </div>
          );
        })
      ) : (
        <div style={{ fontSize: 13, color: "var(--text-dim)" }}>{t("bridge.devicesNone")}</div>
      )}
      {error && <div className="error-text">{error}</div>}
    </div>
  );
}
