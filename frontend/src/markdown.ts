/** Minimal markdown -> HTML for idea-board stickers, the agent chat and the
 * design doc.
 *
 * Deliberately not `marked`/`markdown-it`: any real renderer passes raw HTML in
 * the source straight through, so it would have to come with a sanitizer as
 * well -- two dependencies, on a box where `vite build` has been OOM-killed, to
 * render bold text on a sticker. This escapes first and only then recognises a
 * small subset, so there is no path from sticker text to live markup at all.
 *
 * The subset is what people actually type on a sticky note: headings, bold,
 * italic, strikethrough, inline code, bullet/numbered lists, quotes, rules,
 * links, images, line breaks -- plus GitHub-style tables and ``` code fences,
 * which the agent chat (AgentChat.tsx) gets from the model all the time.
 *
 * Links and images may also target something inside the project instead of a
 * URL -- `node:<id>`, `dashboard:<id>`, `board:<id>`, `asset:<id>` (the design doc's references,
 * routes/design_docs.py). What each one stands for is resolved by the caller
 * and handed in as `refs`; this file only turns it into markup, and only ever
 * emits an http(s) or same-origin /api URL as a src/href.
 *
 * Note this is display only. Text on its way into a prompt is stripped, not
 * rendered, and that happens on the backend (core/idea_macros.py) so the run
 * and the preview can't disagree.
 */

/** A reference as the renderer needs it: already resolved to URLs. */
export interface RenderRef {
  missing: boolean;
  label: string | null;
  /** Thumbnail for an inline/embedded picture. */
  src: string | null;
  mime: string | null;
  /** A text sticker's own markdown, embedded as a quote. */
  text: string | null;
}

export interface MarkdownOptions {
  /** Added to a heading's level. A sticker's "# " is a card title, not a page
   * h1, so stickers and chat keep the old +2; the design doc is a page. */
  headingOffset?: number;
  refs?: Record<string, RenderRef>;
}

/** Same scheme list as REF_PATTERN in backend/app/api/routes/design_docs.py. */
export const REF_TARGET = /^(?:node|board|asset|dashboard):[0-9a-fA-F-]{36}$/;

/** Every reference target in a markdown text, for resolving them in one call. */
export const findRefs = (source: string): string[] =>
  [...new Set([...source.matchAll(/\]\(((?:node|board|asset|dashboard):[0-9a-fA-F-]{36})\)/g)].map((m) => m[1]))];

const escapeHtml = (text: string): string =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// Only for values that didn't come through escapeHtml already (resolved URLs).
const attr = (value: string): string => escapeHtml(value);

const isSafeUrl = (url: string): boolean => /^https?:\/\//.test(url) || /^\/api\//.test(url);

const mediaTag = (src: string, mime: string | null, alt: string, cls: string): string =>
  mime?.startsWith("video/")
    ? `<video class="${cls}" src="${attr(src)}" controls preload="metadata"></video>`
    : mime?.startsWith("audio/")
      ? `<audio class="${cls}" src="${attr(src)}" controls preload="none"></audio>`
      : `<img class="${cls}" src="${attr(src)}" alt="${alt}" loading="lazy" />`;

/** `#A8A5A1` / `#fff` -- a colour written down in a doc. Rendered with a
 * swatch of itself in front, so a palette table reads as colours rather than
 * as strings to paste somewhere else. The value is matched in full by this
 * pattern before it reaches a style attribute, so nothing else can. */
const HEX_COLOR = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const colorChip = (hex: string): string =>
  `<code class="color-chip"><span class="color-swatch" style="background:${hex}"></span>${hex}</code>`;

/** Emphasis only: runs on text that has no links or code left in it. */
const emphasis = (text: string): string =>
  text
    .replace(/(\*\*|__)(.+?)\1/g, "<strong>$2</strong>")
    .replace(/~~(.+?)~~/g, "<del>$1</del>")
    .replace(/(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)/g, "<em>$2</em>");

