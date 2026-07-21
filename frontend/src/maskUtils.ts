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

/** Mask fields are native-only today (see node_types.py's "mask" entry) --
 * native node types have no capability/workflow_json graph to walk (same
 * reasoning as resolveCropImageField's !capability branch in cropUtils.ts),
 * so this only handles the unambiguous "exactly one image/file field" case. */
export function resolveMaskImageField(schemaFields: ParamField[]): string | null {
  const imageFields = schemaFields.filter((f) => f.type === "image" || f.type === "file");
  return imageFields.length === 1 ? imageFields[0].name : null;
}
