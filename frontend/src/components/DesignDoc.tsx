import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { resolveAssetPreviewUrl, resolveAssetUrl } from "../api/client";
import { assetsApi, boardApi, designDocApi } from "../api/endpoints";
import { assetClipboard, subgraphClipboard, useClipboardSlot } from "../clipboard";
import { useLangStore, useT } from "../i18n";
import { findRefs, type RenderRef, renderMarkdown } from "../markdown";
import type { BoardItem, DesignDocRef } from "../types";
import { FullSizeModal } from "./NodeCell";

/** The project's design doc: one markdown page per project, kept on the site
 * next to the grid and the board it describes (routes/design_docs.py).
 *
 * References into the project are plain markdown with a scheme instead of a
 * URL -- `![caption](node:<id>)` for a grid cell, `dashboard:<id>` for a subgraph's
 * current result, `board:<id>` for a sticker --
 * so the text stays ordinary markdown anywhere it's pasted, and the page shows
 * each cell as it is *now*. Grid cells come in through the cell's existing
 * "copy" button (clipboard.ts; a grid is too big to browse as a list), board
 * stickers through a picker, since a board is small enough to scan.
 *
 * Edits save themselves a moment after typing stops: this is mostly used from
 * a phone, where a forgotten Save button is a lost paragraph.
 *
 * There are two docs per project, Ukrainian and English, written side by side
 * rather than translated by the app. They open in the interface's language;
 * the UK/EN toggle switches between them. */

/** Mirrors `Lang` in backend/app/api/routes/design_docs.py. */
type DocLang = "uk" | "en";
const DOC_LANGS: DocLang[] = ["uk", "en"];

const SAVE_DELAY_MS = 1200;
const BOARD_KINDS = new Set(["text", "image", "audio", "video"]);

const toRenderRef = (ref: DesignDocRef): RenderRef => {
  const mime = ref.asset?.mime_type ?? null;
  const isImage = !!mime?.startsWith("image/");
  return {
    missing: ref.missing,
    label: ref.label,
    // A picture shows its thumbnail (the full file opens on click); video and
    // audio have no thumbnail to show, so they play the original.
    src: ref.asset ? (isImage ? resolveAssetPreviewUrl(ref.asset) : resolveAssetUrl(ref.asset.url)) : null,
    mime,
    text: ref.text,
  };
};

type SaveState = "saved" | "dirty" | "saving" | "error";

