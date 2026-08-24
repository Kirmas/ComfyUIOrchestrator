import { assetFace, storeOutputsFor, type OutputsFor } from "./assetNodes";
import { assetNodeAtRowOffset } from "./slotResolution";
import type { InputRef, NodeItem, NodeTemplate, ParamSchema, Track } from "./types";

// Mirrors core/node_types.py's is_slot_field/slot_fields: a "fixed" image/file
// field's value is a constant baked onto the node type (NodeTemplate.defaults),
// not something a grid cell supplies, so it never occupies a row and never
// gets a per-cell picker.
export function slotFields(schema: ParamSchema | undefined | null) {
  return (schema?.fields ?? []).filter((f) => (f.type === "image" || f.type === "file") && !f.fixed);
}

/** Context defaultInputsForSchema needs to smart-default by kind -- everything
 * NodeCell.tsx's chooseTemplate already has in scope. Optional: without it,
 * behavior is exactly the old positional 0,1,2,... default. */
export interface KindMatchContext {
  node: NodeItem;
  tracks: Track[];
  nodesById: Record<string, NodeItem>;
  outputsFor?: OutputsFor;
}

export async function defaultInputsForSchema(
  schema: ParamSchema | undefined | null,
  existing: InputRef[],
  context?: KindMatchContext,
): Promise<InputRef[]> {
  const slots = slotFields(schema);
  // Row-span paradigm: a fresh slot defaults to reading its own row offset
  // within the workflow node's span (0, 1, 2, ... in slot order).
  const inputs: InputRef[] = slots.map((_, i) => existing[i] ?? { type: "cell_index", index: i });
  if (!context) return inputs;

  // What's actually sitting in each row of the node's prospective span right
  // now, by AssetKind (undefined for an empty/unresolvable row).
  const kindAtRow = await Promise.all(
    slots.map(async (_, i) => {
      const assetNode = assetNodeAtRowOffset(context.node, i, context.tracks, context.nodesById);
      if (!assetNode) return undefined;
      const asset = await assetFace(assetNode, context.outputsFor ?? storeOutputsFor);
      return asset?.kind;
    }),
  );

  // Match unambiguously *within* each expected kind, not across the whole
  // node at once -- a lone mask among several images matches directly even
  // though the two image slots among themselves don't (nothing distinguishes
  // "Image 1" from "Image 2" but declaration order), so e.g. "2 images + 1
  // mask, mask physically sitting in row 0" still lands correctly instead of
  // falling back to naive 0,1,2 for everything just because the image half
  // is ambiguous.
  const slotPositionsByKind = new Map<string, number[]>();
  slots.forEach((field, i) => {
    if (existing[i]) return; // an already-made choice is never overridden
    const kind = field.expects_kind ?? "image";
    const positions = slotPositionsByKind.get(kind) ?? [];
    positions.push(i);
    slotPositionsByKind.set(kind, positions);
  });

  const candidateRowsByKind = new Map<string, number[]>();
  kindAtRow.forEach((kind, row) => {
    if (!kind) return;
    const rows = candidateRowsByKind.get(kind) ?? [];
    rows.push(row);
    candidateRowsByKind.set(kind, rows);
  });

  for (const [kind, positions] of slotPositionsByKind) {
    const rows = candidateRowsByKind.get(kind);
    // Counts must agree exactly: too few candidates means nothing to assign,
    // too many means which-is-which is genuinely ambiguous -- either way,
    // leave this kind's slots at their naive positional default instead of
    // guessing.
    if (!rows || rows.length !== positions.length) continue;
    positions.forEach((slotIndex, j) => {
      inputs[slotIndex] = { type: "cell_index", index: rows[j] };
    });
  }

  return inputs;
}

/** The template.* node types split into the picker's sub-groups, in display
 * order: named categories alphabetically, uncategorized last.
 *
 * A `<select>` can't nest optgroups, so a sub-group is a group of its own with
 * a compound label -- which also means the split has to disappear entirely
 * when there's only one group, or every list would grow a redundant "Templates
 * · Other" header. That's what the third tuple element says.
 *
 * Order within a group stays the server's (creation order), so a type doesn't
 * move around in the list beyond the one grouping.
 */
export function groupTemplatesByCategory(templates: NodeTemplate[]): [string, NodeTemplate[], boolean][] {
  const byCategory = new Map<string, NodeTemplate[]>();
  for (const template of templates) {
    if (template.node_type.startsWith("native.")) continue;
    const category = template.category?.trim() ?? "";
    const bucket = byCategory.get(category);
    if (bucket) bucket.push(template);
    else byCategory.set(category, [template]);
  }
  const groups = [...byCategory.entries()].sort(([a], [b]) => (a === "" ? 1 : b === "" ? -1 : a.localeCompare(b)));
  return groups.map(([category, items]) => [category, items, groups.length === 1]);
}
