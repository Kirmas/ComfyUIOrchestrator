from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.auth import check_ws_auth
from app.core.ws_manager import ws_manager

router = APIRouter(tags=["ws"])


@router.websocket("/ws/projects/{project_id}")
async def project_ws(websocket: WebSocket, project_id: str):
    if not await check_ws_auth(websocket):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    ws_manager.register(project_id, websocket)
    try:
        while True:
            # Client doesn't need to send anything; keep the connection alive
            # and drop it cleanly on disconnect. Any inbound message is ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.unregister(project_id, websocket)
