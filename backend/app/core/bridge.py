"""Bridge to the DCC apps on the person's own PC (Unreal, Blender).

The plugins dial *out* to this server over a WebSocket (`/api/bridge/ws`), so
the PC never opens a port and a changed PC address doesn't matter. The socket
carries only small JSON commands; file bytes go over plain HTTP (the plugin
downloads `/api/assets/{id}/file`, uploads to `/api/bridge/upload`).

Deliberately **no queue**: sending to an app that isn't connected is an error
the person sees right away, not something that silently waits for the next
time Unreal is opened (the person's call, 2026-11-22). So all state is the set
of live sockets, held in memory -- a deploy drops them and the plugins
reconnect by themselves.

Auth is per device, never the site's API_TOKEN: a plugin asks to pair
(`Pairing` below), the person approves it on the Settings page (the same code
shows on both screens, so a stranger's request can't be mistaken for one's
own), and the plugin receives its own key, valid for a month (BridgeDevice in
db/models.py; only its SHA-256 is stored). A device key opens nothing but the
plugin-facing `/api/bridge/device/*` routes, and its file download only
serves assets that were actually sent to that device.

Protocol (JSON text frames):
  plugin -> server  {"type":"hello","version":..}
  server -> plugin  {"type":"welcome","client_id":..}
  server -> plugin  {"type":"import","request_id":..,"asset":{...}}
  plugin -> server  {"type":"result","request_id":..,"ok":bool,"message":..}
  either way        {"type":"ping"} / {"type":"pong"}
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import WebSocket

log = logging.getLogger(__name__)

APPS = ("unreal", "blender")
DEVICE_KEY_LIFETIME = timedelta(days=30)
PAIR_REQUEST_TTL = timedelta(minutes=10)
MAX_PENDING_PAIRS = 5


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_device_key() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class BridgeClient:
    id: str
    device_id: str
    app: str
    name: str
    version: str
    ws: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pending: dict[str, asyncio.Future] = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "app": self.app,
            "name": self.name,
            "version": self.version,
            "connected_at": self.connected_at.isoformat(),
            "busy": len(self.pending),
        }


class BridgeError(Exception):
    pass


class BridgeHub:
    def __init__(self) -> None:
        self._clients: dict[str, BridgeClient] = {}
        # device_id -> asset ids sent to it: the only files its key may download.
        self.offered: dict[str, set[str]] = {}

    def register(self, device_id: str, app: str, name: str, version: str, ws: WebSocket) -> BridgeClient:
        client = BridgeClient(id=uuid.uuid4().hex[:12], device_id=device_id, app=app, name=name, version=version, ws=ws)
        self._clients[client.id] = client
        log.info("bridge: %s '%s' connected (%s)", app, name, client.id)
        return client

    def unregister(self, client: BridgeClient) -> None:
        self._clients.pop(client.id, None)
        for fut in client.pending.values():
            if not fut.done():
                fut.set_exception(BridgeError(f"{client.app} '{client.name}' disconnected during the import"))
        client.pending.clear()
        log.info("bridge: %s '%s' disconnected (%s)", client.app, client.name, client.id)

    def of_device(self, device_id: str) -> list[BridgeClient]:
        return [c for c in self._clients.values() if c.device_id == device_id]

    async def drop_device(self, device_id: str, reason: str) -> None:
        """A revoked device loses its live connection at once, not at its next reconnect."""
        self.offered.pop(device_id, None)
        for c in self.of_device(device_id):
            try:
                await c.ws.close(code=4401, reason=reason)
            except Exception:  # already gone
                pass

    def clients(self) -> list[BridgeClient]:
        return sorted(self._clients.values(), key=lambda c: (c.app, c.name, c.connected_at))

    def get(self, client_id: str) -> BridgeClient | None:
        return self._clients.get(client_id)

    def handle_message(self, client: BridgeClient, msg: dict) -> None:
        if msg.get("type") == "result":
            fut = client.pending.pop(str(msg.get("request_id")), None)
            if fut is not None and not fut.done():
                fut.set_result({"ok": bool(msg.get("ok")), "message": str(msg.get("message") or "")})

    async def send_import(self, client: BridgeClient, asset: dict, timeout_s: float) -> dict:
        """Hand one asset to a plugin and wait for its verdict. A timeout is not
        a failure -- a 300 MB FBX can take a while in Unreal -- so the caller
        gets `pending` and the import carries on over there."""
        request_id = uuid.uuid4().hex
        self.offered.setdefault(client.device_id, set()).add(str(asset["id"]))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        client.pending[request_id] = fut
        try:
            await client.ws.send_text(json.dumps({"type": "import", "request_id": request_id, "asset": asset}))
        except Exception as exc:  # socket died between listing and sending
            client.pending.pop(request_id, None)
            raise BridgeError(f"{client.app} '{client.name}' is not reachable: {exc}") from exc
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout_s)
        except asyncio.TimeoutError:
            # Nobody is waiting any more: a late result is simply dropped.
            client.pending.pop(request_id, None)
            return {"ok": True, "pending": True, "message": "still importing"}


@dataclass
class PairRequest:
    id: str
    app: str
    name: str
    code: str
    secret_hash: str
    remote: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "pending"  # pending | approved | denied
    key: str | None = None  # handed to the plugin once, then forgotten
    expires_at: datetime | None = None

    def public(self) -> dict:
        return {"id": self.id, "app": self.app, "name": self.name, "code": self.code,
                "remote": self.remote, "created_at": self.created_at.isoformat()}


class PairingError(Exception):
    pass


class Pairing:
    """Pending pairing requests, in memory: a request lives 10 minutes, and a
    deploy simply means the plugin asks again. Capped, so an unauthenticated
    caller can't flood the Settings page."""

    def __init__(self) -> None:
        self._requests: dict[str, PairRequest] = {}

    def _expire(self) -> None:
        cutoff = datetime.now(timezone.utc) - PAIR_REQUEST_TTL
        for rid in [r.id for r in self._requests.values() if r.created_at < cutoff]:
            del self._requests[rid]

    def create(self, app: str, name: str, remote: str) -> tuple[PairRequest, str]:
        self._expire()
        if sum(r.status == "pending" for r in self._requests.values()) >= MAX_PENDING_PAIRS:
            raise PairingError("too many pairing requests waiting -- approve or deny them on the Settings page first")
        secret = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1000):03d} {secrets.randbelow(1000):03d}"
        req = PairRequest(id=uuid.uuid4().hex, app=app, name=name, code=code, secret_hash=hash_key(secret), remote=remote)
        self._requests[req.id] = req
        return req, secret

    def pending(self) -> list[PairRequest]:
        self._expire()
        return sorted((r for r in self._requests.values() if r.status == "pending"), key=lambda r: r.created_at)

    def get_pending(self, request_id: str) -> PairRequest:
        self._expire()
        req = self._requests.get(request_id)
        if req is None or req.status != "pending":
            raise PairingError("no such pairing request (it may have expired -- press Connect in the plugin again)")
        return req

    def poll(self, request_id: str, secret: str) -> dict:
        self._expire()
        req = self._requests.get(request_id)
        if req is None or not secrets.compare_digest(req.secret_hash, hash_key(secret)):
            return {"status": "expired"}
        if req.status == "approved":
            out = {"status": "approved", "key": req.key, "expires_at": req.expires_at.isoformat() if req.expires_at else None}
            del self._requests[req.id]  # the key leaves exactly once
            return out
        if req.status == "denied":
            del self._requests[req.id]
            return {"status": "denied"}
        return {"status": "pending", "code": req.code}


hub = BridgeHub()
pairing = Pairing()
