import { useEffect, useState } from "react";
import { apiKeysApi, backendsApi, capabilitiesApi, nodeTemplatesApi } from "../api/endpoints";
import type { ApiKeyPermission, Backend, Capability, NodeTemplate } from "../types";
import { NodeTypeWizard } from "./NodeTypeWizard";

/** Delete buttons below have no other feedback mechanism -- without this, a
 * failed request (network error, FK conflict, wrong API base URL) just leaves
 * the row sitting there with zero indication that anything went wrong. */
async function tryDelete(action: () => Promise<unknown>, reload: () => void): Promise<void> {
  try {
    await action();
    reload();
  } catch (err) {
    alert(err instanceof Error ? err.message : "Delete failed.");
  }
}

function describeError(err: unknown): string {
  return err instanceof Error ? err.message : "Failed to load.";
}

/** The "fetch a list, clear/set the load error, stash the result" shape
 * repeated for every section below (backends/capabilities/templates/API
 * keys) -- factored out so there's one place to get the error handling
 * right instead of four copies that could drift. */
function makeReloader<T>(fetcher: () => Promise<T>, setItems: (v: T) => void, setError: (msg: string | null) => void): () => Promise<void> {
  return () =>
    fetcher()
      .then((v) => {
        setError(null);
        setItems(v);
      })
      .catch((err) => setError(describeError(err)));
}

