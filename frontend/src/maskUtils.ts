import type { ParamField } from "./types";

export interface MaskGroup {
  maskField: string;
  label: string;
}

/** Finds "mask"-typed fields (today only native.mask's own "mask_png" field)
 * so they render as a paint canvas instead of a raw text input -- mirrors
 * cropUtils.ts's detectCropGroups, but a mask group is just one field, not
 * four grouped by name suffix. */
export function detectMaskGroups(fields: ParamField[]): MaskGroup[] {
  return fields.filter((f) => f.type === "mask").map((f) => ({ maskField: f.name, label: f.label ?? f.name }));
}

export interface LayerMaskGroup {
  maskField: string;
  label: string;
  /** The image field kept on top (everything not painted comes from it). */
  targetField: string;
  /** The image field underneath, revealed wherever the mask is painted. */
  sourceField: string;
  /** Optional float field softening the mask's edge, by the same convention
   * cropUtils uses for its `_x`/`_y`/`_width`/`_height` group: a name suffix,
   * no extra stored metadata. The editor previews it so what's on screen
   * matches what the backend renders -- including its schema default, which
   * is what an untouched node runs with (nothing copies defaults into
   * Node.params, so reading the param alone would preview a 0 the backend
   * never uses). */
  featherField: string | null;
  featherDefault: number;
}

/** Finds "layer_mask"-typed fields (today only native.transplant's own
 * "transplant_png") -- a mask painted over *two* stacked images rather than
 * one, so the group has to name both. Like resolveMaskImageField below, this
 * is native-only and so can read the two slots straight off the schema in
 * declaration order (which is also the layer order, see node_types.py's
 * "transplant" entry) instead of walking a capability's workflow graph.
 * Anything but exactly two image slots is not a stack, so no group. */
export function detectLayerMaskGroups(fields: ParamField[]): LayerMaskGroup[] {
  const imageFields = fields.filter((f) => f.type === "image" || f.type === "file");
  if (imageFields.length !== 2) return [];
  const feather = fields.find((f) => f.type === "float" && f.name.endsWith("_feather")) ?? null;
  return fields
    .filter((f) => f.type === "layer_mask")
    .map((f) => ({
      maskField: f.name,
      label: f.label ?? f.name,
      targetField: imageFields[0].name,
      sourceField: imageFields[1].name,
      featherField: feather?.name ?? null,
      featherDefault: Number(feather?.default ?? 0),
    }));
}

/** Mask fields are native-only today (see node_types.py's "mask" entry) --
 * native node types have no capability/workflow_json graph to walk (same
 * reasoning as resolveCropImageField's !capability branch in cropUtils.ts),
 * so this only handles the unambiguous "exactly one image/file field" case. */
export function resolveMaskImageField(schemaFields: ParamField[]): string | null {
  const imageFields = schemaFields.filter((f) => f.type === "image" || f.type === "file");
  return imageFields.length === 1 ? imageFields[0].name : null;
}
