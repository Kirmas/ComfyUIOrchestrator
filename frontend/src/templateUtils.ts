import type { InputRef, ParamSchema } from "./types";

// Mirrors core/node_types.py's is_slot_field/slot_fields: a "fixed" image/file
// field's value is a constant baked onto the node type (NodeTemplate.defaults),
// not something a grid cell supplies, so it never occupies a row and never
// gets a per-cell picker.
export function slotFields(schema: ParamSchema | undefined | null) {
  return (schema?.fields ?? []).filter((f) => (f.type === "image" || f.type === "file") && !f.fixed);
}

export function defaultInputsForSchema(schema: ParamSchema | undefined | null, existing: InputRef[]): InputRef[] {
  const slots = slotFields(schema);
  const inputs: InputRef[] = [];
  for (let i = 0; i < slots.length; i++) {
    // Row-span paradigm: a fresh slot defaults to reading its own row
    // offset within the workflow node's span (0, 1, 2, ... in slot order) --
    // position is the connection now, so there's nothing left to ask.
    inputs.push(existing[i] ?? { type: "cell_index", index: i });
  }
  return inputs;
}