function BackendsSection({ items, reload }: { items: Backend[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState<"comfyui" | "api_provider">("comfyui");

  const create = async () => {
    if (!name.trim()) return;
    await backendsApi.create({ name: name.trim(), kind, base_url: kind === "comfyui" ? baseUrl : null });
    setName("");
    setBaseUrl("");
    reload();
  };

  return (
    <div className="settings-section">
      <h2>Backends</h2>
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Kind</th>
            <th>URL</th>
            <th>Active</th>
            <th>Last heartbeat</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((b) => (
            <tr key={b.id}>
              <td>{b.name}</td>
              <td>{b.kind}</td>
              <td>{b.base_url}</td>
              <td>
                <input
                  type="checkbox"
                  checked={b.is_active}
                  onChange={async (e) => {
                    await backendsApi.update(b.id, { is_active: e.target.checked });
                    reload();
                  }}
                />
              </td>
              <td>{b.last_heartbeat_at ?? "never"}</td>
              <td>
                <button onClick={() => tryDelete(() => backendsApi.remove(b.id), reload)}>delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="inline-form">
        <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
          <option value="comfyui">comfyui</option>
          <option value="api_provider">api_provider</option>
        </select>
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
        {kind === "comfyui" && <input placeholder="http://host:8188" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: 220 }} />}
        <button className="primary" onClick={create}>
          + Add backend
        </button>
      </div>
    </div>
  );
}

function NodeTypeCard({
  template,
  backends,
  capabilities,
  wizardOpen,
  onOpenWizard,
  onCancelWizard,
  onSaved,
  reloadCapabilities,
  reloadTemplates,
}: {
  template: NodeTemplate;
  backends: Backend[];
  capabilities: Capability[];
  wizardOpen: boolean;
  onOpenWizard: () => void;
  onCancelWizard: () => void;
  onSaved: () => void;
  reloadCapabilities: () => void;
  reloadTemplates: () => void;
}) {
  const backendName = (id: string) => backends.find((b) => b.id === id)?.name ?? id;

  return (
    <div className="settings-section node-type-card">
      <div className="node-cell-header">
        <span>{template.name}</span>
        <span className="status-pill">{template.node_type_slug}</span>
      </div>
      <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
        {(template.param_schema.fields ?? []).map((f) => `${f.name}: ${f.type}`).join(", ") || "(no fields)"}
      </div>

      <table>
        <thead>
          <tr>
            <th>Backend</th>
            <th>Execution</th>
            <th>Enabled</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {capabilities.map((c) => (
            <tr key={c.id}>
              <td>{backendName(c.backend_id)}</td>
              <td>{c.execution_type}</td>
              <td>
                <input
                  type="checkbox"
                  checked={c.enabled}
                  onChange={async (e) => {
                    await capabilitiesApi.update(c.id, { enabled: e.target.checked });
                    reloadCapabilities();
                  }}
                />
              </td>
              <td>
                <button onClick={() => tryDelete(() => capabilitiesApi.remove(c.id), reloadCapabilities)}>delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {wizardOpen ? (
        <NodeTypeWizard
          backends={backends}
          mode={{ kind: "add-instance", template, excludeBackendIds: capabilities.map((c) => c.backend_id) }}
          onCancel={onCancelWizard}
          onSaved={onSaved}
        />
      ) : (
        <div className="node-actions">
          <button className="primary" onClick={onOpenWizard}>
            + Add ComfyUI instance
          </button>
          <button
            onClick={() => tryDelete(() => nodeTemplatesApi.remove(template.id), reloadTemplates)}
            disabled={capabilities.length > 0}
            title={capabilities.length > 0 ? "Remove all instances before deleting this node type" : "Delete this node type"}
          >
            delete node type
          </button>
        </div>
      )}
    </div>
  );
}

function NodeTypesSection({
  templates,
  backends,
  capabilities,
  reloadTemplates,
  reloadCapabilities,
}: {
  templates: NodeTemplate[];
  backends: Backend[];
  capabilities: Capability[];
  reloadTemplates: () => void;
  reloadCapabilities: () => void;
}) {
  const [wizard, setWizard] = useState<{ kind: "create" } | { kind: "add-instance"; templateId: string } | null>(null);

  const closeWizard = () => setWizard(null);
  const saved = () => {
    reloadTemplates();
    reloadCapabilities();
    setWizard(null);
  };

  // GET /api/node-templates merges real DB rows with a synthesized entry
  // per code-registry native type (node_templates.py's _native_template_read
  // -- see memory/native_execution_type.md), so this list isn't DB rows
  // only. A native.* entry has no Capability concept (it runs in-process,
  // never via a ComfyUI backend) and no DB row to delete -- "+ Add ComfyUI
  // instance" is meaningless for one, and "delete node type" 404s instead
  // of doing anything (db.get on its synthesized uuid5 id finds nothing,
  // and it just reappears next reload anyway, regenerated straight from the
  // registry). This tab is for the DB-backed template types those two
  // actions actually apply to.
  const editableTemplates = templates.filter((t) => !t.node_type.startsWith("native."));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <h2 style={{ margin: 0 }}>Node types</h2>
      {editableTemplates.map((t) => (
        <NodeTypeCard
          key={t.id}
          template={t}
          backends={backends}
          capabilities={capabilities.filter((c) => c.node_type_slug === t.node_type_slug)}
          wizardOpen={wizard?.kind === "add-instance" && wizard.templateId === t.id}
          onOpenWizard={() => setWizard({ kind: "add-instance", templateId: t.id })}
          onCancelWizard={closeWizard}
          onSaved={saved}
          reloadCapabilities={reloadCapabilities}
          reloadTemplates={reloadTemplates}
        />
      ))}

      {wizard?.kind === "create" ? (
        <NodeTypeWizard backends={backends} mode={{ kind: "create" }} onCancel={closeWizard} onSaved={saved} />
      ) : (
        <button className="primary" style={{ alignSelf: "flex-start" }} onClick={() => setWizard({ kind: "create" })}>
          + New node type
        </button>
      )}
    </div>
  );
}

function ApiKeysSection() {
  const [items, setItems] = useState<ApiKeyPermission[]>([]);
  const [provider, setProvider] = useState("nano_banana");
  const [nodeTypeSlug, setNodeTypeSlug] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  const reload = makeReloader(apiKeysApi.list, setItems, setLoadError);
  useEffect(() => {
    reload();
  }, []);

  const create = async () => {
    if (!nodeTypeSlug.trim() || !apiKey.trim()) return;
    await apiKeysApi.create({ provider, node_type_slug: nodeTypeSlug.trim(), api_key: apiKey.trim() });
    setNodeTypeSlug("");
    setApiKey("");
    reload();
  };

  return (
    <div className="settings-section">
      <h2>API provider permissions</h2>
      <p style={{ fontSize: 12, color: "var(--text-dim)" }}>
        Grants a paid provider's key to a specific node type (SPEC §8 open question — permission is per provider + node type).
      </p>
      {loadError && <div className="error-text">{loadError}</div>}
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Node type</th>
            <th>Enabled</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((k) => (
            <tr key={k.id}>
              <td>{k.provider}</td>
              <td>{k.node_type_slug}</td>
              <td>{k.enabled ? "yes" : "no"}</td>
              <td>
                <button onClick={() => tryDelete(() => apiKeysApi.remove(k.id), reload)}>delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="inline-form">
        <input placeholder="provider (e.g. nano_banana)" value={provider} onChange={(e) => setProvider(e.target.value)} />
        <input placeholder="node_type_slug" value={nodeTypeSlug} onChange={(e) => setNodeTypeSlug(e.target.value)} />
        <input placeholder="API key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} style={{ width: 220 }} />
        <button className="primary" onClick={create}>
          + Grant
        </button>
      </div>
    </div>
  );
}

export function Settings() {
  const [backends, setBackends] = useState<Backend[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [templates, setTemplates] = useState<NodeTemplate[]>([]);
  // Without a .catch, a failed load (bad token, unreachable backend, ...)
  // just leaves these lists empty with zero indication anything went wrong
  // -- indistinguishable from "nothing configured yet".
  const [loadError, setLoadError] = useState<string | null>(null);

  const reloadBackends = makeReloader(backendsApi.list, setBackends, setLoadError);
  const reloadCapabilities = makeReloader(capabilitiesApi.list, setCapabilities, setLoadError);
  const reloadTemplates = makeReloader(nodeTemplatesApi.list, setTemplates, setLoadError);

  useEffect(() => {
    reloadBackends();
    reloadCapabilities();
    reloadTemplates();
  }, []);

  return (
    <div className="settings-panel">
      {loadError && <div className="error-text">{loadError}</div>}
      <BackendsSection items={backends} reload={reloadBackends} />
      <NodeTypesSection
        templates={templates}
        backends={backends}
        capabilities={capabilities}
        reloadTemplates={reloadTemplates}
        reloadCapabilities={reloadCapabilities}
      />
      <ApiKeysSection />
    </div>
  );
}
