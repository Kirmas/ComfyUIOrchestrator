import type { DetectedField, ParamField, WorkflowAnalysis, WorkflowNodeInfo } from "./types";

// Matching a workflow's detected fields onto an *existing* template's fixed
// schema (add-instance flow). DetectedField.key is deterministic, fixed
// vocabulary produced by analyze_workflow (seed/steps/cfg/.../slugified
// Primitive titles), and field.name was originally populated from that same
// key when the template was first created -- so exact key equality is a
// correct match, not a heuristic guess.
export type FieldResolution = "unresolved" | { detectedKey: string | null };

export function matchTypeFor(detectedType: string): ParamField["type"] {
  return detectedType === "seed"
    ? "seed"
    : detectedType === "int"
      ? "int"
      : detectedType === "float"
        ? "float"
        : detectedType === "bool"
          ? "bool"
          : detectedType === "enum"
            ? "enum"
            : "text";
}

/** Whether a detected field can fill an existing template field's slot.
 * Exact type equality, except that enum and text are interchangeable: both
 * write a string into the same ComfyUI input, and whether a widget reads as a
 * dropdown depends on whether the analyze call could reach a backend at all
 * (see apply_combo_options). Without this, adding a second backend to a node
 * type created before options existed would leave every combo field
 * unresolved. */
export function typesCompatible(a: ParamField["type"], b: ParamField["type"]): boolean {
  if (a === b) return true;
  const stringy = (t: ParamField["type"]) => t === "enum" || t === "text";
  return stringy(a) && stringy(b);
}

/** Pairs each declared image slot (by its own kind -- same AssetKind value
 * space as WorkflowNodeInfo.likely_kind, null meaning "just a plain
 * picture") with one of the workflow's actually-detected LoadImage nodes --
 * used by NodeTypeWizard.tsx for both a fresh template's input slots
 * (`slotKinds` from each slot's own typed label) and an existing template's
 * image fields when adding a second backend (`slotKinds` from
 * ParamField.expects_kind).
 *
 * Naively zipping the two lists by declaration order (the previous behavior)
 * pairs purely on coincidence: a workflow's own LoadImage node order has
 * nothing to do with the order a human happened to type slot labels in, so a
 * 2-slot "Image"/"Mask" workflow could -- and did (2026-08-13 incident) --
 * come out backwards whenever the graph's "Load Mask" node sits before its
 * "Load Image" one. Matching within each kind's own bucket first (falling
 * back to a plain positional zip only when a bucket's counts don't actually
 * line up on both sides, i.e. genuinely ambiguous) mirrors templateUtils.ts's
 * defaultInputsForSchema kind-matching -- same "only a same-bucket,
 * same-count pairing is unambiguous" reasoning. Deliberately N-way by kind
 * rather than a mask-only boolean: the day a mesh (or anything else with its
 * own ComfyUI loader) gets detected the same way, it's just another bucket
 * here, not a second parallel special case. */
/** Which of the workflow's detected LoadImage nodes are valid dropdown
 * choices for a slot that wants `wantedKind` -- masks only pair with masks,
 * plain pictures only with plain pictures, kind for kind, structurally, not
 * "pick whatever and get warned about it" (2026-08-13: a warning next to the
 * dropdown wasn't enough, the wrong pairing could still be saved). Falls
 * back to the full list when nothing actually matches `wantedKind` -- kind
 * detection is a heuristic (workflow_analyzer.py's ImageToMask-edge check),
 * and an empty dropdown would strand the user with no way to pick anything
 * at all, which is worse than an unfiltered one. */
export function nodesForKind(nodes: WorkflowNodeInfo[], wantedKind: string | null): WorkflowNodeInfo[] {
  const matching = nodes.filter((n) => (n.likely_kind ?? null) === wantedKind);
  return matching.length > 0 ? matching : nodes;
}

