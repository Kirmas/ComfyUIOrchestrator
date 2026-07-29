/** Minimal markdown -> HTML for idea-board stickers.
 *
 * Deliberately not `marked`/`markdown-it`: any real renderer passes raw HTML in
 * the source straight through, so it would have to come with a sanitizer as
 * well -- two dependencies, on a box where `vite build` has been OOM-killed, to
 * render bold text on a sticker. This escapes first and only then recognises a
 * small subset, so there is no path from sticker text to live markup at all.
 *
 * The subset is what people actually type on a sticky note: headings, bold,
 * italic, inline code, bullet/numbered lists, links, line breaks.
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

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      closeList();
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
