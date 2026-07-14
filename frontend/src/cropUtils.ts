import type { Capability, ParamField } from "./types";

export interface CropGroup {
  prefix: string;
  xField: string;
  yField: string;
  widthField: string;
  heightField: string;
}

/** Finds groups of 4 int fields named "<prefix>_x/_y/_width/_height" -- the shape
 * workflow_analyzer.py's KNOWN_NODE_COMPOSITE_FIELDS produces for a node like
 * ImageCropV2's crop_region -- so they render as one crop box instead of 4
 * unrelated number inputs. Works on already-persisted param_schema (no new
 * stored metadata needed), so it applies to templates created before this
 * existed too. */
export function detectCropGroups(fields: ParamField[]): CropGroup[] {
  const intNames = new Set(fields.filter((f) => f.type === "int").map((f) => f.name));
  const prefixes = new Set<string>();
  for (const name of intNames) {
    if (name.endsWith("_x")) prefixes.add(name.slice(0, -2));
  }
  const groups: CropGroup[] = [];
  for (const prefix of prefixes) {
    const xField = `${prefix}_x`;
    const yField = `${prefix}_y`;
    const widthField = `${prefix}_width`;
    const heightField = `${prefix}_height`;
    if (intNames.has(yField) && intNames.has(widthField) && intNames.has(heightField)) {
      groups.push({ prefix, xField, yField, widthField, heightField });
    }
  }
  return groups;
}

function titleIndex(workflowJson: Record<string, unknown>): { byTitle: Map<string, string>; byId: Map<string, string> } {
  const byTitle = new Map<string, string>();
  const byId = new Map<string, string>();
  for (const [nodeId, node] of Object.entries(workflowJson)) {
    const title = (node as { _meta?: { title?: string } } | undefined)?._meta?.title;
    if (title) {
      byTitle.set(title, nodeId);
      byId.set(nodeId, title);
    }
  }
  return { byTitle, byId };
}

/** Traces the crop node's own "image" input back to whichever schema image
 * field feeds it, using the same param_mapping/workflow_json a capability
 * already carries (see template_engine.py's build_workflow). There's no
 * naming convention linking a crop group to an image slot -- it's pure graph
 * topology -- so this has to walk the actual workflow graph rather than
 * guess (e.g. "first image field"), which would silently be wrong whenever
 * the crop applies to a later slot, as it does here (image_2, not image_1). */
export function resolveCropImageField(capability: Capability | undefined, group: CropGroup, schemaFields: ParamField[]): string | null {
  if (!capability) return null;
  const paramMapping = (capability.config?.param_mapping ?? {}) as Record<string, string>;
  const workflowJson = (capability.config?.workflow_json ?? {}) as Record<string, unknown>;

  const xTarget = paramMapping[group.xField];
  if (!xTarget || !xTarget.includes(".")) return null;
  const cropTitle = xTarget.split(".")[0];

  const { byTitle, byId } = titleIndex(workflowJson);
  const cropNodeId = byTitle.get(cropTitle);
  if (!cropNodeId) return null;

  const cropNode = workflowJson[cropNodeId] as { inputs?: { image?: unknown } } | undefined;
  const imageRef = cropNode?.inputs?.image;
  if (!Array.isArray(imageRef) || imageRef.length < 1) return null;
  const sourceNodeId = String(imageRef[0]);
  const sourceTitle = byId.get(sourceNodeId);
  if (!sourceTitle) return null;

  const imageFieldNames = new Set(schemaFields.filter((f) => f.type === "image" || f.type === "file").map((f) => f.name));
  for (const [fieldName, target] of Object.entries(paramMapping)) {
    if (imageFieldNames.has(fieldName) && target === `${sourceTitle}.image`) return fieldName;
  }
  return null;
}