export function matchInputNodesToSlots(nodes: WorkflowNodeInfo[], slotKinds: (string | null)[]): string[] {
  const nodesByKind = new Map<string | null, WorkflowNodeInfo[]>();
  for (const n of nodes) {
    const bucket = nodesByKind.get(n.likely_kind) ?? [];
    bucket.push(n);
    nodesByKind.set(n.likely_kind, bucket);
  }
  const slotCountByKind = new Map<string | null, number>();
  for (const kind of slotKinds) slotCountByKind.set(kind, (slotCountByKind.get(kind) ?? 0) + 1);

  // Every bucket has to reconcile in both directions -- a kind the slots ask
  // for but no node has (or vice versa) means the buckets can't actually be
  // trusted, so this falls back to the old positional behavior wholesale
  // rather than guessing part of the pairing.
  const useBuckets =
    [...slotCountByKind].every(([kind, count]) => (nodesByKind.get(kind)?.length ?? 0) === count) &&
    [...nodesByKind].every(([kind, bucket]) => (slotCountByKind.get(kind) ?? 0) === bucket.length);

  const nextIndexByKind = new Map<string | null, number>();
  return slotKinds.map((kind, i) => {
    if (!useBuckets) return nodes[i]?.node_id ?? "";
    const idx = nextIndexByKind.get(kind) ?? 0;
    nextIndexByKind.set(kind, idx + 1);
    return nodesByKind.get(kind)?.[idx]?.node_id ?? "";
  });
}

export function autoMatchField(field: ParamField, analysis: WorkflowAnalysis): FieldResolution {
  const detected = analysis.detected_fields.find((f) => f.key === field.name);
  if (detected && typesCompatible(matchTypeFor(detected.type), field.type)) return { detectedKey: detected.key };
  return "unresolved";
}

export interface DetectedFieldGroup {
  signature: string;
  fields: DetectedField[];
}

function longestCommonPrefix(strings: string[]): string {
  if (strings.length === 0) return "";
  let prefix = strings[0];
  for (const s of strings.slice(1)) {
    let i = 0;
    while (i < prefix.length && i < s.length && prefix[i] === s[i]) i++;
    prefix = prefix.slice(0, i);
  }
  return prefix;
}

/** Groups sibling fields flattened from one composite widget back together
 * (see workflow_analyzer.py's KNOWN_NODE_COMPOSITE_FIELDS, e.g. ImageCropV2's
 * crop_region -> crop_x/y/width/height) -- a "create new node type" wizard
 * showing 4 independently checkable fields for one crop box doesn't make
 * sense, since including only some of them is meaningless. A composite
 * subfield is the only kind of detected field whose input_key carries a dot
 * ("crop_region.x"); anything else (KSampler literal, Primitive, ...) is a
 * standalone singleton group. */
export function groupDetectedFields(fields: DetectedField[]): DetectedFieldGroup[] {
  const groups = new Map<string, DetectedField[]>();
  for (const f of fields) {
    const signature = f.input_key.includes(".") ? `${f.node_id}:${f.input_key.split(".")[0]}` : `single:${f.key}`;
    const list = groups.get(signature);
    if (list) list.push(f);
    else groups.set(signature, [f]);
  }
  return [...groups.entries()].map(([signature, groupFields]) => ({ signature, fields: groupFields }));
}

/** Default shared label for a group -- the common leading text of its
 * members' labels ("Crop X"/"Crop Y"/"Crop Width"/"Crop Height" -> "Crop"). */
export function defaultGroupLabel(fields: DetectedField[]): string {
  return longestCommonPrefix(fields.map((f) => f.label)).trim() || fields[0].label;
}

/** Field-name suffix distinguishing one group member from another ("crop_x"
 * under shared name-prefix "crop_" -> "x"), used to rebuild each member's
 * individual label from a new shared group label ("Crop" + "x" -> "Crop X"). */
export function groupMemberSuffix(fields: DetectedField[], field: DetectedField): string {
  const namePrefix = longestCommonPrefix(fields.map((f) => f.key));
  const suffix = field.key.slice(namePrefix.length);
  return suffix.charAt(0).toUpperCase() + suffix.slice(1);
}
