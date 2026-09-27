import { useT } from "../i18n";
import type { Annotation } from "../types";
import { cx } from "../utils";
import { threadState } from "./AnnotationThread";

interface Props {
  annotation: Annotation;
  // Bounding box in grid coordinates, derived by the caller from where the
  // member nodes currently sit -- nothing positional is stored on the
  // annotation itself.
  box: { minRow: number; maxRow: number; minCol: number; maxCol: number };
  // Which share of the top edge the label gets when several frames start in
  // the same cell (Grid.tsx's annotationBoxes); count 1 means the whole edge.
  labelSlot: { index: number; count: number };
  onOpen: () => void;
}

/** A comment thread's frame on the grid. The label shows where the thread
 * stands -- its latest message, coloured by who wrote it, or a quiet ✓ once
 * it's resolved -- and opens the conversation itself (AnnotationThread). */
export function AnnotationFrame({ annotation, box, labelSlot, onOpen }: Props) {
  const t = useT();
  const state = threadState(annotation);
  const last = annotation.messages[annotation.messages.length - 1];
  const count = annotation.messages.length;
  const title = [
    last ? `${last.source === "agent" ? t("annotation.agent") : t("annotation.you")}: ${last.text}` : t("annotation.empty"),
    count > 1 ? t("annotation.messageCount", { n: count }) : null,
    state === "resolved"
      ? annotation.resolved_by === "agent"
        ? t("annotation.resolvedByAgent")
        : t("annotation.resolvedByYou")
      : null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div
      // A grid item spanning its members' rows/columns, not an absolutely
      // positioned overlay: the grid itself then keeps the frame aligned at
      // any zoom level, with no coordinate math to drift.
      className={cx("annotation-frame", `annotation-${state}`)}
      style={{
        gridRow: `${box.minRow + 1} / span ${box.maxRow - box.minRow + 1}`,
        gridColumn: `${box.minCol + 2} / span ${box.maxCol - box.minCol + 1}`,
      }}
    >
      {/* The frame body must not eat clicks meant for the cells inside it
          (pointer-events: none in CSS); only this label is interactive. */}
      <button
        className="annotation-label"
        onClick={onOpen}
        title={title}
        style={
          labelSlot.count > 1
            ? {
                left: `calc(10px + ${labelSlot.index} * (100% - 20px) / ${labelSlot.count})`,
                maxWidth: `calc((100% - 20px) / ${labelSlot.count} - 4px)`,
              }
            : undefined
        }
      >
        {state === "resolved" && <span className="annotation-mark">✓</span>}
        <span className="annotation-text">{last?.text || t("annotation.empty")}</span>
        {count > 1 && <span className="annotation-count">{count}</span>}
      </button>
    </div>
  );
}
