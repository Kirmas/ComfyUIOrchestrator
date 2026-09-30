import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { resolveAssetUrl } from "../api/client";
import { assetsApi, boardApi, designDocApi } from "../api/endpoints";
import { assetClipboard, subgraphClipboard, useClipboardSlot } from "../clipboard";
import { useLangStore, useT } from "../i18n";
import { findRefs, type RenderRef, renderMarkdown, toggleTask } from "../markdown";
import type { BoardItem, DesignDocRef, DesignDocSummary } from "../types";
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
export type DocLang = "uk" | "en";

/** Where a followed doc link asked to land (App.tsx). */
export interface DocLanding {
  anchor: string | null;
  lang: DocLang;
  nonce: number;
}
const DOC_LANGS: DocLang[] = ["uk", "en"];

const SAVE_DELAY_MS = 1200;
const BOARD_KINDS = new Set(["text", "image", "audio", "video"]);

const toRenderRef = (ref: DesignDocRef): RenderRef => {
  const mime = ref.asset?.mime_type ?? null;
  const isImage = !!mime?.startsWith("image/");
  return {
    missing: ref.missing,
    label: ref.label,
    // A picture shows its page-sized rendition -- uncropped, unlike the grid's
    // square preview -- and the full file opens on click; video and audio have
    // no rendition, so they play the original.
    src: ref.asset ? resolveAssetUrl(isImage ? (ref.asset.fit_url ?? ref.asset.url) : ref.asset.url) : null,
    mime,
    text: ref.text,
  };
};

type SaveState = "saved" | "dirty" | "saving" | "error";

/** One entry of the table of contents. Read off the rendered DOM rather than
 * re-parsed from the markdown, so it can never disagree with what's on the
 * page (a fenced "# comment" isn't a heading, and only the renderer knows). */
interface TocEntry {
  el: HTMLElement;
  level: number;
  text: string;
}

// h1-h3: a character bible has hundreds of h4s; listing them buries the outline.
const TOC_SELECTOR = "h1, h2, h3";

/** Whose doc this is: a project's own (addressed through the project, which
 * may not have one yet), or a global one sitting in a folder. */
export type DocSource = { kind: "project"; projectId: string } | { kind: "global"; docId: string; title: string };

const sourceKey = (s: DocSource) => (s.kind === "project" ? `p:${s.projectId}` : `g:${s.docId}`);
const fetchDoc = (s: DocSource, lang: DocLang) =>
  s.kind === "project" ? designDocApi.projectGet(s.projectId, lang) : designDocApi.getText(s.docId, lang);
const saveDoc = (s: DocSource, lang: DocLang, content: string) =>
  s.kind === "project" ? designDocApi.projectSave(s.projectId, lang, content) : designDocApi.saveText(s.docId, lang, content);

