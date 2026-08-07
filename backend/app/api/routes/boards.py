"""The idea board: stickers (text / image / audio / video / frame / ink /
connector / comment) at free coordinates, plus the project-scoped asset library
they draw their media from.

roadmap.md §1. Two rules worth restating here because they're what keeps this
from turning into a whiteboard toy bolted onto the side:

- The board OWNS its media (Asset.project_id). The grid only ever references it
  (asset.refasset). There is deliberately no "send this output to the board"
  direction: Asset.node_id is ondelete=CASCADE, so an asset owned by a grid cell
  would vanish from the board the moment that cell was deleted.
- A `tag` on a text sticker is a prompt macro handle, and a macro resolves
  against the PROJECT, not one board. Uniqueness is therefore enforced
  project-wide here, on top of the per-board DB constraint.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idea_macros import resolve_macros, strip_markdown
from app.core.storage import build_asset_url, get_storage
from app.db.base import get_db
from app.db.models import Asset, AssetKind, Board, BoardItem, BoardItemKind, Project
from app.schemas.schemas import (
    AssetRead,
    AssetTagsUpdate,
    BoardItemCreate,
    BoardItemRead,
    BoardItemUpdate,
    BoardRead,
    IdeaTextRead,
    MacroResolveRequest,
    MacroResolveResult,
)

router = APIRouter(prefix="/api", tags=["boards"])


def _read_item(item: BoardItem, asset: Asset | None = None) -> BoardItemRead:
    read = BoardItemRead.model_validate(item)
    if asset is not None:
        read.asset_url = build_asset_url(asset.id)
        read.asset_mime_type = asset.mime_type
        read.asset_tags = list(asset.tags or [])
    return read


async def _read_items(db: AsyncSession, items: list[BoardItem]) -> list[BoardItemRead]:
    """Resolves every media sticker's asset in one query rather than per item --
    a board is loaded whole, and N+1 here would be N+1 on every board open."""
    asset_ids = {i.asset_id for i in items if i.asset_id}
    assets: dict[uuid.UUID, Asset] = {}
    if asset_ids:
        result = await db.execute(select(Asset).where(Asset.id.in_(asset_ids)))
        assets = {a.id: a for a in result.scalars().all()}
    return [_read_item(i, assets.get(i.asset_id) if i.asset_id else None) for i in items]


async def _get_board(db: AsyncSession, board_id: uuid.UUID) -> Board:
    board = await db.get(Board, board_id)
    if not board:
        raise HTTPException(404, "Board not found")
    return board


async def _assert_tag_free(db: AsyncSession, board: Board, tag: str, exclude_item_id: uuid.UUID | None) -> None:
    """A `{tag}` macro in a prompt is resolved against the whole project, so two
    stickers answering to the same tag would make it ambiguous which text a run
    actually used -- and the run would still succeed, silently, with one of
    them. Rejected up front instead."""
    query = (
        select(BoardItem.id)
        .join(Board, Board.id == BoardItem.board_id)
        .where(Board.project_id == board.project_id, BoardItem.tag == tag)
    )
    if exclude_item_id:
        query = query.where(BoardItem.id != exclude_item_id)
    if (await db.execute(query)).first():
        raise HTTPException(409, f"Tag '{tag}' is already used by another sticker in this project")


# ---------- boards ----------
@router.get("/projects/{project_id}/board", response_model=BoardRead)
async def get_or_create_board(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """The project's board, created on first access. One per project by design --
    there is no "create a board" step, and no second board to pick between."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    result = await db.execute(select(Board).where(Board.project_id == project_id).order_by(Board.created_at))
    board = result.scalars().first()
    if board is None:
        board = Board(project_id=project_id, name="Ideas")
        db.add(board)
        await db.commit()
        await db.refresh(board)
    return BoardRead.model_validate(board)


