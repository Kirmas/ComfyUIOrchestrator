import { useState } from "react";
import { capabilitiesApi, nodeTemplatesApi } from "../api/endpoints";
import { detectCropGroups } from "../cropUtils";
import { slotFields } from "../templateUtils";
import {
  autoMatchField,
  defaultGroupLabel,
  groupDetectedFields,
  groupMemberSuffix,
  matchTypeFor,
  type DetectedFieldGroup,
  type FieldResolution,
} from "../workflowMatching";
import type { Backend, NodeTemplate, ParamField, WorkflowAnalysis } from "../types";

export type WizardMode = { kind: "create" } | { kind: "add-instance"; template: NodeTemplate; excludeBackendIds: string[] };

interface InputSlot {
  label: string;
  nodeId: string;
}

const ABSENT = "__absent__";

function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

// Shared by both the crop-group row and the plain-field row below (the
// add-instance "match against this template's schema" step) -- the two
// differ in what they're matching (a whole crop group vs one field) and how
// a pick gets applied, but the select/options/status-footer markup itself
// would otherwise be pasted twice verbatim.
function ResolveFieldRow<T>({
  label,
  typeHint,
  value,
  options,
  optionValue,
  optionLabel,
  onChange,
}: {
  label: string;
  typeHint: string;
  value: string; // "" (unresolved) | ABSENT | optionValue(picked option)
  options: T[];
  optionValue: (opt: T) => string;
  optionLabel: (opt: T) => string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="inline-form" style={{ marginTop: 0 }}>
      <span style={{ width: 140 }}>
        {label} <span style={{ color: "var(--text-dim)", fontSize: 11 }}>({typeHint})</span>
      </span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="" disabled>
          — choose —
        </option>
        {options.map((o) => (
          <option key={optionValue(o)} value={optionValue(o)}>
            {optionLabel(o)}
          </option>
        ))}
        <option value={ABSENT}>— not present in this workflow —</option>
      </select>
      {value === ABSENT && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>will be left as-is in this workflow</span>}
      {value !== ABSENT && value !== "" && <span style={{ color: "var(--text-dim)", fontSize: 11 }}>matched</span>}
      {value === "" && <span className="error-text">no confident auto-match -- choose one</span>}
    </div>
  );
}

