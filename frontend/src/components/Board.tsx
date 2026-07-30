import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { assetsApi, boardApi } from "../api/endpoints";
import { inkTransform, pathBounds, withInkBoxes } from "../boardGeometry";
import type { Board as BoardType, BoardItem, BoardItemKind } from "../types";
import { useT } from "../i18n";
import { usePinchPan } from "../usePinchPan";
import { cx } from "../utils";
import { BoardSticker } from "./BoardSticker";

/** The idea board: pre-production, the half of the pipeline the grid can't hold.
 *
 * The grid is convergent by construction (column parity, creator/output
 * binding, one asset per cell) and that's exactly what makes it wrong for
 * ideation. Here a sticker's x/y is its only truth -- nothing derives it,
 * nothing validates it against a layout. See roadmap.md §1.
 *
 * Two bridges keep this from being a whiteboard bolted on the side: the grid
 * can reference this board's media (asset.refasset, one-way), and a node's
 * prompt can pull a text sticker's words in (the "from ideas" picker).
 */

export const STICKER_COLORS = ["#f6d36b", "#8fd6a0", "#8fc4f0", "#e39ad4", "#f0a37a", "#c9c9c9"];

/** "pen" and "circle" are two shapes of the same drawing tool -- a circle is a
 * ready-made stroke you drag out instead of tracing by hand, not a separate
 * kind of object. "erase" is the only thing that deletes drawn marks; clicking
 * a stroke while drawing used to delete it, which made drawing feel like it
 * was fighting back. */
type Tool = "select" | "pen" | "circle" | "erase";

const DRAWING_TOOLS: Tool[] = ["pen", "circle", "erase"];

interface Props {
  projectId: string;
}

