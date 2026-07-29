import { useEffect, useRef, useState } from "react";
import { resolveAssetUrl } from "../api/client";
import { renderMarkdown } from "../markdown";
import type { BoardItem } from "../types";
import { cx } from "../utils";
import { STICKER_COLORS } from "./Board";

// Floors for the resize handle. A card can't go below the size of its own
// action bar and handle; a drawn mark has neither, and a small doodle would be
// unshrinkable at the card's floor.
const MIN_CARD_W = 120;
const MIN_CARD_H = 60;
const MIN_MARK = 16;

/** Anything inside a sticker that has its own click behaviour. A pointerdown
 * landing on one of these must NOT start a drag: `startDrag` calls
 * setPointerCapture on the sticker, and a captured pointer retargets the
 * subsequent events at the capturing element, so the button under the finger
 * never receives its click. That's what made the ⋯ and × buttons fire only
 * sometimes -- and never at all once two stickers overlapped, since the drag
 * also swallowed the pointerup.
 *
 * `.board-sticker-text` is in here for the same reason: it's a text field, and
 * a field you might drag by is a field whose click can't be trusted. Dragging
 * a note is the header's job instead. */
const INTERACTIVE_INSIDE =
  "button, input, textarea, select, a, audio, video, .board-resize, .board-sticker-meta, .board-comments, .board-sticker-text";

/** One sticker. Holds exactly one kind of content -- there is no mixed card --
 * so `item.kind` is also the content-type discriminator.
 *
 * Geometry during a drag/resize goes through `onLive`, which only touches the
 * board's local state; the single PATCH happens on pointerup. It's reported
 * upward rather than kept here because a `frame`'s visible outline is drawn in
 * the board's shared SVG layer (it has to be, so the eraser can hit the outline
 * and only the outline) -- so the board, not this component, needs the
 * in-progress geometry to render it.
 */

interface Props {
  item: BoardItem;
  comments: BoardItem[];
  zoom: number;
  selected: boolean;
  interactive: boolean;
  /** True for a note that was just created, so it opens with the cursor
   * already in it -- creating a note and then having to discover how to type
   * into it is a step nobody should have to take. */
  autoEdit: boolean;
  onSelect: (additive: boolean) => void;
  onLive: (id: string, data: Partial<BoardItem>) => void;
  onPatch: (id: string, data: Partial<BoardItem>) => Promise<void>;
  onSetAssetTags: (item: BoardItem, tags: string[]) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
}

