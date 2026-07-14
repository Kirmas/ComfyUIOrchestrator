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
        for ws in self._connections.get(project_id, ()):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections[project_id].discard(ws)


ws_manager = WebSocketManager()
