import type { InputRef, ParamSchema } from "./types";

export function slotFields(schema: ParamSchema | undefined | null) {
  return (schema?.fields ?? []).filter((f) => f.type === "image" || f.type === "file");
}

export function defaultInputsForSchema(schema: ParamSchema | undefined | null, existing: InputRef[]): InputRef[] {
  const slots = slotFields(schema);
  const inputs: InputRef[] = [];
  for (let i = 0; i < slots.length; i++) {
    if (existing[i]) {
      inputs.push(existing[i]);
    } else {
      inputs.push(i === 1 ? { type: "track_below_prev" } : { type: "self_prev" });
    }
  }
  return inputs;
}
