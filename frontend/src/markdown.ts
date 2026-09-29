/** Minimal markdown -> HTML for idea-board stickers.
 *
 * Deliberately not `marked`/`markdown-it`: any real renderer passes raw HTML in
 * the source straight through, so it would have to come with a sanitizer as
 * well -- two dependencies, on a box where `vite build` has been OOM-killed, to
 * render bold text on a sticker. This escapes first and only then recognises a
 * small subset, so there is no path from sticker text to live markup at all.
 *
 * The subset is what people actually type on a sticky note: headings, bold,
 * italic, inline code, bullet/numbered lists, links, line breaks -- plus
 * GitHub-style tables and ``` code fences, which the agent chat
 * (AgentChat.tsx) gets from the model all the time.
 *
 * Note this is display only. Text on its way into a prompt is stripped, not
 * rendered, and that happens on the backend (core/idea_macros.py) so the run
 * and the preview can't disagree.
 */

const escapeHtml = (text: string): string =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const inline = (text: string): string =>
  text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(\*\*|__)(.+?)\1/g, "<strong>$2</strong>")
    .replace(/(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)/g, "<em>$2</em>")
    // Only http(s) links become anchors -- "javascript:" and friends stay as
    // plain text, which is the whole reason this isn't a general renderer.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');

const isTableRow = (line: string): boolean => /^\s*\|.*\|\s*$/.test(line);
const isTableSeparator = (line: string): boolean => /^\s*\|(\s*:?-+:?\s*\|)+\s*$/.test(line);
const splitRow = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());

export function renderMarkdown(source: string): string {
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
          .map((c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ""}>${inline(c)}</${tag}>`)
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
      const level = Math.min(heading[1].length + 2, 6); // a sticker's "# " is a card title, not a page h1
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    if (bullet) {
      if (listType !== "ul") {
        closeList();
        out.push("<ul>");
        listType = "ul";
      }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }

    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (numbered) {
      if (listType !== "ol") {
        closeList();
        out.push("<ol>");
        listType = "ol";
      }
      out.push(`<li>${inline(numbered[1])}</li>`);
      continue;
    }

    closeList();
    out.push(`<p>${inline(line)}</p>`);
  }

  closeList();
  return out.join("");
}