@router.get("/boards/{board_id}/items", response_model=list[BoardItemRead])
async def list_board_items(board_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _get_board(db, board_id)
    result = await db.execute(select(BoardItem).where(BoardItem.board_id == board_id).order_by(BoardItem.z, BoardItem.created_at))
    return await _read_items(db, list(result.scalars().all()))


@router.post("/boards/{board_id}/items", response_model=BoardItemRead, status_code=201)
async def create_board_item(board_id: uuid.UUID, payload: BoardItemCreate, db: AsyncSession = Depends(get_db)):
    board = await _get_board(db, board_id)

    asset: Asset | None = None
    if payload.kind in (BoardItemKind.image, BoardItemKind.audio, BoardItemKind.video):
        if not payload.asset_id:
            raise HTTPException(400, f"A '{payload.kind.value}' sticker needs an asset_id")
        asset = await db.get(Asset, payload.asset_id)
        if not asset:
            raise HTTPException(404, "Asset not found")
        if asset.project_id != board.project_id:
            # A node-owned asset here would be destroyed by deleting that grid
            # cell, taking the sticker's content with it -- see module docstring.
            raise HTTPException(400, "Only assets in this project's library can go on the board")

    if payload.kind == BoardItemKind.connector and not (payload.source_item_id and payload.target_item_id):
        raise HTTPException(400, "A connector needs both source_item_id and target_item_id")
    if payload.kind == BoardItemKind.comment and not payload.target_item_id:
        raise HTTPException(400, "A comment needs target_item_id")

    for ref in (payload.source_item_id, payload.target_item_id):
        if ref is None:
            continue
        other = await db.get(BoardItem, ref)
        if not other or other.board_id != board_id:
            raise HTTPException(400, f"Referenced item {ref} is not on this board")

    if payload.tag:
        await _assert_tag_free(db, board, payload.tag, None)

    item = BoardItem(board_id=board_id, **payload.model_dump(exclude={"tag"}), tag=payload.tag or None)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _read_item(item, asset)


@router.patch("/board-items/{item_id}", response_model=BoardItemRead)
async def update_board_item(item_id: uuid.UUID, payload: BoardItemUpdate, db: AsyncSession = Depends(get_db)):
    item = await db.get(BoardItem, item_id)
    if not item:
        raise HTTPException(404, "Board item not found")

    data = payload.model_dump(exclude_unset=True)
    if "tag" in data:
        tag = (data.pop("tag") or "").strip()
        if tag:
            await _assert_tag_free(db, await _get_board(db, item.board_id), tag, item.id)
        item.tag = tag or None

    for field, value in data.items():
        if value is not None:
            setattr(item, field, value)

    await db.commit()
    await db.refresh(item)
    asset = await db.get(Asset, item.asset_id) if item.asset_id else None
    return _read_item(item, asset)


@router.delete("/board-items/{item_id}", status_code=204)
async def delete_board_item(item_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Connectors and comments attached to this sticker go with it (both self-FKs
    cascade) rather than being left pointing at coordinates that mean nothing.

    An image/audio/video sticker OWNS its asset -- one sticker, one dedicated
    Asset row, never shared (Board.tsx's uploadFiles always creates a fresh
    Asset and exactly one sticker together in the same call; nothing ever
    attaches a second sticker to an already-existing asset_id). So deleting
    the sticker deletes its file and its Asset row too, the same as
    delete_node already does for a grid cell's own outputs -- fixes the
    2026-08-07 incident where board-item delete only removed the board_items
    row, leaving the file and the (now unreachable-from-any-UI) Asset row
    behind forever. This deliberately does NOT check whether some grid
    asset.refasset elsewhere still points at this asset id: a dangling
    refasset already renders nothing rather than erroring (see
    Dashboard.result_asset_id's own docstring on the same graceful-degradation
    behavior) -- the sticker is the owner here, a grid reference to it is not."""
    item = await db.get(BoardItem, item_id)
    if not item:
        raise HTTPException(404, "Board item not found")
    if item.asset_id:
        asset = await db.get(Asset, item.asset_id)
        if asset:
            get_storage().delete_object(asset.storage_key)
            # Cascades to delete `item` too (assets.id -> board_items.asset_id
            # is ON DELETE CASCADE) -- no separate db.delete(item) needed.
            await db.delete(asset)
            await db.commit()
            return
    await db.delete(item)
    await db.commit()


# ---------- project asset library ----------
@router.get("/projects/{project_id}/assets", response_model=list[AssetRead])
async def list_project_assets(project_id: uuid.UUID, tag: str | None = None, db: AsyncSession = Depends(get_db)):
    """The same library the board shows by position, as a flat list -- this is
    what the grid's "з референсів" picker reads. Tag filtering is done in Python
    on purpose (see Asset.tags in db/models.py)."""
    result = await db.execute(
        select(Asset).where(Asset.project_id == project_id).order_by(Asset.created_at.desc())
    )
    assets = list(result.scalars().all())
    if tag:
        assets = [a for a in assets if tag in (a.tags or [])]
    reads = []
    for asset in assets:
        read = AssetRead.model_validate(asset)
        read.url = build_asset_url(asset.id)
        reads.append(read)
    return reads


@router.post("/projects/{project_id}/assets", response_model=AssetRead, status_code=201)
async def upload_project_asset(project_id: uuid.UUID, file: UploadFile, db: AsyncSession = Depends(get_db)):
    """Upload straight into the project library -- no node, no cell, no column
    parity. This is what makes collecting 40 loose references possible at all."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    data = await file.read()
    mime_type = file.content_type or "application/octet-stream"
    kind = AssetKind.for_mime(mime_type)
    key = get_storage().put_object(data, mime_type, prefix=f"projects/{project_id}")
    asset = Asset(project_id=project_id, storage_key=key, mime_type=mime_type, kind=kind, selected=False, tags=[], meta={})
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    read = AssetRead.model_validate(asset)
    read.url = build_asset_url(asset.id)
    return read


# ---------- bridge 2: idea text -> prompt ----------
@router.get("/projects/{project_id}/idea-texts", response_model=list[IdeaTextRead])
async def list_idea_texts(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Every text sticker in the project, for the node config's "взяти з ідей"
    picker. `text_plain` is what gets inserted either way -- markdown belongs on
    the board, not in a prompt."""
    result = await db.execute(
        select(BoardItem)
        .join(Board, Board.id == BoardItem.board_id)
        .where(Board.project_id == project_id, BoardItem.kind == BoardItemKind.text)
        .order_by(BoardItem.created_at)
    )
    return [
        IdeaTextRead(
            item_id=item.id,
            tag=item.tag,
            text_markdown=item.text,
            text_plain=strip_markdown(item.text),
        )
        for item in result.scalars().all()
    ]


@router.post("/projects/{project_id}/resolve-macros", response_model=MacroResolveResult)
async def resolve_macros_preview(project_id: uuid.UUID, payload: MacroResolveRequest, db: AsyncSession = Depends(get_db)):
    """What a prompt containing `{tag}` macros will actually look like when it
    runs. Shares one implementation with the worker (core/idea_macros.py) so the
    preview shown in the node config can't drift from what is generated -- a
    macro must never be able to hide what will run."""
    resolved, unresolved = await resolve_macros(db, project_id, payload.text)
    return MacroResolveResult(resolved=resolved, unresolved=unresolved)


@router.patch("/assets/{asset_id}/tags", response_model=AssetRead)
async def set_asset_tags(asset_id: uuid.UUID, payload: AssetTagsUpdate, db: AsyncSession = Depends(get_db)):
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Asset not found")
    asset.tags = list(dict.fromkeys(t.strip() for t in payload.tags if t.strip()))
    await db.commit()
    await db.refresh(asset)
    read = AssetRead.model_validate(asset)
    read.url = build_asset_url(asset.id)
    return read