export function Board({ projectId }: Props) {
  const t = useT();
  const [board, setBoard] = useState<BoardType | null>(null);
  const [items, setItems] = useState<BoardItem[]>([]);
  const [tool, setTool] = useState<Tool>("select");
  const [inkColor, setInkColor] = useState(STICKER_COLORS[0]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  // The note created last, so it can open with the cursor already in it.
  const [autoEditId, setAutoEditId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // panAtMinZoom: the board extends past its frame at every zoom level, unlike
  // the image viewers this hook was written for, where min zoom means "fits".
  const { view, grabbing, reset, zoomBy, handlers } = usePinchPan(containerRef, tool === "select", {
    minZoom: 0.2,
    maxZoom: 4,
    panAtMinZoom: true,
  });

  const load = useCallback(async () => {
    const b = await boardApi.get(projectId);
    setBoard(b);
    setItems(withInkBoxes(await boardApi.items(b.id)));
  }, [projectId]);

  useEffect(() => {
    load().catch((e) => setError(String(e)));
  }, [load]);

  /** Client point -> board coordinates. Every creation gesture needs this, and
   * getting it wrong is invisible until you're zoomed or panned. */
  const toBoard = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } => {
      const el = containerRef.current;
      if (!el) return { x: 0, y: 0 };
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      return {
        x: (clientX - cx - view.x) / view.zoom + rect.width / 2,
        y: (clientY - cy - view.y) / view.zoom + rect.height / 2,
      };
    },
    [view],
  );

  /** Where a new sticker goes: the middle of what you're currently looking at,
   * nudged until it isn't sitting exactly on top of something else. Everything
   * used to be created at a fixed (60, 60), so a second "+ note" landed
   * pixel-perfectly on the first -- deleting the top one just revealed an
   * identical one underneath and looked like the button had done nothing. */
  const freeSpot = (w = 220, h = 180): { x: number; y: number } => {
    const el = containerRef.current;
    const rect = el?.getBoundingClientRect();
    const centre = rect
      ? toBoard(rect.left + rect.width / 2, rect.top + rect.height / 2)
      : { x: 60 + w / 2, y: 60 + h / 2 };
    let x = centre.x - w / 2;
    let y = centre.y - h / 2;
    while (items.some((i) => Math.abs(i.x - x) < 24 && Math.abs(i.y - y) < 24)) {
      x += 28;
      y += 28;
    }
    return { x, y };
  };

  const create = async (data: Partial<BoardItem> & { kind: BoardItemKind }) => {
    if (!board) return null;
    try {
      const created = await boardApi.createItem(board.id, data);
      setItems((prev) => [...prev, ...withInkBoxes([created])]);
      if (created.kind === "text") setAutoEditId(created.id);
      return created;
    } catch (e) {
      setError(String(e));
      return null;
    }
  };

  /** In-progress geometry from a drag/resize: local only, no request. Held here
   * rather than inside the sticker because a frame's outline is drawn in the
   * shared SVG layer below and has to follow the grips in real time. */
  const live = (id: string, data: Partial<BoardItem>) =>
    setItems((prev) => prev.map((i) => (i.id === id ? { ...i, ...data } : i)));

  /** Local state first, then the PATCH: dragging writes x/y many times a
   * second and must not wait on a round-trip to look responsive. */
  const patch = async (id: string, data: Partial<BoardItem>) => {
    setItems((prev) => prev.map((i) => (i.id === id ? { ...i, ...data } : i)));
    try {
      const saved = await boardApi.updateItem(id, data);
      setItems((prev) => prev.map((i) => (i.id === id ? saved : i)));
    } catch (e) {
      setError(String(e));
      await load();
    }
  };

  /** Tags live on the library Asset, not on the sticker -- a different endpoint
   * and a different object, so it can't go through `patch` above. The sticker's
   * own copy is updated locally to avoid refetching the whole board for a label. */
  const setAssetTags = async (item: BoardItem, tags: string[]) => {
    if (!item.asset_id) return;
    try {
      const saved = await assetsApi.setTags(item.asset_id, tags);
      const next = saved.tags ?? [];
      setItems((prev) => prev.map((i) => (i.asset_id === item.asset_id ? { ...i, asset_tags: next } : i)));
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (id: string) => {
    await boardApi.removeItem(id);
    // Connectors and comments attached to it cascade server-side, so the whole
    // list is refetched rather than guessing which local rows also died.
    await load();
    setSelectedIds((prev) => prev.filter((s) => s !== id));
  };

  const uploadFiles = async (files: FileList | File[], at?: { x: number; y: number }) => {
    if (!board) return;
    let offset = 0;
    for (const file of Array.from(files)) {
      try {
        const asset = await boardApi.uploadAsset(projectId, file);
        const kind: BoardItemKind = file.type.startsWith("audio/")
          ? "audio"
          : file.type.startsWith("video/")
            ? "video"
            : "image";
        const w = kind === "audio" ? 260 : 240;
        const spot = at ?? freeSpot(w, kind === "audio" ? 90 : 200);
        await create({
          kind,
          asset_id: asset.id,
          x: spot.x + offset,
          y: spot.y + offset,
          w,
          h: kind === "audio" ? 90 : 200,
        });
        offset += 24;
      } catch (e) {
        setError(String(e));
      }
    }
  };

  // ---- freehand ink ----
  const [stroke, setStroke] = useState<{ x: number; y: number }[]>([]);
  const drawing = useRef(false);

  const inkPointerDown = (e: React.PointerEvent) => {
    if (tool !== "pen") return;
    drawing.current = true;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const p = toBoard(e.clientX, e.clientY);
    setStroke([p]);
  };

  const inkPointerMove = (e: React.PointerEvent) => {
    if (!drawing.current) return;
    setStroke((prev) => [...prev, toBoard(e.clientX, e.clientY)]);
  };

  const inkPointerUp = async () => {
    if (!drawing.current) return;
    drawing.current = false;
    const points = stroke;
    setStroke([]);
    if (points.length < 2) return;
    // Stored in board coordinates as an absolute path; the sticker itself has
    // no box, so erasing is deleting the row -- there is no raster layer and
    // no per-pixel eraser (roadmap.md §1).
    const d = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
    // The box starts out equal to the path's own bounds, so the render
    // transform is an identity until the stroke is actually moved or scaled.
    const box = pathBounds(d) ?? { x: 0, y: 0, w: 0, h: 0 };
    await create({ kind: "ink", path: d, color: inkColor, stroke_width: 3, ...box });
  };

  // ---- frame drag-out ----
  const [frameDraft, setFrameDraft] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  const framePointerDown = (e: React.PointerEvent) => {
    if (tool !== "circle") return;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    const p = toBoard(e.clientX, e.clientY);
    setFrameDraft({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
  };

  const framePointerMove = (e: React.PointerEvent) => {
    if (!frameDraft) return;
    const p = toBoard(e.clientX, e.clientY);
    setFrameDraft((d) => (d ? { ...d, x1: p.x, y1: p.y } : d));
  };

  const framePointerUp = async () => {
    if (!frameDraft) return;
    const { x0, y0, x1, y1 } = frameDraft;
    setFrameDraft(null);
    const w = Math.abs(x1 - x0);
    const h = Math.abs(y1 - y0);
    if (w < 20 || h < 20) return;
    await create({ kind: "frame", shape: "ellipse", x: Math.min(x0, x1), y: Math.min(y0, y1), w, h, color: inkColor });
  };

  // ---- eraser ----
  // Whole marks, never pixels: a stroke or a circle is one row, so erasing it is
  // deleting that row. A pixel eraser would drag in a raster layer and an undo
  // stack for an outcome this already gives (roadmap.md §1).
  const erase = (id: string) => {
    if (tool !== "erase") return;
    void remove(id);
  };

  // ---- connectors ----
  const connectSelected = async () => {
    if (selectedIds.length !== 2) return;
    await create({ kind: "connector", source_item_id: selectedIds[0], target_item_id: selectedIds[1] });
    setSelectedIds([]);
  };

  const commentOnSelected = async () => {
    if (selectedIds.length !== 1) return;
    await create({ kind: "comment", target_item_id: selectedIds[0], text: "", w: 200, h: 70 });
  };

  const byId = useMemo(() => new Map(items.map((i) => [i.id, i])), [items]);

  // Connectors are drawn straight from stored coordinates. Deliberately NOT via
  // ArrowsOverlay: that one measures the DOM and re-polls on a 500ms interval,
  // which is fine for the grid but visibly lags a board where items are dragged.
  const connectors = useMemo(
    () =>
      items
        .filter((i) => i.kind === "connector")
        .map((c) => {
          const from = c.source_item_id ? byId.get(c.source_item_id) : undefined;
          const to = c.target_item_id ? byId.get(c.target_item_id) : undefined;
          if (!from || !to) return null;
          const x1 = from.x + from.w / 2;
          const y1 = from.y + from.h / 2;
          const x2 = to.x + to.w / 2;
          const y2 = to.y + to.h / 2;
          const midX = (x1 + x2) / 2;
          return { id: c.id, d: `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`, color: c.color };
        })
        .filter((c): c is { id: string; d: string; color: string | null } => c !== null),
    [items, byId],
  );

  const inkItems = items.filter((i) => i.kind === "ink");
  // Frames render as shapes in the SVG layer; the sticker component only
  // contributes their move/resize grips (and only in select mode). Strokes are
  // in that same group now -- a drawn mark is as worth repositioning as a
  // circle, and redrawing it from scratch was the only alternative.
  const frameItems = items.filter((i) => i.kind === "frame");
  const stickers = items.filter((i) => i.kind !== "connector" && i.kind !== "comment");
  const commentsByTarget = useMemo(() => {
    const map = new Map<string, BoardItem[]>();
    for (const c of items.filter((i) => i.kind === "comment")) {
      if (!c.target_item_id) continue;
      map.set(c.target_item_id, [...(map.get(c.target_item_id) ?? []), c]);
    }
    return map;
  }, [items]);

  /** A plain press selects, full stop -- it does not toggle. Toggling meant
   * that pressing an already-selected sticker to drag it also deselected it,
   * so the thing you were holding lost its outline mid-gesture. Shift/ctrl is
   * where adding and removing lives; empty canvas is where clearing lives. */
  /** True when a press landed on bare canvas rather than on anything drawn.
   * Testing `target === containerRef.current` does NOT work: `.board-plane`
   * covers the container edge to edge, so the target of an empty-space click is
   * always the plane (or the SVG layer above it) and never the container --
   * which is why double-clicking empty canvas to add a note did nothing at all. */
  const isBackground = (target: EventTarget | null): boolean => {
    const el = target as HTMLElement | null;
    if (!el) return false;
    return el === containerRef.current || el.classList.contains("board-plane") || el.classList.contains("board-svg");
  };

  const toggleSelected = (id: string, additive: boolean) =>
    setSelectedIds((prev) => (additive ? (prev.includes(id) ? prev.filter((s) => s !== id) : [...prev, id]) : [id]));

  const strokePath = stroke.length > 1 ? stroke.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x} ${p.y}`).join(" ") : null;

  return (
    <div className="main-area board-area">
      <div className="board-toolbar">
        <button className={cx(tool === "select" && "active")} onClick={() => setTool("select")} title={t("board.selectTitle")}>
          ✋ {t("board.select")}
        </button>

        {/* One drawing tool with two shapes plus its eraser -- a circle is a
            ready-made stroke, not a different kind of object, so it belongs in
            this group rather than standing on its own. */}
        <span className="board-tool-group">
          <button className={cx(tool === "pen" && "active")} onClick={() => setTool("pen")} title={t("board.penTitle")}>
            ✎
          </button>
          <button className={cx(tool === "circle" && "active")} onClick={() => setTool("circle")} title={t("board.circleTitle")}>
            ◯
          </button>
          <button className={cx(tool === "erase" && "active")} onClick={() => setTool("erase")} title={t("board.eraseTitle")}>
            ⌫
          </button>
        </span>

        <span className="board-colors">
          {STICKER_COLORS.map((c) => (
            <button
              key={c}
              className={cx("board-color", inkColor === c && "active")}
              style={{ background: c }}
              onClick={() => setInkColor(c)}
              title={t("board.colorTitle")}
            />
          ))}
        </span>

        <span className="board-toolbar-sep" />

        <button onClick={() => create({ kind: "text", ...freeSpot(), color: inkColor, text: "" })}>{t("board.addNote")}</button>
        <button onClick={() => fileInputRef.current?.click()}>{t("board.addMedia")}</button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,audio/*,video/*"
          style={{ display: "none" }}
          onChange={(e) => {
            if (e.target.files) void uploadFiles(e.target.files);
            e.target.value = "";
          }}
        />
        {selectedIds.length === 2 && <button onClick={connectSelected}>{t("board.connect")}</button>}
        {selectedIds.length === 1 && <button onClick={commentOnSelected}>{t("board.comment")}</button>}
        {selectedIds.length > 0 && <button onClick={() => setSelectedIds([])}>{t("board.clearSelection", { n: selectedIds.length })}</button>}

        <span className="board-toolbar-sep" />
        <span className="board-zoom">
          {Math.round(view.zoom * 100)}%
          <button onClick={() => zoomBy(1 / 1.2)}>−</button>
          <button onClick={() => zoomBy(1.2)}>+</button>
          <button onClick={reset}>{t("common.reset")}</button>
        </span>
        {error && (
          <span className="board-error" onClick={() => setError(null)} title={t("board.dismissError")}>
            {error}
          </span>
        )}
      </div>

      <div
        ref={containerRef}
        className={cx("board-canvas", grabbing && "grabbing", DRAWING_TOOLS.includes(tool) && "drawing", tool === "erase" && "erasing")}
        {...handlers}
        onPointerDown={(e) => {
          // Reaching the canvas at all means the press missed every sticker
          // (they stop propagation), so it's the natural place to clear.
          if (tool === "select" && isBackground(e.target)) setSelectedIds([]);
          handlers.onPointerDown(e);
          framePointerDown(e);
          inkPointerDown(e);
        }}
        onPointerMove={(e) => {
          handlers.onPointerMove(e);
          framePointerMove(e);
          inkPointerMove(e);
        }}
        onPointerUp={(e) => {
          handlers.onPointerUp(e);
          void framePointerUp();
          void inkPointerUp();
        }}
        onPointerCancel={handlers.onPointerCancel}
        onDoubleClick={(e) => {
          if (tool !== "select" || !isBackground(e.target)) return;
          const p = toBoard(e.clientX, e.clientY);
          void create({ kind: "text", x: p.x, y: p.y, color: inkColor, text: "" });
        }}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          if (e.dataTransfer.files.length) void uploadFiles(e.dataTransfer.files, toBoard(e.clientX, e.clientY));
        }}
      >
        <div
          className="board-plane"
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}
        >
          {/* Offset left/top by half its own size with a matching viewBox origin,
              so a stroke at a negative board coordinate still renders instead of
              being clipped at the plane's edge. */}
          <svg className="board-svg" viewBox="-5000 -5000 10000 10000">
            {/* Freehand strokes and circles are both just marks in this layer,
                erased the same way. Each gets a second, invisible copy with a
                fat stroke: a 3px line is nearly impossible to hit with a mouse,
                and only the eraser turns those hit targets on -- otherwise a
                stray click while drawing would delete what's underneath. */}
            {inkItems.map((i) => (
              // vectorEffect keeps the line the same visible thickness however
              // the box is stretched -- scaling a wide box would otherwise make
              // one axis of the stroke fat and the other thin.
              <g key={i.id} transform={inkTransform(i)} vectorEffect="non-scaling-stroke">
                <path
                  d={i.path ?? ""}
                  fill="none"
                  stroke={i.color ?? "#ddd"}
                  strokeWidth={i.stroke_width ?? 3}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                />
                {tool === "erase" && (
                  <path d={i.path ?? ""} className="board-erasable" fill="none" onClick={() => erase(i.id)} />
                )}
              </g>
            ))}
            {frameItems.map((f) =>
              f.shape === "rect" ? (
                <g key={f.id}>
                  <rect x={f.x} y={f.y} width={f.w} height={f.h} rx={10} fill="none" stroke={f.color ?? "var(--accent)"} strokeWidth={2} />
                  {tool === "erase" && (
                    <rect
                      x={f.x}
                      y={f.y}
                      width={f.w}
                      height={f.h}
                      rx={10}
                      className="board-erasable"
                      fill="none"
                      onClick={() => erase(f.id)}
                    />
                  )}
                </g>
              ) : (
                <g key={f.id}>
                  <ellipse
                    cx={f.x + f.w / 2}
                    cy={f.y + f.h / 2}
                    rx={f.w / 2}
                    ry={f.h / 2}
                    fill="none"
                    stroke={f.color ?? "var(--accent)"}
                    strokeWidth={2}
                  />
                  {tool === "erase" && (
                    <ellipse
                      cx={f.x + f.w / 2}
                      cy={f.y + f.h / 2}
                      rx={f.w / 2}
                      ry={f.h / 2}
                      className="board-erasable"
                      fill="none"
                      onClick={() => erase(f.id)}
                    />
                  )}
                </g>
              ),
            )}
            {connectors.map((c) => (
              <path key={c.id} d={c.d} fill="none" stroke={c.color ?? "var(--accent)"} strokeWidth={2} markerEnd="url(#board-arrow)" />
            ))}
            {strokePath && <path d={strokePath} fill="none" stroke={inkColor} strokeWidth={3} strokeLinecap="round" />}
            {frameDraft && (
              <ellipse
                cx={(frameDraft.x0 + frameDraft.x1) / 2}
                cy={(frameDraft.y0 + frameDraft.y1) / 2}
                rx={Math.abs(frameDraft.x1 - frameDraft.x0) / 2}
                ry={Math.abs(frameDraft.y1 - frameDraft.y0) / 2}
                fill="none"
                stroke={inkColor}
                strokeDasharray="6 4"
              />
            )}
            <defs>
              <marker id="board-arrow" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">
                <path d="M0,0 L6,3 L0,6 Z" fill="var(--accent)" />
              </marker>
            </defs>
          </svg>

          {stickers.map((item) => (
            <BoardSticker
              key={item.id}
              item={item}
              comments={commentsByTarget.get(item.id) ?? []}
              zoom={view.zoom}
              selected={selectedIds.includes(item.id)}
              autoEdit={item.id === autoEditId}
              interactive={tool === "select"}
              onSelect={(additive) => toggleSelected(item.id, additive)}
              onLive={live}
              onPatch={patch}
              onSetAssetTags={setAssetTags}
              onRemove={remove}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
