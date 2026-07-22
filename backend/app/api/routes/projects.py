import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grid_layout import compute_layout
from app.core.track_order import ordered_tracks
from app.db.base import get_db
from app.db.models import Project
from app.schemas.schemas import ProjectCreate, ProjectRead, TrackRead

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectRead])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at))
    return result.scalars().all()


@router.post("", response_model=ProjectRead, status_code=201)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(**payload.model_dump())
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/{project_id}/tracks", response_model=list[TrackRead])
async def list_project_tracks(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Returned already in list order (head -> next -> ...) -- the frontend
    # derives each track's ephemeral row number from this position, so the
    # order the client renders is exactly the order established here.
    return await ordered_tracks(db, project_id)


@router.get("/{project_id}/layout")
async def project_layout(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Backend-computed derived layout (workflow row-spans + blocked cells) --
    see core/grid_layout.py. The client renders from this instead of
    recomputing the span formula itself."""
    return await compute_layout(db, project_id)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    await db.delete(project)
    await db.commit()
