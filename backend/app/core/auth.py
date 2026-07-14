from fastapi import Request, WebSocket, status
from fastapi.responses import JSONResponse

from app.config import get_settings

PUBLIC_PATHS = {"/api/health"}


async def auth_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    if not path.startswith("/api/") and not path.startswith("/ws/"):
        return await call_next(request)
    if path in PUBLIC_PATHS:
        return await call_next(request)

    token = _extract_token(request.headers.get("authorization"), request.query_params.get("token"))
    if token != settings.api_token:
        # Raising HTTPException here would NOT be caught into a clean response --
        # Starlette's ExceptionMiddleware sits *inside* custom `@app.middleware("http")`
        # functions, not around them, so an exception raised here falls through to
        # ServerErrorMiddleware as a generic 500 instead of the intended 401.
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Invalid or missing API token"})
    return await call_next(request)


def _extract_token(auth_header: str | None, query_token: str | None) -> str | None:
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return query_token


async def check_ws_auth(websocket: WebSocket) -> bool:
    settings = get_settings()
    token = websocket.query_params.get("token")
    auth_header = websocket.headers.get("authorization")
    extracted = _extract_token(auth_header, token)
    return extracted == settings.api_token
