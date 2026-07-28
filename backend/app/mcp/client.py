"""In-process HTTP client the MCP tools use to call this app's own REST API.

Every MCP tool that has a REST equivalent goes through here rather than
re-implementing route logic, so the tools inherit the routes' validation and
error handling instead of drifting from them. `ASGITransport` dispatches
straight into the ASGI app -- no socket, no port, same event loop.

The app is bound late (`set_app` from main.py's create_app) because importing
main.py from here would be circular.
"""
import httpx

from app.config import get_settings

_app = None


def set_app(app) -> None:
    global _app
    _app = app


def get_client() -> httpx.AsyncClient:
    if _app is None:
        raise RuntimeError("MCP internal client used before set_app()")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app),
        base_url="http://internal",
        headers={"Authorization": f"Bearer {get_settings().api_token}"},
        timeout=120.0,
    )


def raise_for_api_error(response: httpx.Response) -> None:
    """Surface a REST error to the agent as its actual message.

    httpx's own `raise_for_status` reports only the status code; the agent needs
    the route's `detail` string to know whether to fix its call or give up.
    """
    if response.is_success:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise RuntimeError(f"{response.request.method} {response.request.url.path} -> {response.status_code}: {detail}")
