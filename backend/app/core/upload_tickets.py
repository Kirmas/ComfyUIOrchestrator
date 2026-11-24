"""Short-lived upload tickets: a way for an agent on ANOTHER machine to push big
files into one asset cell without ever holding the site's API_TOKEN.

The MCP tool `create_upload_ticket` mints one; the agent then `curl`s files to
`/api/upload-ticket/<ticket>`. That path is public (auth_middleware) because the
ticket itself is the credential: random, bound to one node, limited in number
of files and in time. In memory only -- a restart drops them, and that's fine
(ask for a new one)."""
import secrets
import time
import uuid
from dataclasses import dataclass

MAX_MINUTES = 120
MAX_FILES = 500


@dataclass
class Ticket:
    """Either one cell (`node_id`) or a whole graph (`project_id` [+ `dashboard_id`]):
    then every file gets its own new row at the bottom, named after the file."""
    node_id: uuid.UUID | None
    expires: float
    uses_left: int
    project_id: uuid.UUID | None = None
    dashboard_id: uuid.UUID | None = None


_TICKETS: dict[str, Ticket] = {}


def _sweep() -> None:
    now = time.time()
    for k in [k for k, t in _TICKETS.items() if t.expires < now or t.uses_left <= 0]:
        del _TICKETS[k]


def create(node_id: uuid.UUID | None, max_files: int, minutes: int, project_id: uuid.UUID | None = None,
           dashboard_id: uuid.UUID | None = None) -> tuple[str, Ticket]:
    _sweep()
    t = Ticket(node_id, time.time() + 60 * max(1, min(minutes, MAX_MINUTES)), max(1, min(max_files, MAX_FILES)),
               project_id, dashboard_id)
    key = secrets.token_urlsafe(24)
    _TICKETS[key] = t
    return key, t


def peek(key: str) -> Ticket | None:
    """The ticket if still valid (does not spend a use)."""
    t = _TICKETS.get(key)
    if t is None or t.expires < time.time() or t.uses_left <= 0:
        return None
    return t
