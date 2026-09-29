import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models import Project, ProjectCategory
from app.schemas.schemas import ProjectCategoryCreate, ProjectCategoryRead, ProjectCategoryUpdate

router = APIRouter(prefix="/api/project-categories", tags=["project-categories"])


async def _get(db: AsyncSession, category_id: uuid.UUID) -> ProjectCategory:
    category = await db.get(ProjectCategory, category_id)
    if not category:
        raise HTTPException(404, "Category not found")
    return category


def _clean_name(name: str) -> str:
    if not name.strip():
        raise HTTPException(422, "Category name can't be empty")
    return name.strip()


@router.get("", response_model=list[ProjectCategoryRead])
async def list_categories(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(ProjectCategory).order_by(ProjectCategory.name))).all()


@router.post("", response_model=ProjectCategoryRead, status_code=201)
async def create_category(payload: ProjectCategoryCreate, db: AsyncSession = Depends(get_db)):
    if payload.parent_id is not None:
        await _get(db, payload.parent_id)
    category = ProjectCategory(name=_clean_name(payload.name), parent_id=payload.parent_id)
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return category


@router.patch("/{category_id}", response_model=ProjectCategoryRead)
async def update_category(category_id: uuid.UUID, payload: ProjectCategoryUpdate, db: AsyncSession = Depends(get_db)):
    category = await _get(db, category_id)
    if payload.name is not None:
        category.name = _clean_name(payload.name)
    if "parent_id" in payload.model_fields_set:
        # Walk up from the new parent: meeting ourselves means the move would
        # put a folder inside its own subtree and cut it off from the top.
        cursor = payload.parent_id
        while cursor is not None:
            if cursor == category_id:
                raise HTTPException(409, "Can't move a category into itself or its own subcategory")
            cursor = (await _get(db, cursor)).parent_id
        category.parent_id = payload.parent_id
    await db.commit()
    await db.refresh(category)
    return category


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Only an empty category can go -- see ProjectCategory's docstring."""
    category = await _get(db, category_id)
    has_child = await db.scalar(select(ProjectCategory.id).where(ProjectCategory.parent_id == category_id).limit(1))
    has_project = await db.scalar(select(Project.id).where(Project.category_id == category_id).limit(1))
    if has_child or has_project:
        raise HTTPException(409, "Category isn't empty -- move its projects and subcategories out first")
    await db.delete(category)
    await db.commit()