export function DesignDoc({ projectId }: { projectId: string }) {
  const t = useT();
  const [lang, setLang] = useState<DocLang>(useLangStore.getState().lang);
  const [content, setContent] = useState<string | null>(null);
  const [refs, setRefs] = useState<Record<string, DesignDocRef>>({});
  const [editing, setEditing] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [boardPickerOpen, setBoardPickerOpen] = useState(false);
  const [openRef, setOpenRef] = useState<DesignDocRef | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const copied = useClipboardSlot(assetClipboard);
  const copiedSubgraph = useClipboardSlot(subgraphClipboard);
  // Refs asked for but not answered yet, so a ref isn't re-requested on every
  // keystroke while its first request is still in flight.
  const requested = useRef(new Set<string>());

  // Which doc the latest load was for: a slow answer for the language just
  // switched away from must not land in the one switched to.
  const loadingFor = useRef("");

  const load = () => {
    const key = `${projectId}:${lang}`;
    loadingFor.current = key;
    return designDocApi.get(projectId, lang).then((doc) => {
      if (loadingFor.current !== key) return;
      setContent(doc.content);
      setRefs((prev) => ({ ...prev, ...doc.refs }));
      setSaveState("saved");
    });
  };

  useEffect(() => {
    setContent(null);
    setEditing(false);
    setRefs({});
    requested.current.clear();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  useEffect(() => {
    setContent(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, lang]);

  // Autosave. Keyed on the text itself, so the timer restarts on every edit.
  useEffect(() => {
    if (saveState !== "dirty" || content === null) return;
    const timer = setTimeout(() => {
      setSaveState("saving");
      designDocApi
        .save(projectId, lang, content)
        .then((doc) => {
          setRefs((prev) => ({ ...prev, ...doc.refs }));
          // Typing may have continued while the request was out.
          setSaveState((s) => (s === "saving" ? "saved" : s));
        })
        .catch(() => setSaveState("error"));
    }, SAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [content, saveState, projectId, lang]);

  // Live preview: resolve references the moment they appear in the text,
  // without waiting for a save.
  useEffect(() => {
    if (content === null) return;
    const unknown = findRefs(content).filter((r) => !(r in refs) && !requested.current.has(r));
    if (unknown.length === 0) return;
    unknown.forEach((r) => requested.current.add(r));
    designDocApi.resolveRefs(projectId, unknown).then((resolved) => setRefs((prev) => ({ ...prev, ...resolved })));
  }, [content, refs, projectId]);

  const html = useMemo(() => {
    const renderRefs = Object.fromEntries(Object.entries(refs).map(([k, v]) => [k, toRenderRef(v)]));
    return renderMarkdown(content ?? "", { headingOffset: 0, refs: renderRefs });
  }, [content, refs]);

  const edit = (next: string) => {
    setContent(next);
    setSaveState("dirty");
  };

  /** Inserts on a line of its own at the cursor (or at the end when the
   * textarea isn't open) -- a reference alone on its line renders as a block. */
  const insert = (snippet: string) => {
    const text = content ?? "";
    const el = textareaRef.current;
    const at = el && editing ? el.selectionStart : text.length;
    const before = text.slice(0, at);
    const after = text.slice(at);
    const lead = before && !before.endsWith("\n") ? "\n" : "";
    const trail = after.startsWith("\n") ? "" : "\n";
    edit(before + lead + snippet + trail + after);
    if (!editing) setEditing(true);
    requestAnimationFrame(() => {
      const pos = (before + lead + snippet + trail).length;
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(pos, pos);
    });
  };

  const insertCopiedCell = () => {
    if (!copied) return;
    insert(copied.nodeId ? `![${copied.label}](node:${copied.nodeId})` : `![${copied.label}](asset:${copied.assetId})`);
  };

  /** The dashboard itself rather than the pointer cell it was copied from --
   * the doc then follows whatever result is chosen inside it. */
  const insertCopiedSubgraph = () => {
    if (!copiedSubgraph) return;
    insert(`![${copiedSubgraph.name}](dashboard:${copiedSubgraph.dashboardId})`);
  };

  const onRenderedClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-ref]");
    if (!el) return;
    e.preventDefault();
    // A player's own controls sit inside the link; let them work.
    if ((e.target as HTMLElement).closest("video, audio")) return;
    const ref = refs[el.dataset.ref ?? ""];
    if (ref && !ref.missing) setOpenRef(ref);
  };

  const status = { saved: t("doc.saved"), dirty: t("doc.unsaved"), saving: t("doc.saving"), error: t("doc.saveError") }[saveState];

  return (
    <div className="main-area design-doc-page">
      <div className="design-doc-toolbar">
        {/* Switching with unsaved text would save it into the other language. */}
        <div className="design-doc-langs">
          {DOC_LANGS.map((l) => (
            <button key={l} className={lang === l ? "active" : ""} onClick={() => setLang(l)} disabled={saveState !== "saved"}>
              {l.toUpperCase()}
            </button>
          ))}
        </div>
        <button disabled={content === null || (editing && saveState !== "saved")} className={editing ? "active" : ""} onClick={() => (editing ? setEditing(false) : void load().then(() => setEditing(true)))}>
          {editing ? t("doc.done") : t("doc.edit")}
        </button>
        {editing && (
          <>
            <button onClick={() => setBoardPickerOpen(true)} title={t("doc.fromBoardTitle")}>
              {t("doc.fromBoard")}
            </button>
            <button onClick={insertCopiedCell} disabled={!copied} title={copied ? t("doc.pasteCellTitle", { label: copied.label }) : t("doc.pasteCellHint")}>
              {t("doc.pasteCell")}
            </button>
            <button onClick={insertCopiedSubgraph} disabled={!copiedSubgraph} title={copiedSubgraph ? t("doc.pasteSubgraphTitle", { name: copiedSubgraph.name }) : t("doc.pasteSubgraphHint")}>
              {t("doc.pasteSubgraph")}
            </button>
          </>
        )}
        <span className={`design-doc-status ${saveState}`}>{status}</span>
      </div>

      <div className={editing ? "design-doc-split" : undefined}>
        {editing && (
          <textarea
            ref={textareaRef}
            className="design-doc-editor"
            value={content ?? ""}
            onChange={(e) => edit(e.target.value)}
            placeholder={t("doc.placeholder")}
            spellCheck
          />
        )}
        {/* renderMarkdown escapes before it formats (see markdown.ts). */}
        <div className="design-doc-rendered markdown-body" onClick={onRenderedClick}>
          {content === null ? (
            <p className="design-doc-empty">{t("common.loading")}</p>
          ) : content.trim() ? (
            <div dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <p className="design-doc-empty">{t("doc.empty")}</p>
          )}
        </div>
      </div>

      {boardPickerOpen && (
        <BoardItemPicker
          projectId={projectId}
          onPick={(item) => {
            setBoardPickerOpen(false);
            insert(`![${item.tag ?? ""}](board:${item.id})`);
          }}
          onClose={() => setBoardPickerOpen(false)}
        />
      )}

      {openRef && <RefModal docRef={openRef} onClose={() => setOpenRef(null)} />}
    </div>
  );
}

/** A clicked reference: the picture full size and zoomable, or a text
 * sticker's note. */
function RefModal({ docRef, onClose }: { docRef: DesignDocRef; onClose: () => void }) {
  const t = useT();
  if (docRef.asset?.mime_type.startsWith("image/")) return <FullSizeModal url={resolveAssetUrl(docRef.asset.url)} onClose={onClose} />;
  if (!docRef.text) return null;
  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="params-modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="markdown-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(docRef.text, { headingOffset: 1 }) }} />
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
          <button onClick={onClose}>{t("common.close")}</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The board's stickers as a flat list -- pictures, notes and media; frames,
 * ink and connectors mean nothing outside the board. */
function BoardItemPicker({ projectId, onPick, onClose }: { projectId: string; onPick: (item: BoardItem) => void; onClose: () => void }) {
  const t = useT();
  const [items, setItems] = useState<BoardItem[] | null>(null);

  useEffect(() => {
    boardApi
      .get(projectId)
      .then((board) => boardApi.items(board.id))
      .then((all) => setItems(all.filter((i) => BOARD_KINDS.has(i.kind) && (i.asset_id || i.text.trim()))))
      .catch(() => setItems([]));
  }, [projectId]);

  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="params-modal-content reference-picker" onClick={(e) => e.stopPropagation()}>
        <h3>{t("doc.fromBoard")}</h3>
        {items === null ? (
          <p style={{ color: "var(--text-dim)" }}>{t("common.loading")}</p>
        ) : items.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>{t("doc.boardEmpty")}</p>
        ) : (
          <div className="reference-grid">
            {items.map((item) => (
              <button key={item.id} className="reference-item" onClick={() => onPick(item)} title={item.tag ?? ""}>
                {item.kind === "image" && item.asset_id ? (
                  <img src={assetsApi.previewUrl(item.asset_id)} alt="" loading="lazy" decoding="async" />
                ) : item.kind === "text" ? (
                  <span className="design-doc-pick-text">{item.text.slice(0, 120)}</span>
                ) : (
                  <span className="design-doc-pick-text">{item.kind === "video" ? "🎬" : "🔊"}</span>
                )}
              </button>
            ))}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
          <button onClick={onClose}>{t("common.cancel")}</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
