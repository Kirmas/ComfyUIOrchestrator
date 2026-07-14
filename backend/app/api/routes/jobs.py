import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.queue import job_queue
from app.db.base import get_db
from app.db.models import Job, JobStatusEnum
from app.schemas.schemas import JobRead
from app.worker.tasks import cancel_job

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobRead)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobRead)
async def cancel_job_endpoint(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in (JobStatusEnum.pending, JobStatusEnum.running, JobStatusEnum.waiting_for_backend):
        raise HTTPException(409, f"Job is already {job.status.value}")
    await job_queue.enqueue(cancel_job, str(job.id))
    return job
