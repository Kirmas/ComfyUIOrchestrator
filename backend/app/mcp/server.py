"""The MCP server instance, mounted into the main FastAPI app at /mcp.

Stateless HTTP: each tool call is self-contained (node ids are the handles --
there is no per-session state to keep, see roadmap.md's "handle persistence"),
so nothing is lost if a client reconnects.
"""
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings

# streamable_http_path is the route *inside* this sub-app. It defaults to
# "/mcp", which would sit at /mcp/mcp once the sub-app is itself mounted at
# /mcp in main.py; "/" puts the endpoint exactly at the mount point.
#
# transport_security: FastMCP auto-enables DNS-rebinding protection only when
# constructed with host="127.0.0.1" (its default), and even then only allows
# Host headers of 127.0.0.1/localhost/::1 -- our uvicorn bind is 0.0.0.0
# (unrelated to this `host` kwarg, which never touches the actual socket) so
# every LAN client's Host header is the box's real address and gets a 421
# "Invalid Host header" unless that address is listed too. Keep protection ON
# (never allowed_hosts=["*"], which is what disabling it amounts to) --
# extend the allow-list instead, from settings.mcp_allowed_hosts.
_settings = get_settings()
mcp_server = FastMCP(
    "comfy-orchestrator",
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_settings.mcp_allowed_hosts,
        allowed_origins=[f"http://{h}" for h in _settings.mcp_allowed_hosts],
    ),
)