export function DesignDoc({
  source,
  landing,
  onFollow,
  onBack,
}: {
  source: DocSource;
  landing?: DocLanding | null;
  onFollow?: (docId: string, anchor: string | null, lang: DocLang) => void;
  onBack?: () => void;
}) {
  const t = useT();
  const key = sourceKey(source);
  // The chapter to scroll to once the text is on screen; then cleared.
  const pendingAnchor = useRef<string | null>(landing?.anchor ?? null);
  const [docId, setDocId] = useState<string | null>(null);
  const [linkPickerOpen, setLinkPickerOpen] = useState(false);
  const [lang, setLang] = useState<DocLang>(landing?.lang ?? useLangStore.getState().lang);
  const [content, setContent] = useState<string | null>(null);
  const [refs, setRefs] = useState<Record<string, DesignDocRef>>({});
  const [editing, setEditing] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [boardPickerOpen, setBoardPickerOpen] = useState(false);
  const [openRef, setOpenRef] = useState<DesignDocRef | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const renderedRef = useRef<HTMLDivElement>(null);
  const [toc, setToc] = useState<TocEntry[]>([]);
  const [activeToc, setActiveToc] = useState(0);
  const copied = useClipboardSlot(assetClipboard);
  const copiedSubgraph = useClipboardSlot(subgraphClipboard);
  // Refs asked for but not answered yet, so a ref isn't re-requested on every
  // keystroke while its first request is still in flight.
  const requested = useRef(new Set<string>());

  // Which doc the latest load was for: a slow answer for the language just
  // switched away from must not land in the one switched to.
  const loadingFor = useRef("");

  const load = () => {
    const loadKey = `${key}:${lang}`;
    loadingFor.current = loadKey;
    return fetchDoc(source, lang).then((doc) => {
      if (loadingFor.current !== loadKey) return;
      setDocId(doc.doc_id);
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
  }, [key]);

  useEffect(() => {
    setContent(null);
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, lang]);

  // Autosave. Keyed on the text itself, so the timer restarts on every edit.
  useEffect(() => {
    if (saveState !== "dirty" || content === null) return;
    const timer = setTimeout(() => {
      setSaveState("saving");
      saveDoc(source, lang, content)
        .then((doc) => {
          setRefs((prev) => ({ ...prev, ...doc.refs }));
          // Typing may have continued while the request was out.
          setSaveState((s) => (s === "saving" ? "saved" : s));
        })
        .catch(() => setSaveState("error"));
    }, SAVE_DELAY_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [content, saveState, key, lang]);

  // Live preview: resolve references the moment they appear in the text,
  // without waiting for a save.
  useEffect(() => {
    if (content === null) return;
    const unknown = findRefs(content).filter((r) => !(r in refs) && !requested.current.has(r));
    if (unknown.length === 0) return;
    unknown.forEach((r) => requested.current.add(r));
    designDocApi.resolveRefs(unknown).then((resolved) => setRefs((prev) => ({ ...prev, ...resolved })));
  }, [content, refs]);

  const html = useMemo(() => {
    const renderRefs = Object.fromEntries(Object.entries(refs).map(([k, v]) => [k, toRenderRef(v)]));
    return renderMarkdown(content ?? "", {
      headingOffset: 0,
      refs: renderRefs,
      interactiveTasks: true,
      lineAnchors: true,
      headingIds: true,
    });
  }, [content, refs]);

  useEffect(() => {
    const root = renderedRef.current;
    setToc(
      root && !editing
        ? [...root.querySelectorAll<HTMLElement>(TOC_SELECTOR)].map((el) => ({
            el,
            level: Number(el.tagName[1]),
            text: el.textContent ?? "",
          }))
        : [],
    );
  }, [html, editing]);

  // ---- editor <-> preview scroll sync ----
  // Source lines don't map to textarea pixels one to one (long lines wrap), so
  // the pixel top of every line is measured off a hidden copy of the textarea
  // with the same width and font. The preview side needs no measuring: every
  // top-level block carries data-line (markdown.ts lineAnchors).
  const lineTops = useRef<number[]>([]);
  // Whichever pane the pointer/finger/focus is on drives; the other follows.
  // Without this each programmatic scroll would echo back as a user scroll.
  const driver = useRef<"editor" | "preview">("editor");

  const measureLines = () => {
    const ta = textareaRef.current;
    if (!ta || content === null) return;
    const cs = getComputedStyle(ta);
    const mirror = document.createElement("div");
    Object.assign(mirror.style, {
      position: "absolute",
      visibility: "hidden",
      left: "-99999px",
      top: "0",
      width: `${ta.clientWidth}px`,
      boxSizing: "border-box",
      paddingLeft: cs.paddingLeft,
      paddingRight: cs.paddingRight,
      fontFamily: cs.fontFamily,
      fontSize: cs.fontSize,
      fontWeight: cs.fontWeight,
      lineHeight: cs.lineHeight,
      letterSpacing: cs.letterSpacing,
      whiteSpace: "pre-wrap",
      overflowWrap: "break-word",
    });
    for (const line of content.split("\n")) {
      const row = document.createElement("div");
      row.textContent = line || "\u200b";
      mirror.appendChild(row);
    }
    document.body.appendChild(mirror);
    const padTop = parseFloat(cs.paddingTop) || 0;
    lineTops.current = Array.from(mirror.children, (row) => (row as HTMLElement).offsetTop + padTop);
    mirror.remove();
  };

  /** [source line, pixel top] for every anchored block, in order. */
  const previewAnchors = (): [number, number][] =>
    Array.from(renderedRef.current?.querySelectorAll<HTMLElement>("[data-line]") ?? [], (el) => [
      Number(el.dataset.line),
      el.offsetTop,
    ]);

  /** Linear interpolation over sorted [x, y] pairs. */
  const interpolate = (pairs: [number, number][], x: number): number => {
    if (pairs.length === 0) return 0;
    let k = 0;
    while (k + 1 < pairs.length && pairs[k + 1][0] <= x) k++;
    const [x0, y0] = pairs[k];
    const next = pairs[k + 1];
    if (!next || next[0] === x0) return y0;
    return y0 + ((x - x0) / (next[0] - x0)) * (next[1] - y0);
  };

  const editorLinePairs = (): [number, number][] => lineTops.current.map((top, line) => [line, top]);

  const syncFromEditor = () => {
    const ta = textareaRef.current;
    const pv = renderedRef.current;
    if (!ta || !pv || driver.current !== "editor") return;
    if (ta.scrollTop + ta.clientHeight >= ta.scrollHeight - 2) {
      pv.scrollTop = pv.scrollHeight;
      return;
    }
    const line = interpolate(
      editorLinePairs().map(([l, top]) => [top, l]),
      ta.scrollTop,
    );
    pv.scrollTop = interpolate(previewAnchors(), line);
  };

  const syncFromPreview = () => {
    const ta = textareaRef.current;
    const pv = renderedRef.current;
    if (!ta || !pv || driver.current !== "preview") return;
    if (pv.scrollTop + pv.clientHeight >= pv.scrollHeight - 2) {
      ta.scrollTop = ta.scrollHeight;
      return;
    }
    const line = interpolate(
      previewAnchors().map(([l, top]) => [top, l]),
      pv.scrollTop,
    );
    ta.scrollTop = interpolate(editorLinePairs(), line);
  };

  // Re-measure when the text changes (debounced -- a 90 KB doc is a lot of
  // rows to lay out per keystroke) and when the editor changes width.
  useEffect(() => {
    if (!editing) return;
    const timer = setTimeout(() => {
      measureLines();
      syncFromEditor();
    }, 250);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing, content]);

  useEffect(() => {
    const ta = textareaRef.current;
    if (!editing || !ta) return;
    const observer = new ResizeObserver(() => measureLines());
    observer.observe(ta);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editing]);

  // Highlights the section being read: the last heading scrolled past the top.
  const onPageScroll = () => {
    const top = (pageRef.current?.getBoundingClientRect().top ?? 0) + 90;
    let current = 0;
    toc.forEach((entry, i) => {
      if (entry.el.getBoundingClientRect().top <= top) current = i;
    });
    setActiveToc(current);
  };

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

  /** At the cursor, inline -- a link sits inside a sentence, unlike an embed. */
  const insertInline = (snippet: string) => {
    const text = content ?? "";
    const el = textareaRef.current;
    const at = el && editing ? el.selectionEnd : text.length;
    edit(text.slice(0, at) + snippet + text.slice(at));
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(at + snippet.length, at + snippet.length);
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

  const scrollToAnchor = (anchor: string): boolean => {
    const el = renderedRef.current?.querySelector(`[id="${CSS.escape(anchor)}"]`);
    el?.scrollIntoView({ block: "start" });
    return !!el;
  };

  // Land on the chapter a followed link named, once its text has rendered.
  useEffect(() => {
    if (!pendingAnchor.current || content === null) return;
    const anchor = pendingAnchor.current;
    pendingAnchor.current = null;
    requestAnimationFrame(() => scrollToAnchor(anchor));
  }, [html, content]);

  const onRenderedClick = (e: React.MouseEvent) => {
    // A link to another doc (or a chapter of this one).
    const link = (e.target as HTMLElement).closest<HTMLElement>("a.doc-link");
    if (link) {
      e.preventDefault();
      const target = link.dataset.doc;
      const anchor = link.dataset.anchor ?? null;
      if (!target || target === docId) {
        if (anchor) scrollToAnchor(anchor);
      } else onFollow?.(target, anchor, lang);
      return;
    }
    // A ticked checkbox is an edit like any other: it goes through the same
    // autosave, in view mode too -- ticking a TODO shouldn't need the editor.
    const task = (e.target as HTMLElement).closest<HTMLInputElement>("input.task-check[data-task]");
    if (task) {
      e.preventDefault();
      if (content !== null) edit(toggleTask(content, Number(task.dataset.task)));
      return;
    }
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-ref]");
    if (!el) return;
    e.preventDefault();
    // A player's own controls sit inside the link; let them work.
    if ((e.target as HTMLElement).closest("video, audio")) return;
    const ref = refs[el.dataset.ref ?? ""];
    if (ref && !ref.missing) setOpenRef(ref);
  };

  /** Hands the project's doc over to its folder as a global one. Rare, and it
   * empties this tab, hence the confirm. */
  const makeGlobal = async () => {
    if (source.kind !== "project" || !confirm(t("doc.makeGlobalConfirm"))) return;
    try {
      const doc = await designDocApi.makeGlobal(source.projectId);
      alert(t("doc.madeGlobal", { title: doc.title }));
      setRefs({});
      await load();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  };

  const status = { saved: t("doc.saved"), dirty: t("doc.unsaved"), saving: t("doc.saving"), error: t("doc.saveError") }[saveState];

  return (
    <div className="main-area design-doc-page" ref={pageRef} onScroll={toc.length > 1 ? onPageScroll : undefined}>
      <div className="design-doc-toolbar">
        {/* Switching with unsaved text would save it into the other language. */}
        <div className="design-doc-langs">
          {DOC_LANGS.map((l) => (
            <button key={l} className={lang === l ? "active" : ""} onClick={() => setLang(l)} disabled={saveState !== "saved"}>
              {l.toUpperCase()}
            </button>
          ))}
        </div>
        {onBack && (
          <button onClick={onBack} disabled={saveState !== "saved"} title={t("doc.backTitle")}>
            {t("doc.back")}
          </button>
        )}
        {source.kind === "global" && <strong className="design-doc-title">📄 {source.title}</strong>}
        <button disabled={content === null || (editing && saveState !== "saved")} className={editing ? "active" : ""} onClick={() => (editing ? setEditing(false) : void load().then(() => setEditing(true)))}>
          {editing ? t("doc.done") : t("doc.edit")}
        </button>
        {editing && (
          <>
            {/* A board belongs to a project; a global doc has none to pick from
                (a sticker ref pasted in by hand still resolves). */}
            {source.kind === "project" && (
              <button onClick={() => setBoardPickerOpen(true)} title={t("doc.fromBoardTitle")}>
                {t("doc.fromBoard")}
              </button>
            )}
            <button onClick={insertCopiedCell} disabled={!copied} title={copied ? t("doc.pasteCellTitle", { label: copied.label }) : t("doc.pasteCellHint")}>
              {t("doc.pasteCell")}
            </button>
            <button onClick={() => setLinkPickerOpen(true)} title={t("doc.linkDocTitle")}>
              {t("doc.linkDoc")}
            </button>
            <button onClick={insertCopiedSubgraph} disabled={!copiedSubgraph} title={copiedSubgraph ? t("doc.pasteSubgraphTitle", { name: copiedSubgraph.name }) : t("doc.pasteSubgraphHint")}>
              {t("doc.pasteSubgraph")}
            </button>
          </>
        )}
        {source.kind === "project" && !editing && content?.trim() && (
          <button onClick={() => void makeGlobal()} title={t("doc.makeGlobalTitle")}>
            {t("doc.makeGlobal")}
          </button>
        )}
        <span className={`design-doc-status ${saveState}`}>{status}</span>
      </div>

      <div className={editing ? "design-doc-split" : "design-doc-view"}>
        {editing && (
          <textarea
            ref={textareaRef}
            className="design-doc-editor"
            value={content ?? ""}
            onChange={(e) => edit(e.target.value)}
            onScroll={syncFromEditor}
            onPointerEnter={() => (driver.current = "editor")}
            onTouchStart={() => (driver.current = "editor")}
            onFocus={() => (driver.current = "editor")}
            placeholder={t("doc.placeholder")}
            spellCheck
          />
        )}
        {/* renderMarkdown escapes before it formats (see markdown.ts). */}
        <div
          className="design-doc-rendered markdown-body"
          ref={renderedRef}
          onClick={onRenderedClick}
          onScroll={editing ? syncFromPreview : undefined}
          onPointerEnter={() => (driver.current = "preview")}
          onTouchStart={() => (driver.current = "preview")}
        >
          {content === null ? (
            <p className="design-doc-empty">{t("common.loading")}</p>
          ) : content.trim() ? (
            <div dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <p className="design-doc-empty">{t("doc.empty")}</p>
          )}
        </div>
        {toc.length > 1 && (
          <nav className="design-doc-toc">
            <div className="design-doc-toc-title">{t("doc.toc")}</div>
            {(() => {
              const minLevel = Math.min(...toc.map((e) => e.level));
              return toc.map((entry, i) => (
                <button
                  key={i}
                  className={i === activeToc ? "active" : ""}
                  style={{ paddingLeft: 8 + (entry.level - minLevel) * 12 }}
                  onClick={() => entry.el.scrollIntoView({ behavior: "smooth", block: "start" })}
                  title={entry.text}
                >
                  {entry.text}
                </button>
              ));
            })()}
          </nav>
        )}
      </div>

      {boardPickerOpen && source.kind === "project" && (
        <BoardItemPicker
          projectId={source.projectId}
          onPick={(item) => {
            setBoardPickerOpen(false);
            insert(`![${item.tag ?? ""}](board:${item.id})`);
          }}
          onClose={() => setBoardPickerOpen(false)}
        />
      )}

      {linkPickerOpen && (
        <DocLinkPicker
          lang={lang}
          onPick={(snippet) => {
            setLinkPickerOpen(false);
            insertInline(snippet);
          }}
          onClose={() => setLinkPickerOpen(false)}
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

/** Pick another design doc (a global one or a project's), then optionally one
 * of its chapters; produces `[title](doc:<id>#chapter)`. Chapters are read in
 * the language being edited, since the two versions' anchors differ. */
function DocLinkPicker({ lang, onPick, onClose }: { lang: DocLang; onPick: (snippet: string) => void; onClose: () => void }) {
  const t = useT();
  const [docs, setDocs] = useState<DesignDocSummary[] | null>(null);
  const [chosen, setChosen] = useState<DesignDocSummary | null>(null);
  const [chapters, setChapters] = useState<{ id: string; text: string; level: number }[] | null>(null);

  useEffect(() => {
    designDocApi
      .list(true)
      .then(setDocs)
      .catch(() => setDocs([]));
  }, []);

  useEffect(() => {
    if (!chosen) return;
    setChapters(null);
    designDocApi.getText(chosen.id, lang).then((doc) => {
      // The renderer's own headings, ids and all -- so the anchor written here
      // is exactly the one that page will have.
      const html = renderMarkdown(doc.content, { headingOffset: 0, headingIds: true });
      const found = [...html.matchAll(/<h(\d)[^>]*\bid="([^"]*)"[^>]*>(.*?)<\/h\1>/g)].map((m) => ({
        level: Number(m[1]),
        id: m[2],
        text: m[3].replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"'),
      }));
      setChapters(found);
    });
  }, [chosen, lang]);

  const link = (label: string, anchor?: string) => onPick(`[${label.replace(/[[\]]/g, "")}](doc:${chosen!.id}${anchor ? `#${anchor}` : ""})`);

  return createPortal(
    <div className="image-modal-backdrop" onClick={onClose}>
      <div className="params-modal-content doc-link-picker" onClick={(e) => e.stopPropagation()}>
        <h3>{chosen ? chosen.title : t("doc.linkDoc")}</h3>
        {!chosen ? (
          docs === null ? (
            <p style={{ color: "var(--text-dim)" }}>{t("common.loading")}</p>
          ) : (
            <div className="doc-link-list">
              {docs.map((d) => (
                <button key={d.id} onClick={() => setChosen(d)}>
                  {d.project_id ? "🖼 " : "📄 "}
                  {d.title}
                </button>
              ))}
            </div>
          )
        ) : (
          <div className="doc-link-list">
            <button className="doc-link-whole" onClick={() => link(chosen.title)}>
              {t("doc.linkWholeDoc")}
            </button>
            {chapters === null ? (
              <p style={{ color: "var(--text-dim)" }}>{t("common.loading")}</p>
            ) : (
              chapters.map((c, i) => (
                <button key={i} style={{ paddingLeft: 8 + (c.level - 1) * 14 }} onClick={() => link(c.text, c.id)}>
                  {c.text}
                </button>
              ))
            )}
          </div>
        )}
        <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8 }}>
          {chosen ? <button onClick={() => setChosen(null)}>{t("doc.back")}</button> : <span />}
          <button onClick={onClose}>{t("common.cancel")}</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
