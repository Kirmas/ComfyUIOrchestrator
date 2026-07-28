"""The MCP server instance, mounted into the main FastAPI app at /mcp.

Stateless HTTP: each tool call is self-contained (node ids are the handles --
there is no per-session state to keep, see roadmap.md's "handle persistence"),
so nothing is lost if a client reconnects.
"""
from mcp.server.fastmcp import FastMCP

# streamable_http_path is the route *inside* this sub-app. It defaults to
# "/mcp", which would sit at /mcp/mcp once the sub-app is itself mounted at
# /mcp in main.py; "/" puts the endpoint exactly at the mount point.
mcp_server = FastMCP("comfy-orchestrator", stateless_http=True, streamable_http_path="/")