export function NodeTypeWizard({ backends, mode, onCancel, onSaved }: { backends: Backend[]; mode: WizardMode; onCancel: () => void; onSaved: () => void }) {
  const comfyBackends = backends.filter((b) => b.kind === "comfyui");
  const availableBackends = mode.kind === "add-instance" ? comfyBackends.filter((b) => !mode.excludeBackendIds.includes(b.id)) : comfyBackends;

  const [step, setStep] = useState<1 | 2 | 3>(1);

  // create-only config
  const [name, setName] = useState("");
  const [nodeTypeSlug, setNodeTypeSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [inputSlots, setInputSlots] = useState<InputSlot[]>([]);
  const [expectedOutputCount, setExpectedOutputCount] = useState(1);
  const [fieldIncluded, setFieldIncluded] = useState<Record<string, boolean>>({});
  const [fieldLabels, setFieldLabels] = useState<Record<string, string>>({});
  const [groupLabelOverride, setGroupLabelOverride] = useState<Record<string, string>>({});

  const [selectedBackendId, setSelectedBackendId] = useState("");

  const [workflowJson, setWorkflowJson] = useState<Record<string, unknown> | null>(null);
  const [analysis, setAnalysis] = useState<WorkflowAnalysis | null>(null);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  // add-instance-only mapping state
  const [imageSlotNodeIds, setImageSlotNodeIds] = useState<string[]>([]);
  const [fieldResolutions, setFieldResolutions] = useState<Record<string, FieldResolution>>({});

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const updateName = (value: string) => {
    setName(value);
    if (!slugTouched) setNodeTypeSlug(slugify(value));
  };

  const addInputSlot = () => setInputSlots((s) => [...s, { label: `Image ${s.length + 1}`, nodeId: "" }]);
  const removeInputSlot = (i: number) => setInputSlots((s) => s.filter((_, idx) => idx !== i));

  const templateImageFields = mode.kind === "add-instance" ? slotFields(mode.template.param_schema) : [];
  const templateOtherFields = mode.kind === "add-instance" ? mode.template.param_schema.fields.filter((f) => f.type !== "image" && f.type !== "file") : [];

  // Same complaint as "create" mode's Detected fields, mirrored here: this
  // template's own schema already has crop_x/y/width/height as 4 separate
  // fields (detectCropGroups matches them by name, same convention
  // KNOWN_NODE_COMPOSITE_FIELDS produces), so resolving each independently
  // against the uploaded workflow means 4 near-identical dropdowns for what's
  // really one crop box -- group them into a single dropdown instead.
  const templateCropGroups = mode.kind === "add-instance" ? detectCropGroups(templateOtherFields) : [];
  const cropGroupedFieldNames = new Set(templateCropGroups.flatMap((g) => [g.xField, g.yField, g.widthField, g.heightField]));
  const templateSingleFields = templateOtherFields.filter((f) => !cropGroupedFieldNames.has(f.name));

  // Candidate matches for a template crop group: composite groups detected in
  // the *uploaded* workflow (see groupDetectedFields) that decompose into the
  // same x/y/width/height shape.
  const detectedCropGroups: DetectedFieldGroup[] = analysis
    ? groupDetectedFields(analysis.detected_fields).filter(
        (g) =>
          g.fields.length === 4 &&
          ["x", "y", "width", "height"].every((want) => g.fields.some((f) => groupMemberSuffix(g.fields, f).toLowerCase() === want)),
      )
    : [];
  const detectedGroupField = (group: DetectedFieldGroup, suffix: string) =>
    group.fields.find((f) => groupMemberSuffix(group.fields, f).toLowerCase() === suffix);

  const canGoToUpload = mode.kind === "create" ? Boolean(name.trim() && nodeTypeSlug.trim() && selectedBackendId) : Boolean(selectedBackendId);

  const handleFile = async (file: File) => {
    setAnalyzeError(null);
    setAnalysis(null);
    const text = await file.text();
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch {
      setAnalyzeError("File is not valid JSON.");
      return;
    }
    setWorkflowJson(parsed);

    let result: WorkflowAnalysis;
    try {
      result = await nodeTemplatesApi.analyzeWorkflow(file);
    } catch (err) {
      setAnalyzeError(err instanceof Error ? err.message : "Failed to analyze workflow.");
      return;
    }

    if (mode.kind === "create") {
      if (result.input_image_nodes.length !== inputSlots.length) {
        setAnalyzeError(
          `Declared ${inputSlots.length} input image slot(s), but the workflow has ${result.input_image_nodes.length} LoadImage node(s). Go back and adjust, or upload a different file.`,
        );
        return;
      }
      if (result.output_nodes.length !== expectedOutputCount) {
        setAnalyzeError(
          `Expected ${expectedOutputCount} output node(s), but the workflow has ${result.output_nodes.length} SaveImage/PreviewImage node(s). Go back and adjust, or upload a different file.`,
        );
        return;
      }
      setInputSlots((slots) => slots.map((s, i) => ({ ...s, nodeId: result.input_image_nodes[i]?.node_id ?? "" })));
      const included: Record<string, boolean> = {};
      const labels: Record<string, string> = {};
      for (const f of result.detected_fields) {
        included[f.key] = true;
        labels[f.key] = f.label;
      }
      setFieldIncluded(included);
      setFieldLabels(labels);
    } else {
      if (result.input_image_nodes.length !== templateImageFields.length) {
        setAnalyzeError(
          `This template expects ${templateImageFields.length} image input(s), but the uploaded workflow has ${result.input_image_nodes.length}. Go back and adjust, or upload a different file.`,
        );
        return;
      }
      setImageSlotNodeIds(templateImageFields.map((_, i) => result.input_image_nodes[i]?.node_id ?? ""));
      const resolutions: Record<string, FieldResolution> = {};
      for (const f of templateOtherFields) resolutions[f.name] = autoMatchField(f, result);
      setFieldResolutions(resolutions);
    }

    setAnalysis(result);
    setStep(3);
  };

  const titleOf = (nodeId: string): string | null | undefined => {
    if (!analysis || !workflowJson) return undefined;
    const known = [...analysis.input_image_nodes, ...analysis.output_nodes].find((n) => n.node_id === nodeId)?.title;
    if (known) return known;
    return (workflowJson[nodeId] as { _meta?: { title?: string } } | undefined)?._meta?.title;
  };

  const missingTitlesCreate = analysis
    ? [
        ...inputSlots.map((s) => analysis.input_image_nodes.find((n) => n.node_id === s.nodeId)).filter((n) => n && !n.title),
        ...analysis.detected_fields
          .filter((f) => fieldIncluded[f.key])
          .filter((f) => {
            const node = [...analysis.input_image_nodes, ...analysis.output_nodes].find((n) => n.node_id === f.node_id);
            return node && !node.title;
          }),
      ]
    : [];

  const missingTitlesAddInstance = analysis
    ? [
        ...imageSlotNodeIds.filter((id) => id && !titleOf(id)),
        ...Object.values(fieldResolutions)
          .filter((r): r is { detectedKey: string } => r !== "unresolved" && r.detectedKey !== null)
          .filter((r) => {
            const df = analysis.detected_fields.find((f) => f.key === r.detectedKey);
            return df && !titleOf(df.node_id);
          }),
      ]
    : [];

  const hasUnresolvedField = mode.kind === "add-instance" && Object.values(fieldResolutions).some((r) => r === "unresolved");
  const missingTitlesCount = mode.kind === "create" ? missingTitlesCreate.length : missingTitlesAddInstance.length;

  // analysis.duplicate_titles (from the backend) flags every title collision
  // among *all* detected fields, computed once at analyze time -- it has no
  // idea which fields the user has since unchecked (create mode) or left
  // unmapped (add-instance mode). A collision only actually matters if
  // param_mapping would end up writing two different entries under the same
  // title, so recompute it here against whichever fields are still live,
  // the same way missingTitlesCreate/submitCreate already filter by
  // fieldIncluded/fieldResolutions.
  const activeDuplicateTitles = (() => {
    if (!analysis) return [];
    const nodeIdsByTitle = new Map<string, Set<string>>();
    const record = (nodeId: string | undefined | null) => {
      if (!nodeId) return;
      const title = titleOf(nodeId);
      if (!title) return;
      if (!nodeIdsByTitle.has(title)) nodeIdsByTitle.set(title, new Set());
      nodeIdsByTitle.get(title)!.add(nodeId);
    };
    for (const n of analysis.input_image_nodes) record(n.node_id);
    for (const n of analysis.output_nodes) record(n.node_id);
    const activeFields =
      mode.kind === "create"
        ? analysis.detected_fields.filter((f) => fieldIncluded[f.key] ?? true)
        : analysis.detected_fields.filter((f) =>
            Object.values(fieldResolutions).some((r) => r !== "unresolved" && r.detectedKey === f.key),
          );
    for (const f of activeFields) record(f.node_id);
    return [...nodeIdsByTitle.entries()]
      .filter(([, ids]) => ids.size > 1)
      .map(([title]) => title)
      .sort();
  })();

  const submitCreate = async () => {
    if (!analysis || !workflowJson || mode.kind !== "create") return;
    const paramFields: ParamField[] = [];
    const paramMapping: Record<string, string> = {};
    const defaults: Record<string, unknown> = {};

    inputSlots.forEach((slot, i) => {
      const node = analysis.input_image_nodes.find((n) => n.node_id === slot.nodeId);
      if (!node) throw new Error(`Input slot "${slot.label}" has no assigned node.`);
      if (!node.title) throw new Error(`The LoadImage node assigned to "${slot.label}" has no title in ComfyUI -- rename it and re-export.`);
      const fieldName = `image_${i + 1}`;
      paramFields.push({ name: fieldName, type: "image", label: slot.label, required: true });
      paramMapping[fieldName] = `${node.title}.image`;
    });

    for (const f of analysis.detected_fields) {
      if (!fieldIncluded[f.key]) continue;
      const title = titleOf(f.node_id);
      if (!title) throw new Error(`The node backing "${fieldLabels[f.key] ?? f.label}" has no title in ComfyUI -- rename it and re-export.`);
      const fieldType = matchTypeFor(f.type);
      paramFields.push({ name: f.key, type: fieldType, label: fieldLabels[f.key] ?? f.label, default: f.default });
      paramMapping[f.key] = `${title}.${f.input_key}`;
      defaults[f.key] = f.default;
    }

    await nodeTemplatesApi.create({
      node_type_slug: nodeTypeSlug,
      name,
      param_schema: { fields: paramFields },
      defaults,
    });

    await capabilitiesApi.create({
      backend_id: selectedBackendId,
      node_type_slug: nodeTypeSlug,
      execution_type: "comfyui_workflow",
      config: { workflow_json: workflowJson, param_mapping: paramMapping },
    });
  };

  const submitAddInstance = async () => {
    if (!analysis || !workflowJson || mode.kind !== "add-instance") return;
    const paramMapping: Record<string, string> = {};

    templateImageFields.forEach((field, i) => {
      const nodeId = imageSlotNodeIds[i];
      const node = analysis.input_image_nodes.find((n) => n.node_id === nodeId);
      if (!node) throw new Error(`Image field "${field.label ?? field.name}" has no assigned node.`);
      if (!node.title) throw new Error(`The LoadImage node assigned to "${field.label ?? field.name}" has no title in ComfyUI -- rename it and re-export.`);
      paramMapping[field.name] = `${node.title}.image`;
    });

    for (const field of templateOtherFields) {
      const resolution = fieldResolutions[field.name];
      if (resolution === "unresolved" || !resolution) throw new Error(`Field "${field.label ?? field.name}" is not resolved.`);
      if (resolution.detectedKey === null) continue;
      const detected = analysis.detected_fields.find((f) => f.key === resolution.detectedKey);
      if (!detected) throw new Error(`Field "${field.label ?? field.name}" points to a detected field that no longer exists.`);
      const title = titleOf(detected.node_id);
      if (!title) throw new Error(`The node backing "${field.label ?? field.name}" has no title in ComfyUI -- rename it and re-export.`);
      paramMapping[field.name] = `${title}.${detected.input_key}`;
    }

    await capabilitiesApi.create({
      backend_id: selectedBackendId,
      node_type_slug: mode.template.node_type_slug,
      execution_type: "comfyui_workflow",
      config: { workflow_json: workflowJson, param_mapping: paramMapping },
    });
  };

  const submit = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      if (mode.kind === "create") await submitCreate();
      else await submitAddInstance();
      onSaved();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save.");
    } finally {
      setSaving(false);
    }
  };

  const canApprove = analysis && !saving && missingTitlesCount === 0 && activeDuplicateTitles.length === 0 && !hasUnresolvedField;

  return (
    <div className="settings-section">
      <h2>{mode.kind === "create" ? "New node type (from a ComfyUI workflow)" : `Add ComfyUI instance to "${mode.template.name}"`}</h2>

      {step === 1 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {mode.kind === "create" && (
            <>
              <div className="field-row">
                <label>Display name</label>
                <input value={name} onChange={(e) => updateName(e.target.value)} placeholder="Text to Image" />
              </div>
              <div className="field-row">
                <label>node_type_slug</label>
                <input
                  value={nodeTypeSlug}
                  onChange={(e) => {
                    setSlugTouched(true);
                    setNodeTypeSlug(e.target.value);
                  }}
                  placeholder="text_to_image"
                />
              </div>
            </>
          )}

          <div className="field-row">
            <label>ComfyUI backend this instance runs on</label>
            {availableBackends.length === 0 && (
              <span style={{ color: "var(--text-dim)" }}>
                {mode.kind === "add-instance" ? "All registered ComfyUI backends already have an instance of this node type." : "No ComfyUI backends registered yet."}
              </span>
            )}
            <select value={selectedBackendId} onChange={(e) => setSelectedBackendId(e.target.value)}>
              <option value="">choose backend…</option>
              {availableBackends.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>

          {mode.kind === "create" && (
            <>
              <div className="field-row">
                <label>Input image slots (0 or more -- e.g. a reference photo)</label>
                {inputSlots.map((slot, i) => (
                  <div key={i} className="inline-form" style={{ marginTop: 0 }}>
                    <input
                      value={slot.label}
                      onChange={(e) => setInputSlots((s) => s.map((x, idx) => (idx === i ? { ...x, label: e.target.value } : x)))}
                    />
                    <button onClick={() => removeInputSlot(i)}>remove</button>
                  </div>
                ))}
                <button onClick={addInputSlot} style={{ alignSelf: "flex-start" }}>
                  + input slot
                </button>
              </div>

              <div className="field-row">
                <label>Expected output count (SaveImage/PreviewImage nodes in the workflow)</label>
                <input
                  type="number"
                  min={1}
                  value={expectedOutputCount}
                  onChange={(e) => setExpectedOutputCount(Number(e.target.value))}
                  style={{ width: 80 }}
                />
              </div>
            </>
          )}

          <div className="node-actions">
            <button className="primary" disabled={!canGoToUpload} onClick={() => setStep(2)}>
              Next: upload workflow.json
            </button>
            <button onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <input
            type="file"
            accept=".json,application/json"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) handleFile(file);
            }}
          />
          {analyzeError && <div className="error-text">{analyzeError}</div>}
          <button onClick={() => setStep(1)} style={{ alignSelf: "flex-start" }}>
            ← back
          </button>
        </div>
      )}

      {step === 3 && analysis && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {activeDuplicateTitles.length > 0 && (
            <div className="error-text">
              These node titles are duplicated among the fields you're keeping, so mapping by title is ambiguous: {activeDuplicateTitles.join(", ")}.
              Uncheck/remap enough of them to leave only one per title, or rename the nodes in ComfyUI and re-upload.
            </div>
          )}

          {mode.kind === "create" &&
            inputSlots.map((slot, i) => (
              <div key={i} className="field-row">
                <label>{slot.label} -- source LoadImage node</label>
                <select
                  value={slot.nodeId}
                  onChange={(e) => setInputSlots((s) => s.map((x, idx) => (idx === i ? { ...x, nodeId: e.target.value } : x)))}
                >
                  {analysis.input_image_nodes.map((n) => (
                    <option key={n.node_id} value={n.node_id}>
                      {n.title ?? `(untitled node ${n.node_id})`}
                    </option>
                  ))}
                </select>
              </div>
            ))}

          {mode.kind === "add-instance" &&
            templateImageFields.map((field, i) => (
              <div key={field.name} className="field-row">
                <label>{field.label ?? field.name} -- source LoadImage node</label>
                <select
                  value={imageSlotNodeIds[i] ?? ""}
                  onChange={(e) => setImageSlotNodeIds((s) => s.map((x, idx) => (idx === i ? e.target.value : x)))}
                >
                  {analysis.input_image_nodes.map((n) => (
                    <option key={n.node_id} value={n.node_id}>
                      {n.title ?? `(untitled node ${n.node_id})`}
                    </option>
                  ))}
                </select>
              </div>
            ))}

          <div className="field-row">
            <label>Output nodes found</label>
            <span>{analysis.output_nodes.map((n) => n.title ?? n.node_id).join(", ")}</span>
          </div>

          {mode.kind === "create" && (
            <div className="field-row">
              <label>Detected fields</label>
              {analysis.detected_fields.length === 0 && <span style={{ color: "var(--text-dim)" }}>None detected (no KSampler found).</span>}
              {groupDetectedFields(analysis.detected_fields).map((group) =>
                group.fields.length === 1 ? (
                  (() => {
                    const f = group.fields[0];
                    return (
                      <div key={f.key} className="inline-form" style={{ marginTop: 0 }}>
                        <input
                          type="checkbox"
                          checked={fieldIncluded[f.key] ?? true}
                          onChange={(e) => setFieldIncluded((m) => ({ ...m, [f.key]: e.target.checked }))}
                        />
                        <input
                          value={fieldLabels[f.key] ?? f.label}
                          onChange={(e) => setFieldLabels((m) => ({ ...m, [f.key]: e.target.value }))}
                          style={{ width: 140 }}
                        />
                        <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
                          {f.type}, default: {String(f.default)}
                        </span>
                      </div>
                    );
                  })()
                ) : (
                  // A composite widget (e.g. ImageCropV2's crop_region) flattens into
                  // several fields sharing one node -- toggling/labelling them one at a
                  // time doesn't mean anything (a crop with only 3 of its 4 numbers is
                  // meaningless), so this group gets one checkbox/label for all of them.
                  <div key={group.signature} className="inline-form" style={{ marginTop: 0 }}>
                    <input
                      type="checkbox"
                      checked={group.fields.every((f) => fieldIncluded[f.key] ?? true)}
                      onChange={(e) =>
                        setFieldIncluded((m) => {
                          const next = { ...m };
                          for (const f of group.fields) next[f.key] = e.target.checked;
                          return next;
                        })
                      }
                    />
                    <input
                      value={groupLabelOverride[group.signature] ?? defaultGroupLabel(group.fields)}
                      onChange={(e) => {
                        const label = e.target.value;
                        setGroupLabelOverride((m) => ({ ...m, [group.signature]: label }));
                        setFieldLabels((m) => {
                          const next = { ...m };
                          for (const f of group.fields) next[f.key] = `${label} ${groupMemberSuffix(group.fields, f)}`;
                          return next;
                        });
                      }}
                      style={{ width: 140 }}
                    />
                    <span style={{ color: "var(--text-dim)", fontSize: 11 }}>
                      {group.fields.length} numbers ({group.fields.map((f) => groupMemberSuffix(group.fields, f)).join("/")})
                    </span>
                  </div>
                ),
              )}
            </div>
          )}

          {mode.kind === "add-instance" && (
            <div className="field-row">
              <label>Fields (matched against this node type's existing schema)</label>
              {templateOtherFields.length === 0 && <span style={{ color: "var(--text-dim)" }}>No non-image fields on this template.</span>}

              {templateCropGroups.map((group) => {
                const names = [group.xField, group.yField, group.widthField, group.heightField];
                const resolutions = names.map((n) => fieldResolutions[n] ?? "unresolved");
                const anyUnresolved = resolutions.some((r) => r === "unresolved");
                const allAbsent = !anyUnresolved && resolutions.every((r) => r !== "unresolved" && r.detectedKey === null);
                const matched = anyUnresolved
                  ? undefined
                  : detectedCropGroups.find((g) =>
                      (["x", "y", "width", "height"] as const).every(
                        (suffix, i) => (resolutions[i] as { detectedKey: string | null }).detectedKey === detectedGroupField(g, suffix)?.key,
                      ),
                    );
                const value = allAbsent ? ABSENT : matched ? matched.signature : "";
                return (
                  <ResolveFieldRow
                    key={group.prefix}
                    label={group.prefix}
                    typeHint="crop, 4x int"
                    value={value}
                    options={detectedCropGroups}
                    optionValue={(g) => g.signature}
                    optionLabel={(g) => `${defaultGroupLabel(g.fields)} (crop)`}
                    onChange={(v) =>
                      setFieldResolutions((m) => {
                        const next = { ...m };
                        if (v === ABSENT) {
                          for (const n of names) next[n] = { detectedKey: null };
                        } else {
                          const picked = detectedCropGroups.find((g) => g.signature === v);
                          if (!picked) return m;
                          next[group.xField] = { detectedKey: detectedGroupField(picked, "x")?.key ?? null };
                          next[group.yField] = { detectedKey: detectedGroupField(picked, "y")?.key ?? null };
                          next[group.widthField] = { detectedKey: detectedGroupField(picked, "width")?.key ?? null };
                          next[group.heightField] = { detectedKey: detectedGroupField(picked, "height")?.key ?? null };
                        }
                        return next;
                      })
                    }
                  />
                );
              })}

              {templateSingleFields.map((field) => {
                const resolution = fieldResolutions[field.name] ?? "unresolved";
                const value = resolution === "unresolved" ? "" : resolution.detectedKey === null ? ABSENT : resolution.detectedKey;
                return (
                  <ResolveFieldRow
                    key={field.name}
                    label={field.label ?? field.name}
                    typeHint={field.type}
                    value={value}
                    options={analysis.detected_fields}
                    optionValue={(f) => f.key}
                    optionLabel={(f) => `${f.label} (${f.type})`}
                    onChange={(v) =>
                      setFieldResolutions((m) => ({
                        ...m,
                        [field.name]: v === ABSENT ? { detectedKey: null } : { detectedKey: v },
                      }))
                    }
                  />
                );
              })}
            </div>
          )}

          {missingTitlesCount > 0 && (
            <div className="error-text">Some assigned nodes have no title in ComfyUI -- rename them and re-upload before saving.</div>
          )}
          {saveError && <div className="error-text">{saveError}</div>}

          <div className="node-actions">
            <button className="primary" onClick={submit} disabled={!canApprove}>
              {saving ? "Saving…" : "Approve"}
            </button>
            <button onClick={() => setStep(2)}>← back</button>
            <button onClick={onCancel}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
