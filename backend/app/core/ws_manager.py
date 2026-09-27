from collections import defaultdict

from fastapi import WebSocket


class WebSocketManager:
    """Per-project fanout of progress events to connected browser clients.

    Everything (API routes, the in-process job queue, the WS connections)
    runs in this one process, so broadcasting is just an in-memory dict --
    no separate broker needed to bridge an API process and a worker process."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    def register(self, project_id: str, websocket: WebSocket) -> None:
        self._connections[project_id].add(websocket)

    def unregister(self, project_id: str, websocket: WebSocket) -> None:
        self._connections[project_id].discard(websocket)

    async def broadcast(self, project_id: str, payload: dict) -> None:
        dead = set()
        # A snapshot, never the live set: each send awaits, and a browser tab
        # connecting or dropping during that await adds to or discards from
        # this very set. Iterating it live raised "Set changed size during
        # iteration" into the caller -- once a job re-polling for a free
        # backend, which died before it could requeue itself and sat in
        # waiting_for_backend until the next restart (2026-09-27, node
        # e43c72b9: 3 of 4 variants done, the node "running" all night).
        for ws in list(self._connections.get(project_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections[project_id].discard(ws)


ws_manager = WebSocketManager()
