/** Is this drag carrying files from outside the page (OS file manager,
 * desktop, another app) rather than being one of the grid's own internal
 * drags?
 *
 * The grid's internal asset/workflow moves are plain HTML5 drags that never
 * call setData, so their dataTransfer.types is empty -- an external file drag
 * always lists "Files". dataTransfer.files itself is deliberately NOT used
 * here: it's empty during dragenter/dragover in every browser (only readable
 * on drop), so a dragover handler that checked it would never light up a drop
 * target.
 *
 * Shared by NodeCell.tsx (dropping onto an existing empty asset cell) and
 * Grid.tsx (dropping onto an empty grid cell, which creates the cell first) --
 * the two do different things with the drop but must agree on what counts as
 * a file drag in the first place.
 */
export function isFileDrag(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false;
  return Array.from(dataTransfer.types).includes("Files");
}
