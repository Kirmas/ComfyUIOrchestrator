"""Web Push: an agent chat turn finishing can reach a subscribed browser even
with the tab closed -- the far end of the bridge from agent_runner's own
"this turn just ended" moment (runner.py's Chat.set_status, which calls back
POST /api/push/notify with this instance's own api_token, the same way
night.py's health check already does). The VAPID signing key lives in the DB
(VapidKeypair, a singleton row) rather than .env, generated lazily on first
use so there's nothing to configure by hand on dev or prod.
"""

import asyncio
import json
import logging
import uuid

import requests
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PushSubscription, VapidKeypair

logger = logging.getLogger(__name__)

# Rides in the VAPID JWT's "sub" claim, sent to the browser vendor's push
# service (e.g. Google FCM) on every push -- not a safe place for the user's
# real address, and the push service has no legitimate reason to need it.
VAPID_SUBJECT = "mailto:admin@orchestrator.local"


async def get_or_create_vapid_keys(db: AsyncSession) -> VapidKeypair:
    row = await db.get(VapidKeypair, 1)
    if row:
        return row
    v = Vapid()
    v.generate_keys()
    priv_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    row = VapidKeypair(id=1, private_key_b64=b64urlencode(priv_raw), public_key_b64=b64urlencode(pub_raw))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _send_one(endpoint: str, p256dh: str, auth: str, payload: str, private_key_b64: str) -> int | None:
    """Blocking (pywebpush uses `requests`); call via asyncio.to_thread.
    Returns an HTTP status worth pruning the subscription for, else None. A
    pywebpush call that never got an HTTP response at all (DNS failure,
    timeout, connection refused) raises a bare requests exception rather than
    WebPushException -- caught separately so one dead/unreachable subscription
    can't take the whole broadcast down via asyncio.gather."""
    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=payload,
            vapid_private_key=private_key_b64,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status not in (404, 410):
            logger.warning("push to %s failed: %s", endpoint, exc)
        return status
    except requests.exceptions.RequestException as exc:
        logger.warning("push to %s unreachable: %s", endpoint, exc)
        return None
    return None


async def send_to_all(db: AsyncSession, title: str, body: str, url: str = "/") -> None:
    keys = await get_or_create_vapid_keys(db)
    subs = (await db.execute(select(PushSubscription))).scalars().all()
    if not subs:
        return
    payload = json.dumps({"title": title, "body": body, "url": url})
    results = await asyncio.gather(
        *(asyncio.to_thread(_send_one, s.endpoint, s.p256dh, s.auth, payload, keys.private_key_b64) for s in subs)
    )
    dead: list[uuid.UUID] = [s.id for s, status in zip(subs, results) if status in (404, 410)]
    if dead:
        await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(dead)))
        await db.commit()
