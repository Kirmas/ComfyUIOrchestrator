"""Per-scope column parity.

Column kind (asset/workflow) is positional, not a per-node choice: whichever
kind the first node in a grid gets fixes column 0, and it strictly alternates
from there. That origin used to be one value per project (Project.start_kind).
With sub-dashboards each grid scope owns its own origin, so a sub-dashboard can
start on a different kind than the graph that points at it.

The project's *main* grid keeps using Project.start_kind rather than getting a
`dashboards` row of its own -- that is what let sub-dashboards ship without
migrating a single existing row (see Track.dashboard_id, nullable = main).
Everything that needs the origin goes through here so that the
"main means Project, otherwise means Dashboard" branch exists exactly once.
"""

from app.db.models import Dashboard, NodeKind, Project


async def scope_start_kind(db, project_id, dashboard_id) -> NodeKind | None:
    """Column 0's kind for one scope, or None if the scope is still empty
    (no node has been created in it yet, so the origin isn't fixed)."""
    if dashboard_id is None:
        project = await db.get(Project, project_id)
        return project.start_kind if project else None
    dashboard = await db.get(Dashboard, dashboard_id)
    return dashboard.start_kind if dashboard else None


async def set_scope_start_kind(db, project_id, dashboard_id, kind: NodeKind) -> None:
    """Fix this scope's origin. Callers only reach here when scope_start_kind
    returned None -- an origin is written once and never revised, since
    changing it would silently reinterpret the kind of every existing column."""
    if dashboard_id is None:
        project = await db.get(Project, project_id)
        if project is not None:
            project.start_kind = kind
        return
    dashboard = await db.get(Dashboard, dashboard_id)
    if dashboard is not None:
        dashboard.start_kind = kind
