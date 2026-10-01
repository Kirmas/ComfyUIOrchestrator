"""Web Push subscriptions. The browser subscribes once (POST .../subscribe)
after the person opts in from the Agent tab; agent_runner calls POST
.../notify with this instance's own API_TOKEN (same auth as every other /api
route -- see agent_chats.py's _self_check, now passed to every chat it
creates) whenever a turn stops running, so a finished answer can reach the
phone's lock screen even with the tab closed."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import push
from app.db.base import get_db
from app.db.models import PushSubscription

router = APIRouter(prefix="/api/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class UnsubscribeIn(BaseModel):
    endpoint: str


class NotifyIn(BaseModel):
    title: str
    body: str
    url: str = "/"


@router.get("/vapid-public-key")
async def vapid_public_key(db: AsyncSession = Depends(get_db)):
    keys = await push.get_or_create_vapid_keys(db)
    return {"key": keys.public_key_b64}


@router.post("/subscribe", status_code=204)
async def subscribe(payload: SubscriptionIn, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))).scalar_one_or_none()
    if existing:
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
    else:
        db.add(PushSubscription(endpoint=payload.endpoint, p256dh=payload.keys.p256dh, auth=payload.keys.auth))
    await db.commit()


@router.post("/unsubscribe", status_code=204)
async def unsubscribe(payload: UnsubscribeIn, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
    await db.commit()


@router.post("/notify", status_code=204)
async def notify(payload: NotifyIn, db: AsyncSession = Depends(get_db)):
    await push.send_to_all(db, payload.title, payload.body, payload.url)
