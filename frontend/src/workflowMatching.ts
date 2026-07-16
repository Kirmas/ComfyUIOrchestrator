import type { DetectedField, ParamField, WorkflowAnalysis } from "./types";

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
          : "text";
}

export function autoMatchField(field: ParamField, analysis: WorkflowAnalysis): FieldResolution {
  const detected = analysis.detected_fields.find((f) => f.key === field.name);
  if (detected && matchTypeFor(detected.type) === field.type) return { detectedKey: detected.key };
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
