import type { ParamField, WorkflowAnalysis } from "./types";

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
