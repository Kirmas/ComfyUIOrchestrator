import { useEffect, useRef, useState } from "react";
import { backendsApi, capabilitiesApi, nodeTemplatesApi } from "../api/endpoints";
import type { Backend, Capability, DetectedField, NodeTemplate } from "../types";
import { NodeTypeWizard } from "./NodeTypeWizard";

// Only one provider is actually wired up backend-side right now
// (GeminiImageBackend, api_backend.py's PROVIDERS registry) -- a friendly
// label here instead of a free-text box, with room to grow as more get
// implemented.
const KNOWN_PROVIDERS = [{ value: "nano_banana", label: "Google Gemini (“nano banana”)" }];

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

/** A paid provider's key lives on the api_provider Backend row itself -- one
 * key per Backend, shared by every node type whose Capability points at it
 * (see db/models.py's Backend.provider/api_key docstring). Want a second key
 * (a second Gemini account, say)? Add a second api_provider backend, don't
 * grant the same key twice. */
function BackendsSection({ items, reload }: { items: Backend[]; reload: () => void }) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState<"comfyui" | "api_provider">("comfyui");
  const [provider, setProvider] = useState(KNOWN_PROVIDERS[0].value);
  const [apiKey, setApiKey] = useState("");
  const [dailyLimit, setDailyLimit] = useState("");

  const create = async () => {
    if (!name.trim()) return;
    if (kind === "api_provider" && !apiKey.trim()) return;
    await backendsApi.create({
      name: name.trim(),
      kind,
      base_url: kind === "comfyui" ? baseUrl : null,
      provider: kind === "api_provider" ? provider : null,
      api_key: kind === "api_provider" ? apiKey.trim() : null,
      daily_limit: kind === "api_provider" && dailyLimit.trim() ? Number(dailyLimit) : null,
    });
    setName("");
    setBaseUrl("");
    setApiKey("");
    setDailyLimit("");
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
            <th>URL / provider</th>
            <th>Active</th>
            <th>Daily limit</th>
            <th>Used (24h)</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((b) => (
            <tr key={b.id}>
              <td>{b.name}</td>
              <td>{b.kind}</td>
              <td>
                {b.kind === "api_provider" ? (
                  <>
                    {KNOWN_PROVIDERS.find((p) => p.value === b.provider)?.label ?? b.provider ?? "(no provider)"}
                    {" — "}
                    {b.has_api_key ? "key set" : <span className="error-text">no key</span>}
                  </>
                ) : (
                  b.base_url
                )}
              </td>
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
              <td>
                {b.kind === "api_provider" && (
                  <input
                    type="number"
                    min={0}
                    style={{ width: 70 }}
                    placeholder="∞"
                    defaultValue={b.daily_limit ?? ""}
                    onBlur={async (e) => {
                      const v = e.target.value.trim();
                      await backendsApi.update(b.id, { daily_limit: v ? Number(v) : null });
                      reload();
                    }}
                  />
                )}
              </td>
              <td style={{ color: b.daily_limit != null && b.used_today >= b.daily_limit ? "var(--danger)" : undefined }}>
                {b.kind === "api_provider" ? `${b.used_today}${b.daily_limit != null ? ` / ${b.daily_limit}` : ""}` : ""}
              </td>
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
        {kind === "comfyui" ? (
          <input placeholder="http://host:8188" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} style={{ width: 220 }} />
        ) : (
          <>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              {KNOWN_PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
            <input placeholder="API key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} style={{ width: 220 }} />
            <input placeholder="daily limit (blank = ∞)" style={{ width: 130 }} value={dailyLimit} onChange={(e) => setDailyLimit(e.target.value)} />
          </>
        )}
        <button className="primary" onClick={create}>
          + Add backend
        </button>
      </div>
    </div>
  );
}

/** Edits the literal prompt-shaped text values baked directly into a
 * capability's own workflow_json (CLIPTextEncode/TextEncodeQwenImageEditPlus
 * text, or a titled PrimitiveString(Multiline) value) that aren't already
 * exposed as a param_schema field -- see workflow_analyzer.py's
 * find_editable_text_fields. Until now the only way to change one of these
 * was re-uploading the whole workflow.json for this capability. */
function CapabilityTextFieldsModal({ capability, onClose }: { capability: Capability; onClose: () => void }) {
  const [fields, setFields] = useState<DetectedField[] | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Dragging a <textarea>'s native resize handle past the modal's edge lands
  // the mouseup (and the click it synthesizes) directly on the backdrop --
  // same element a real "click outside to close" click would target -- so a
  // plain onClick={onClose} on the backdrop closed the modal mid-resize.
  // Only treating it as "outside" when the *mousedown* also started on the
  // backdrop itself (not just where the pointer happened to end up) tells
  // the two apart.
  const mouseDownOnBackdrop = useRef(false);

  useEffect(() => {
    capabilitiesApi
      .textFields(capability.id)
      .then((fs) => {
        setFields(fs);
        setValues(Object.fromEntries(fs.map((f) => [f.key, String(f.default ?? "")])));
      })
      .catch((err) => setLoadError(describeError(err)));
  }, [capability.id]);

  const save = async (f: DetectedField) => {
    setSavingKey(f.key);
    setSavedKey(null);
    try {
      await capabilitiesApi.updateTextField(capability.id, { node_id: f.node_id, input_key: f.input_key, value: values[f.key] ?? "" });
      setSavedKey(f.key);
    } catch (err) {
      alert(describeError(err));
    } finally {
      setSavingKey(null);
    }
  };

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
        className="image-modal-content"
        onClick={(e) => e.stopPropagation()}
        style={{ width: 860, maxWidth: "92vw", maxHeight: "85vh", overflowY: "auto", display: "flex", flexDirection: "column" }}
      >
        <div className="node-cell-header">
          <span>Prompt fields</span>
          <button className="image-modal-close" onClick={onClose} title="Close">
            ×
          </button>
        </div>
        {loadError && <div className="error-text">{loadError}</div>}
        {fields === null && !loadError && <div style={{ fontSize: 12, color: "var(--text-dim)" }}>Loading…</div>}
        {fields?.length === 0 && (
          <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
            No baked-in text fields found — this workflow's prompts are all already exposed as regular params, or it has none.
          </div>
        )}
        {fields?.map((f) => (
          <div key={f.key} style={{ marginTop: 12 }}>
            <label style={{ display: "block", fontSize: 12, marginBottom: 4, color: "var(--text-dim)" }}>{f.label}</label>
            <textarea
              rows={14}
              style={{ width: "100%", minHeight: 240, resize: "vertical" }}
              value={values[f.key] ?? ""}
              onChange={(e) => {
                setValues((v) => ({ ...v, [f.key]: e.target.value }));
                setSavedKey(null);
              }}
            />
            <div style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 8 }}>
              <button disabled={savingKey === f.key} onClick={() => save(f)}>
                {savingKey === f.key ? "Saving…" : "Save"}
              </button>
              {savedKey === f.key && <span style={{ fontSize: 11, color: "var(--text-dim)" }}>saved</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const ASPECT_RATIOS = ["Auto", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "2:1"];
// generationConfig.imageConfig.imageSize -- reliably honored only by
// gemini-3-pro-image-preview as of 2026-07; the flash variants largely
// ignore it and stay ~1K regardless (see api_backend.py's docstring).
const IMAGE_SIZES = ["Auto", "1K", "2K", "4K"];

// Gemini's own image-generation model ids -- not guessable, so a picker
// instead of a free-text box. "Custom…" escapes to a text input for
// whatever ships next that isn't in this list yet.
const KNOWN_GEMINI_MODELS = [
  { value: "gemini-2.5-flash-image", label: "Gemini 2.5 Flash Image (“nano banana”)" },
  { value: "gemini-3.1-flash-image-preview", label: "Gemini 3.1 Flash Image Preview (“nano banana 2”)" },
  { value: "gemini-3-pro-image-preview", label: "Gemini 3 Pro Image Preview (“nano banana pro”)" },
];
const CUSTOM_MODEL_OPTION = "__custom__";

/** Attaches a paid api_call Capability to an EXISTING template, against a
 * backend already picked by NodeTypeCard's unified "+ Add instance" chooser
 * -- node types themselves are still only ever created from a ComfyUI
 * workflow.json (NodeTypeWizard); this is the "bonus" second backend a
 * template can additionally offer. aspect_ratio/image_size are always baked
 * on (Gemini-specific, nothing on a ComfyUI-derived template to map to --
 * see 2026-07-20 incident where reusing a same-named ComfyUI resolution
 * field silently sent it an invalid value). The prompt has two shapes: "master" bakes a
 * fixed default instruction into a new "prompt" field (e.g. a Back View
 * node's "rotate this character 180°..."); "match" instead points
 * param_mapping.prompt at an existing text field on the template (e.g. a
 * generic CreateImage node's own "Text String (User Prompt)" field detected
 * from its ComfyUI workflow) so the user's own typed prompt flows straight
 * through instead of being shadowed by a second baked field. Either way the
 * field ends up rendered by NodeCell's existing param form (⚙), editable
 * per-node afterward. */
function AddApiInstanceForm({
  template,
  backend,
  onSaved,
  onCancel,
}: {
  template: NodeTemplate;
  backend: Backend;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [modelId, setModelId] = useState(KNOWN_GEMINI_MODELS[0].value);
  const [customModel, setCustomModel] = useState(false);
  const [masterPrompt, setMasterPrompt] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fields = template.param_schema.fields ?? [];
  // Reserved, namespaced names for whatever this form injects -- a plain
  // "prompt"/"aspect_ratio" once silently collided with a ComfyUI-derived
  // field of the same name (a workflow's own "Resolution Selector" node had
  // already claimed "aspect_ratio" as a free-text field holding ComfyUI-
  // style values like "1:1 (Square)"; ASPECT_RATIOS' Gemini-shaped enum was
  // then never added, and the field silently sent that ComfyUI string as
  // Gemini's aspectRatio -- not a valid value, and no new control ever
  // appeared for the user to notice or fix). "api_"-prefixed names can't
  // collide with anything workflow_analyzer derives from a node title.
  const PROMPT_FIELD = "api_prompt";
  const ASPECT_FIELD = "api_aspect_ratio";
  const IMAGE_SIZE_FIELD = "api_image_size";
  const hasPromptField = fields.some((f) => f.name === PROMPT_FIELD);
  const hasAspectField = fields.some((f) => f.name === ASPECT_FIELD);
  const hasImageSizeField = fields.some((f) => f.name === IMAGE_SIZE_FIELD);
  // A ComfyUI-derived template can already have its own free-text field
  // (e.g. a "Text String (User Prompt)" PrimitiveString detected by
  // workflow_analyzer) -- for a genuinely free-prompt node type like
  // CreateImage, that field IS the user's prompt and should just be mapped
  // to, not shadowed by a second baked prompt field nobody asked for.
  // Only offered when such a field actually exists; otherwise there's
  // nothing to map to and this collapses back to the plain master-prompt case.
  const existingTextFields = fields.filter((f) => f.type === "text");
  const [promptMode, setPromptMode] = useState<"master" | "match">("master");
  const [matchedField, setMatchedField] = useState(existingTextFields[0]?.name ?? "");

  const save = async () => {
    if (promptMode === "master" && !masterPrompt.trim()) return;
    if (promptMode === "match" && !matchedField) return;
    setSaving(true);
    setError(null);
    try {
      const promptFieldName = promptMode === "match" ? matchedField : PROMPT_FIELD;
      const needsNewPromptField = promptMode === "master" && !hasPromptField;
      if (needsNewPromptField || !hasAspectField || !hasImageSizeField) {
        const newFields = [...fields];
        if (needsNewPromptField) newFields.push({ name: PROMPT_FIELD, type: "text", label: "Prompt" });
        if (!hasAspectField) {
          newFields.push({ name: ASPECT_FIELD, type: "enum", label: "Aspect ratio", options: ASPECT_RATIOS });
        }
        if (!hasImageSizeField) {
          newFields.push({ name: IMAGE_SIZE_FIELD, type: "enum", label: "Image size (Gemini)", options: IMAGE_SIZES });
        }
        await nodeTemplatesApi.update(template.id, {
          param_schema: { ...template.param_schema, fields: newFields },
          defaults: {
            ...template.defaults,
            ...(needsNewPromptField ? { [PROMPT_FIELD]: masterPrompt } : {}),
            ...(!hasAspectField ? { [ASPECT_FIELD]: "Auto" } : {}),
            ...(!hasImageSizeField ? { [IMAGE_SIZE_FIELD]: "Auto" } : {}),
          },
        });
      }
      await capabilitiesApi.create({
        backend_id: backend.id,
        node_type_slug: template.node_type_slug,
        execution_type: "api_call",
        config: {
          model_id: modelId,
          param_mapping: { prompt: promptFieldName, aspect_ratio: ASPECT_FIELD, image_size: IMAGE_SIZE_FIELD },
        },
      });
      onSaved();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="inline-form" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
      {error && <div className="error-text">{error}</div>}
      <div style={{ fontSize: 12, color: "var(--text-dim)" }}>
        {backend.name} ({KNOWN_PROVIDERS.find((p) => p.value === backend.provider)?.label ?? backend.provider})
      </div>
      {customModel ? (
        <input placeholder="model id" value={modelId} onChange={(e) => setModelId(e.target.value)} />
      ) : (
        <select
          value={modelId}
          onChange={(e) => {
            if (e.target.value === CUSTOM_MODEL_OPTION) {
              setCustomModel(true);
              setModelId("");
            } else {
              setModelId(e.target.value);
            }
          }}
        >
          {KNOWN_GEMINI_MODELS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
          <option value={CUSTOM_MODEL_OPTION}>Other (type manually)…</option>
        </select>
      )}
      {existingTextFields.length > 0 && (
        <select value={promptMode} onChange={(e) => setPromptMode(e.target.value as "master" | "match")}>
          <option value="master">Write a master prompt (baked default)</option>
          <option value="match">Map to an existing field (free user prompt)</option>
        </select>
      )}
      {promptMode === "match" && existingTextFields.length > 0 ? (
        <select value={matchedField} onChange={(e) => setMatchedField(e.target.value)}>
          {existingTextFields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label ?? f.name}
            </option>
          ))}
        </select>
      ) : (
        <textarea
          placeholder="Master prompt — the baked default instruction sent to the model (e.g. 'Generate the back view of this exact character…'). Editable per node afterward."
          rows={3}
          value={masterPrompt}
          onChange={(e) => setMasterPrompt(e.target.value)}
        />
      )}
      <div className="node-actions">
        <button
          className="primary"
          disabled={saving || (promptMode === "master" ? !masterPrompt.trim() : !matchedField)}
          onClick={save}
        >
          {saving ? "Saving…" : "Save API instance"}
        </button>
        <button onClick={onCancel}>Cancel</button>
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
  const [promptsFor, setPromptsFor] = useState<Capability | null>(null);
  // Single "+ Add instance" entry point: pick any not-yet-attached backend
  // first, then branch by its kind -- comfyui hands off to the existing
  // NodeTypeWizard (workflow upload + field mapping), api_provider to the
  // lighter AddApiInstanceForm (model + prompt). Two buttons asking the user
  // to already know which kind they want up front was the wrong shape --
  // the backend they pick already says that.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [apiBackendId, setApiBackendId] = useState<string | null>(null);
  const [comfyBackendId, setComfyBackendId] = useState<string | null>(null);
  const attachedBackendIds = capabilities.map((c) => c.backend_id);
  const pickableBackends = backends.filter((b) => !attachedBackendIds.includes(b.id));

  const closeAll = () => {
    setPickerOpen(false);
    setApiBackendId(null);
    setComfyBackendId(null);
  };
  const pickBackend = (backendId: string) => {
    const backend = backends.find((b) => b.id === backendId);
    if (!backend) return;
    setPickerOpen(false);
    if (backend.kind === "api_provider") {
      setApiBackendId(backendId);
    } else {
      setComfyBackendId(backendId);
      onOpenWizard();
    }
  };
  const anyFormOpen = pickerOpen || apiBackendId !== null || wizardOpen;

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
                {/* Baked-in workflow_json text (prompts) is a comfyui_workflow-only
                    concept -- api_call/native capabilities have no workflow graph
                    for find_editable_text_fields to walk. */}
                {c.execution_type === "comfyui_workflow" && (
                  <button onClick={() => setPromptsFor(c)} title="Edit prompt text baked directly into this workflow">
                    prompts
                  </button>
                )}
                <button onClick={() => tryDelete(() => capabilitiesApi.remove(c.id), reloadCapabilities)}>delete</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {promptsFor && <CapabilityTextFieldsModal capability={promptsFor} onClose={() => setPromptsFor(null)} />}

      {pickerOpen && (
        <div className="inline-form">
          <select defaultValue="" onChange={(e) => e.target.value && pickBackend(e.target.value)}>
            <option value="" disabled>
              which backend?
            </option>
            {pickableBackends.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name} ({b.kind === "api_provider" ? KNOWN_PROVIDERS.find((p) => p.value === b.provider)?.label ?? b.provider : "comfyui"})
              </option>
            ))}
          </select>
          <button onClick={closeAll}>Cancel</button>
          {pickableBackends.length === 0 && (
            <span style={{ fontSize: 12, color: "var(--text-dim)" }}>
              No unattached backends — add one in Backends above first.
            </span>
          )}
        </div>
      )}

      {apiBackendId &&
        (() => {
          const backend = backends.find((b) => b.id === apiBackendId);
          return backend ? (
            <AddApiInstanceForm
              template={template}
              backend={backend}
              onCancel={closeAll}
              onSaved={() => {
                closeAll();
                reloadCapabilities();
                reloadTemplates();
              }}
            />
          ) : null;
        })()}

      {wizardOpen && comfyBackendId && (
        <NodeTypeWizard
          backends={backends}
          mode={{
            kind: "add-instance",
            template,
            // Narrows NodeTypeWizard's own backend picker down to exactly the
            // one already chosen in the unified picker above -- everything
            // else (including already-attached backends) stays excluded.
            excludeBackendIds: backends.map((b) => b.id).filter((id) => id !== comfyBackendId),
          }}
          onCancel={() => {
            onCancelWizard();
            closeAll();
          }}
          onSaved={() => {
            onSaved();
            closeAll();
          }}
        />
      )}

      {!anyFormOpen && (
        <div className="node-actions">
          <button className="primary" onClick={() => setPickerOpen(true)}>
            + Add instance
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
    </div>
  );
}