export function BoardSticker({
  item,
  comments,
  zoom,
  selected,
  interactive,
  autoEdit,
  onSelect,
  onLive,
  onPatch,
  onSetAssetTags,
  onRemove,
}: Props) {
  const [editing, setEditing] = useState(autoEdit);
  const [draft, setDraft] = useState(item.text);
  const [showMeta, setShowMeta] = useState(false);
  const dragRef = useRef<{
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const resizeRef = useRef<{ startX: number; startY: number; originW: number; originH: number } | null>(null);

  useEffect(() => setDraft(item.text), [item.text]);

  const isMark = item.kind === "frame" || item.kind === "ink";

  const startDrag = (e: React.PointerEvent) => {
    // While a drawing tool is active the press has to reach the canvas, so you
    // can draw straight over a sticker.
    if (!interactive) return;
    // Otherwise stop it reaching the canvas no matter what it landed on --
    // pressing a button or clicking into a textarea would otherwise also start
    // a pan and slide the board out from under it.
    e.stopPropagation();
    // No `editing` guard here. There used to be one, and since a freshly
    // created note opens straight into its editor, it left every new note
    // completely undraggable and unselectable until you clicked away. It was
    // redundant anyway: `textarea` is in the exclusion list below, so typing
    // can't be interrupted by a drag no matter what.
    if ((e.target as HTMLElement).closest(INTERACTIVE_INSIDE)) return;
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { startX: e.clientX, startY: e.clientY, originX: item.x, originY: item.y };
    onSelect(e.shiftKey || e.ctrlKey || e.metaKey);
  };

  const onDrag = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    e.stopPropagation();
    // Divided by zoom: the pointer moves in screen pixels, the sticker lives in
    // board coordinates, and at 50% zoom a 100px drag is 200 board units.
    onLive(item.id, { x: d.originX + (e.clientX - d.startX) / zoom, y: d.originY + (e.clientY - d.startY) / zoom });
  };

  const endDrag = async (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    dragRef.current = null;
    e.stopPropagation();
    // A press that didn't move is a click, not a reposition -- no PATCH.
    if (Math.abs(e.clientX - d.startX) < 2 && Math.abs(e.clientY - d.startY) < 2) return;
    await onPatch(item.id, { x: item.x, y: item.y });
  };

  const startResize = (e: React.PointerEvent) => {
    if (!interactive) return;
    e.stopPropagation();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    resizeRef.current = { startX: e.clientX, startY: e.clientY, originW: item.w, originH: item.h };
  };

  const onResize = (e: React.PointerEvent) => {
    const r = resizeRef.current;
    if (!r) return;
    e.stopPropagation();
    const minW = isMark ? MIN_MARK : MIN_CARD_W;
    const minH = isMark ? MIN_MARK : MIN_CARD_H;
    onLive(item.id, {
      w: Math.max(minW, r.originW + (e.clientX - r.startX) / zoom),
      h: Math.max(minH, r.originH + (e.clientY - r.startY) / zoom),
    });
  };

  const endResize = async (e: React.PointerEvent) => {
    if (!resizeRef.current) return;
    resizeRef.current = null;
    e.stopPropagation();
    await onPatch(item.id, { w: item.w, h: item.h });
  };

  const resizeHandle = interactive ? (
    <span
      className="board-resize"
      onPointerDown={startResize}
      onPointerMove={onResize}
      onPointerUp={endResize}
      onPointerCancel={endResize}
      title="Потягни, щоб змінити розмір"
    />
  ) : null;

  const saveText = async () => {
    setEditing(false);
    if (draft !== item.text) await onPatch(item.id, { text: draft });
  };

  const url = resolveAssetUrl(item.asset_url);
  const isMedia = item.kind === "image" || item.kind === "audio" || item.kind === "video";

  if (isMark) {
    // A drawn mark, not a card: the shape itself -- circle or freehand stroke
    // alike -- is rendered in the board's SVG layer, so the eraser hits it the
    // same way and it never swallows clicks meant for the stickers it covers.
    // All that lives here is the invisible box carrying its two grips, and only
    // while the select tool is active. A stroke's box is placed over the path's
    // own bounds rather than baked into the path (see boardGeometry.ts).
    if (!interactive) return null;
    return (
      <div className="board-frame-grips" style={{ left: item.x, top: item.y, width: item.w, height: item.h }}>
        <span
          className="board-grip"
          onPointerDown={startDrag}
          onPointerMove={onDrag}
          onPointerUp={endDrag}
          title="Потягни, щоб пересунути. Видаляється гумкою"
        />
        {resizeHandle}
      </div>
    );
  }

  return (
    <div
      // "board-kind-<kind>", NOT "board-sticker-<kind>": for a text note the
      // latter produces "board-sticker-text" -- the exact class the note's own
      // body field uses. The root then matched every rule and every selector
      // meant for the field, including the exclusion list above, so a text note
      // could not be dragged or selected from anywhere on it while media
      // stickers were fine. A modifier namespace that can collide with a child's
      // class is a trap; this one can't.
      className={cx("board-sticker", `board-kind-${item.kind}`, selected && "selected", item.source === "agent" && "from-agent")}
      style={{ left: item.x, top: item.y, width: item.w, minHeight: item.h, background: item.color ?? undefined, zIndex: item.z }}
      onPointerDown={startDrag}
      onPointerMove={onDrag}
      onPointerUp={endDrag}
    >
      {/* The header is the drag handle. Everything below it belongs to the
          content -- a note's body is a text field, and a field you might drag
          by is a field you can't reliably click into. */}
      <div className="board-sticker-bar" title="Потягни за шапку, щоб пересунути">
        <span className="board-drag-dots" aria-hidden="true">
          ⠿
        </span>
        {item.tag && <span className="board-tag" title="Доступний у промті як {tag}">{`{${item.tag}}`}</span>}
        {isMedia && item.asset_tags.length > 0 && (
          <span className="board-asset-tags" title="За цими мітками фільтрує пікер референсів у гріді">
            {item.asset_tags.join(" · ")}
          </span>
        )}
        <span className="board-sticker-actions">
          <button onClick={() => setShowMeta((v) => !v)} title="Колір і мітки">
            ⋯
          </button>
          <button onClick={() => void onRemove(item.id)} title="Видалити стікер">
            ×
          </button>
        </span>
      </div>

      {showMeta && (
        <div className="board-sticker-meta">
          <div className="board-colors">
            {STICKER_COLORS.map((c) => (
              <button key={c} className="board-color" style={{ background: c }} onClick={() => void onPatch(item.id, { color: c })} />
            ))}
          </div>
          {item.kind === "text" && (
            <label className="board-tag-field">
              tag
              <input
                defaultValue={item.tag ?? ""}
                placeholder="head"
                title="Хендл для макросу {tag} у промті. Унікальний у межах проєкту."
                onBlur={(e) => {
                  const next = e.target.value.trim();
                  if (next !== (item.tag ?? "")) void onPatch(item.id, { tag: next });
                }}
              />
            </label>
          )}
          {/* A media sticker's labels live on the underlying library ASSET, not
              on the sticker -- which is why they're set here, on the board that
              owns the asset, and read over in the grid's reference picker to
              filter it. Not to be confused with a text sticker's `tag` above:
              that one is a single unique prompt-macro handle. */}
          {isMedia && (
            <label className="board-tag-field board-tag-field-wide">
              теги
              <input
                key={item.asset_tags.join(",")}
                defaultValue={item.asset_tags.join(", ")}
                placeholder="обличчя, костюм"
                title="Мітки для фільтра в пікері референсів. Через кому."
                onBlur={(e) => {
                  const next = e.target.value
                    .split(",")
                    .map((s) => s.trim())
                    .filter(Boolean);
                  if (next.join(",") !== item.asset_tags.join(",")) void onSetAssetTags(item, next);
                }}
              />
            </label>
          )}
        </div>
      )}

      {item.kind === "text" &&
        (editing ? (
          <textarea
            autoFocus
            className="board-sticker-editor"
            // autoFocus alone puts the caret at position 0, so typing on a note
            // that already has text prepends to it.
            onFocus={(e) => e.currentTarget.setSelectionRange(draft.length, draft.length)}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={saveText}
            placeholder="Введіть будь-який вміст, який потрібно запам'ятати"
          />
        ) : (
          <div
            className="board-sticker-text"
            onClick={() => setEditing(true)}
            title="Клікни, щоб писати"
            // renderMarkdown escapes before it formats, so nothing in a sticker
            // (including one an agent wrote) can turn into live markup.
            dangerouslySetInnerHTML={{
              __html: renderMarkdown(item.text) || "<p class='board-empty'>Клікни, щоб писати…</p>",
            }}
          />
        ))}

      {item.kind === "image" && url && <img src={url} alt="" draggable={false} />}
      {item.kind === "audio" && url && <audio src={url} controls />}
      {item.kind === "video" && url && <video src={url} controls />}

      {resizeHandle}

      {comments.length > 0 && (
        <div className="board-comments">
          {comments.map((c) => (
            <div key={c.id} className={cx("board-comment", c.source === "agent" && "from-agent")}>
              <textarea
                defaultValue={c.text}
                placeholder="коментар"
                onBlur={(e) => {
                  if (e.target.value !== c.text) void onPatch(c.id, { text: e.target.value });
                }}
              />
              <button onClick={() => void onRemove(c.id)} title="Видалити коментар">
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
