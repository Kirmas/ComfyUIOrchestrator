"""Bridge 2 of roadmap.md §1: pulling idea-board text into a node's prompt.

Two independent pieces:

- `strip_markdown` -- stickers are written in markdown, samplers are not. A
  prompt containing "**scarred** brow" or "- short hair" feeds the model literal
  asterisks and bullets. Every path that moves sticker text into a prompt goes
  through this, whether the user pasted the text or used a `{tag}` macro.
- `resolve_macros` -- expands `{tag}` against the project's tagged text
  stickers, at run time, in the node instance's own params. Never in a
  capability's baked workflow_json: capabilities are global, so a project's
  character description baked in there would leak into every other project
  using the same capability.

The same function serves the run and the node-config preview (see
api/routes/boards.py), so "what the preview showed" and "what actually ran"
cannot drift apart.
"""
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Board, BoardItem, BoardItemKind

# Tags are word-ish on purpose. ComfyUI's own dynamic-prompt syntax uses braces
# too ("{red|blue}"), and excluding "|" and whitespace keeps this from ever
# matching one of those.
_MACRO_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")

_MD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^```.*$", re.MULTILINE), ""),          # fenced code delimiters
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE), ""),  # headings
    (re.compile(r"^\s{0,3}>\s?", re.MULTILINE), ""),       # blockquotes
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),       # bullet lists
    (re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE), ""),     # ordered lists
    (re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE), ""),  # horizontal rules
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),        # images -> alt text
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),         # links -> label
    (re.compile(r"(\*\*\*|___)(.+?)\1", re.DOTALL), r"\2"),
    (re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL), r"\2"),
    (re.compile(r"(?<!\w)([*_])(?=\S)(.+?)(?<=\S)\1(?!\w)", re.DOTALL), r"\2"),
    (re.compile(r"`([^`]*)`"), r"\1"),                     # inline code
]


def strip_markdown(text: str) -> str:
    """Markdown source -> the plain words inside it.

    Deliberately regex-based rather than a real parser: the input is a sticker a
    person typed, not arbitrary documents, and a dependency that has to be
    installed on the box to un-bold a sentence isn't worth it.
    """
    out = text or ""
    for pattern, replacement in _MD_PATTERNS:
        out = pattern.sub(replacement, out)
    # Collapse the blank lines the substitutions above tend to leave behind,
    # then join what's left into prompt-shaped single-spaced text.
    lines = [line.strip() for line in out.splitlines()]
    return "\n".join(line for line in lines if line).strip()


async def project_idea_texts(db: AsyncSession, project_id: uuid.UUID) -> dict[str, str]:
    """Tag -> stripped sticker text, for every tagged text sticker in the
    project. Tags are unique project-wide (enforced in api/routes/boards.py),
    which is what lets this be a flat dict at all."""
    result = await db.execute(
        select(BoardItem)
        .join(Board, Board.id == BoardItem.board_id)
        .where(
            Board.project_id == project_id,
            BoardItem.kind == BoardItemKind.text,
            BoardItem.tag.is_not(None),
        )
    )
    return {item.tag: strip_markdown(item.text) for item in result.scalars().all() if item.tag}


def apply_macros(text: str, texts: dict[str, str]) -> tuple[str, list[str]]:
    """Returns the expanded text plus the tags that had no sticker behind them.

    An unknown `{tag}` is left standing, NOT expanded to an empty string: a
    quietly emptied prompt generates the wrong thing and nobody notices, whereas
    a literal "{head}" is visible both in the config preview (rendered as
    unresolved) and in the generated result. It is not an error either -- a
    deleted note should not make a generation fail outright.
    """
    unresolved: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        tag = match.group(1)
        if tag in texts:
            return texts[tag]
        unresolved.append(tag)
        return match.group(0)

    return _MACRO_RE.sub(substitute, text or ""), list(dict.fromkeys(unresolved))


async def resolve_macros(db: AsyncSession, project_id: uuid.UUID, text: str) -> tuple[str, list[str]]:
    return apply_macros(text, await project_idea_texts(db, project_id))
