"""Read a long markdown document a section at a time.

A design doc can outgrow what one MCP tool result may carry (VanDrow's is
85k characters; Claude Code caps a tool result well below that and parks the
rest in a file a project chat has no tool to open). So a reader gets the
heading tree first, then one section by its number -- and a section still
too big for one answer comes back as its intro plus the list of its
subsections, or, with none to descend into, in paragraph-aligned parts.
"""
import re

MAX_SECTION_CHARS = 30_000

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) of every heading outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and (m := _HEADING.match(line)):
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _sections(content: str) -> tuple[list[str], list[dict]]:
    lines = content.split("\n")
    heads = _headings(lines)
    sections = []
    for n, (start, level, title) in enumerate(heads):
        end = next((s for s, lv, _ in heads[n + 1:] if lv <= level), len(lines))
        children = [k for k in range(n + 1, len(heads)) if heads[k][0] < end]
        sections.append({
            "id": n,
            "level": level,
            "title": title,
            "start": start,
            "end": end,
            # direct children only: the next level actually used below this one
            "children": [k for k in children if heads[k][1] == min(heads[c][1] for c in children)],
        })
    return lines, sections


def _chars(lines: list[str], start: int, end: int) -> int:
    return len("\n".join(lines[start:end]))


def outline(content: str) -> dict:
    lines, sections = _sections(content)
    return {
        "total_chars": len(content),
        "preamble_chars": _chars(lines, 0, sections[0]["start"]) if sections else len(content),
        "sections": [
            {"id": s["id"], "level": s["level"], "title": s["title"], "chars": _chars(lines, s["start"], s["end"])}
            for s in sections
        ],
    }


def _paragraph_parts(text: str, limit: int) -> list[str]:
    """Split on blank lines into chunks under `limit`; a single paragraph
    longer than that is hard-cut, which is the only way it can ever fit."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        piece = para if not cur else cur + "\n\n" + para
        if len(piece) <= limit:
            cur = piece
            continue
        if cur:
            parts.append(cur)
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        cur = para
    if cur:
        parts.append(cur)
    return parts


def section(content: str, section_id: int | None, part: int = 1, limit: int = MAX_SECTION_CHARS) -> dict:
    """`section_id` None = the preamble before the first heading."""
    lines, sections = _sections(content)
    if section_id is None:
        start, end, title, children = 0, sections[0]["start"] if sections else len(lines), None, []
    else:
        if not 0 <= section_id < len(sections):
            raise ValueError(f"No section {section_id}; the outline has ids 0..{len(sections) - 1}")
        s = sections[section_id]
        start, end, title, children = s["start"], s["end"], s["title"], s["children"]
    text = "\n".join(lines[start:end])
    result = {"id": section_id, "title": title, "chars": len(text)}
    if len(text) <= limit:
        return {**result, "content": text, "complete": True}
    if children:
        intro_end = sections[children[0]]["start"]
        return {
            **result,
            "content": "\n".join(lines[start:intro_end]),
            "complete": False,
            "note": "Too long for one answer: this is only the text before the first subsection. Read the subsections by id.",
            "subsections": [
                {"id": k, "title": sections[k]["title"], "chars": _chars(lines, sections[k]["start"], sections[k]["end"])}
                for k in children
            ],
        }
    parts = _paragraph_parts(text, limit)
    if not 1 <= part <= len(parts):
        raise ValueError(f"No part {part}; this section has parts 1..{len(parts)}")
    return {**result, "content": parts[part - 1], "complete": len(parts) == 1, "part": part, "parts": len(parts)}