function inline(text: string, opts: MarkdownOptions): string {
  // Code spans and links are cut out into placeholders first and put back
  // last, so the emphasis rules can never reach into a URL or a code sample
  // (an `_` in an asset token used to be enough to italicise half a link).
  const held: string[] = [];
  const hold = (html: string) => `\u0000${held.push(html) - 1}\u0000`;

  const out = text
    .replace(/`([^`]+)`/g, (_m, code: string) => hold(HEX_COLOR.test(code.trim()) ? colorChip(code.trim()) : `<code>${code}</code>`))
    // Bare too, but only the unambiguous six-digit form: "#123" is as likely
    // an issue number as a colour.
    .replace(/(^|[\s(,;:])(#[0-9a-fA-F]{6})(?![0-9a-zA-Z_])/g, (_m, lead: string, hex: string) => lead + hold(colorChip(hex)))
    .replace(/(!?)\[([^\]]*)\]\(([^)\s]+)\)/g, (m, bang: string, label: string, target: string) => {
      const shown = emphasis(label);
      if (REF_TARGET.test(target)) {
        const ref = opts.refs?.[target];
        if (!ref) return hold(`<span class="doc-ref pending">${shown || "…"}</span>`);
        if (ref.missing) return hold(`<span class="doc-ref missing" title="${target}">⚠ ${shown || target}</span>`);
        const name = shown || escapeHtml(ref.label ?? "");
        if (bang && ref.src && isSafeUrl(ref.src)) {
          return hold(`<a class="doc-ref-media" data-ref="${target}" href="#">${mediaTag(ref.src, ref.mime, name, "doc-inline-media")}</a>`);
        }
        return hold(`<a class="doc-ref" data-ref="${target}" href="#">${name || target}</a>`);
      }
      // Anything that isn't http(s) never becomes a live href/src
      // ("javascript:" and friends), which is the whole reason this isn't a
      // general renderer. A relative path (a doc pasted from a repo, where
      // `Chart.png` or `../World/Race.md` meant a file next to it) shows its
      // caption instead of raw markdown: a link as its text, a picture as a
      // broken-image marker naming what it wanted.
      if (!/^https?:\/\//.test(target)) {
        if (/^[a-z][a-z0-9+.-]*:/i.test(target)) return m;
        return hold(
          bang
            ? `<span class="doc-ref missing" title="${target}">⚠ ${shown || target}</span>`
            : `<span class="doc-ref-dead" title="${target}">${shown}</span>`,
        );
      }
      return hold(
        bang
          ? `<img class="doc-inline-media" src="${target}" alt="${shown}" loading="lazy" />`
          : `<a href="${target}" target="_blank" rel="noreferrer noopener">${shown}</a>`,
      );
    });

  // Repeated: a held piece can itself contain one (a colour inside a link label).
  let html = emphasis(out);
  while (/\u0000\d+\u0000/.test(html)) html = html.replace(/\u0000(\d+)\u0000/g, (_m, i: string) => held[Number(i)]);
  return html;
}

/** A line that is nothing but one reference image -- `![caption](node:...)` --
 * becomes a block: the picture full width with its caption, or a text
 * sticker's whole note as a quote. Inline, the same syntax is just a thumbnail. */
function embedBlock(line: string, opts: MarkdownOptions): string | null {
  const m = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(line.trim());
  if (!m || !REF_TARGET.test(m[2])) return null;
  const [, caption, target] = m;
  const ref = opts.refs?.[target];
  if (!ref || ref.missing) return null; // inline() already renders both states
  const cap = emphasis(caption) || escapeHtml(ref.label ?? "");
  const figcaption = cap ? `<figcaption>${cap}</figcaption>` : "";
  if (ref.src && isSafeUrl(ref.src)) {
    return `<figure class="doc-embed"><a class="doc-ref-media" data-ref="${target}" href="#">${mediaTag(ref.src, ref.mime, cap, "doc-embed-media")}</a>${figcaption}</figure>`;
  }
  if (ref.text) {
    // One level only: the sticker's own references aren't resolved here, so
    // two stickers quoting each other can't recurse.
    return `<figure class="doc-embed doc-embed-text"><blockquote>${renderMarkdown(ref.text, { headingOffset: 2 })}</blockquote>${figcaption}</figure>`;
  }
  return null;
}

const isTableRow = (line: string): boolean => /^\s*\|.*\|\s*$/.test(line);
const isTableSeparator = (line: string): boolean => /^\s*\|(\s*:?-+:?\s*\|)+\s*$/.test(line);
const splitRow = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());

export function renderMarkdown(source: string, opts: MarkdownOptions = {}): string {
  const headingOffset = opts.headingOffset ?? 2;
  const lines = escapeHtml(source || "").split("\n");
  const out: string[] = [];
  let listType: "ul" | "ol" | null = null;

  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    if (!line.trim()) {
      closeList();
      continue;
    }

    if (/^\s*```/.test(line)) {
      closeList();
      const code: string[] = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
      continue;
    }

    // A table is a header row followed by a |---|:--:| separator row; without
    // the separator a line with pipes in it is just text.
    if (isTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      closeList();
      const align = splitRow(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? "center" : /^-+:$/.test(c) ? "right" : ""));
      const cells = (row: string, tag: "th" | "td") =>
        splitRow(row)
          .map((c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${inline(c, opts)}</${tag}>`)
          .join("");
      const body: string[] = [];
      let j = i + 2;
      for (; j < lines.length && isTableRow(lines[j].trimEnd()); j++) body.push(`<tr>${cells(lines[j], "td")}</tr>`);
      out.push(`<table><thead><tr>${cells(line, "th")}</tr></thead><tbody>${body.join("")}</tbody></table>`);
      i = j - 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      closeList();
      const level = Math.min(heading[1].length + headingOffset, 6);
      out.push(`<h${level}>${inline(heading[2], opts)}</h${level}>`);
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      closeList();
      out.push("<hr />");
      continue;
    }

    // Consecutive "> " lines are one quote, rendered as markdown in their own right.
    if (/^\s*&gt;/.test(line)) {
      closeList();
      const quoted: string[] = [];
      for (; i < lines.length && /^\s*&gt;/.test(lines[i]); i++) quoted.push(lines[i].replace(/^\s*&gt;\s?/, ""));
      i--;
      out.push(`<blockquote>${inline(quoted.join("<br />"), opts)}</blockquote>`);
      continue;
    }

    const embed = embedBlock(line, opts);
    if (embed) {
      closeList();
      out.push(embed);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (bullet) {
      if (listType !== "ul") {
        closeList();
        out.push("<ul>");
        listType = "ul";
      }
      out.push(`<li>${inline(bullet[1], opts)}</li>`);
      continue;
    }

    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      if (listType !== "ol") {
        closeList();
        out.push("<ol>");
        listType = "ol";
      }
      out.push(`<li>${inline(numbered[1], opts)}</li>`);
      continue;
    }

    closeList();
    out.push(`<p>${inline(line, opts)}</p>`);
  }

  closeList();
  return out.join("");
}
