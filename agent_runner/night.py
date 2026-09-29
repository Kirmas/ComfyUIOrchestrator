"""Night session mechanics: git, backup, deploy, health, rollback.

Night mode works bug reports from other agents unattended: an agent reads the
report, decides whether it is a real bug, and fixes it in the dev copy. All the
irreversible-looking steps are done *here*, by fixed code, never by the agent
(it is told not to, and its Bash has those commands disallowed):

  first report of the night:
    full prod backup (deploy.sh backup), then `git stash -u` of whatever work
    in progress the dev copy has, so none of it can reach prod
  per report the agent calls a bug and fixes:
    quick checks (backend imports, frontend typechecks) -> create the night
    branch if this is the night's first fix -> deploy.sh -> health probes ->
    healthy: commit the fix on the night branch
    unhealthy: throw the fix away, redeploy the branch head, probe again
  morning, the person decides for the whole session, no partial picks:
    accept: fast-forward main to the night branch (prod already runs it)
    reject: back to main, deploy main (prod returns to the evening's state)
    either way: the stashed work in progress comes back last

The fix is committed only once prod has come up with it, so "roll back" is
always just "discard the uncommitted change and redeploy HEAD" -- no reverts.
Night fixes may not touch schema (migrations), so redeploying older code is a
complete rollback.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

DEPLOY_TIMEOUT = 30 * 60
CHECK_TIMEOUT = 5 * 60
DIFF_LIMIT = 200_000


async def sh(*args: str, cwd: Path, env: dict | None = None, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace")


def tail(text: str, n: int = 30) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


async def git(repo: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    return await sh("git", *args, cwd=repo, timeout=timeout)


async def git_ok(repo: Path, *args: str) -> str:
    code, out = await git(repo, *args)
    if code:
        raise RuntimeError(f"git {' '.join(args)} failed: {tail(out, 10)}")
    return out.strip()


async def branch(repo: Path) -> str:
    return await git_ok(repo, "rev-parse", "--abbrev-ref", "HEAD")


async def head(repo: Path) -> str:
    return await git_ok(repo, "rev-parse", "HEAD")


async def dirty(repo: Path) -> str:
    """`git status --porcelain` -- empty when the working copy is clean."""
    return await git_ok(repo, "status", "--porcelain")


async def discard_changes(repo: Path) -> None:
    """Throw away every uncommitted change. Only ever called when the tree
    was clean before the agent's turn, so all of it is the agent's. Ignored
    files (.venv, node_modules, .env, dev.db) are untouched: no -x."""
    await git_ok(repo, "reset", "--hard", "HEAD")
    await git_ok(repo, "clean", "-fd")


# --- health -----------------------------------------------------------------


def _status(url: str, token: str | None) -> int:
    req = Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urlopen(req, timeout=10) as res:
            return res.status
    except URLError as exc:
        return getattr(exc, "code", 0) or 0
    except Exception:  # noqa: BLE001
        return 0


async def healthy(api_url: str, token: str, attempts: int = 45) -> tuple[bool, str]:
    """Read-only probes of the live service, retried while it restarts."""
    probes = [("/api/health", None), ("/api/projects", token), ("/", None)]
    last = ""
    for _ in range(attempts):
        results = [(p, await asyncio.to_thread(_status, api_url + p, tok)) for p, tok in probes]
        last = ", ".join(f"{p} {s}" for p, s in results)
        if all(s == 200 for _, s in results):
            return True, last
        await asyncio.sleep(2)
    return False, last


# --- checks before a deploy ----------------------------------------------------


async def prechecks(repo: Path, changed: list[str]) -> tuple[bool, str]:
    """Catch what would take prod down or fail the build before deploying:
    the backend must import, the frontend must typecheck. (deploy.sh would
    stop on a failed frontend build before touching prod anyway; a backend
    that doesn't import would only show up as a dead service.)"""
    report = []
    py = repo / "backend/.venv/bin/python"
    if any(p.startswith("backend/") for p in changed) and py.exists():
        env = {"API_TOKEN": "precheck", "FRONTEND_DIST_DIR": "", "PYTHONDONTWRITEBYTECODE": "1"}
        code, out = await sh(str(py), "-c", "import app.main", cwd=repo / "backend", env=env, timeout=CHECK_TIMEOUT)
        if code:
            return False, "backend does not import:\n" + tail(out)
        report.append("backend imports")
    if any(p.startswith("frontend/") for p in changed) and (repo / "frontend/node_modules").exists():
        code, out = await sh("npx", "tsc", "-b", cwd=repo / "frontend", timeout=CHECK_TIMEOUT)
        if code:
            return False, "frontend does not typecheck:\n" + tail(out)
        report.append("frontend typechecks")
    return True, ", ".join(report) or "nothing to check"


async def changed_paths(repo: Path) -> list[str]:
    # Raw output, not dirty(): porcelain lines start with a status column
    # that may be a space (" M path"), and stripping the whole output eats
    # it on the first line -- which then loses a character of its path.
    code, out = await git(repo, "status", "--porcelain")
    if code:
        raise RuntimeError(f"git status failed: {tail(out, 10)}")
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


async def deploy(repo: Path) -> tuple[bool, str]:
    code, out = await sh(str(repo / "deploy/deploy.sh"), cwd=repo, timeout=DEPLOY_TIMEOUT)
    return code == 0, tail(out)


# --- session state -------------------------------------------------------------


class NightState:
    """Persisted in the runner's data dir. `session` is the open night (or
    None); `history` keeps how past nights ended."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"accept_reports": False, "session": None, "history": []}
        if path.exists():
            self.data.update(json.loads(path.read_text()))
        # One git operation on the dev copy at a time: a report being
        # deployed and a morning accept must never interleave.
        self.lock = asyncio.Lock()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    @property
    def session(self) -> dict | None:
        return self.data["session"]

    def note(self, text: str) -> None:
        if self.session is not None:
            self.session.setdefault("notes", []).append({"ts": time.time(), "text": text})
            self.save()


async def open_session(state: NightState, repo: Path) -> str | None:
    """First report of the night: back up prod, stash work in progress.
    Returns None on success, else why the night can't start."""
    current = await branch(repo)
    if current != "main":
        return f"the dev copy is on branch {current!r}, not main -- a night session starts from main"
    code, out = await sh(str(repo / "deploy/deploy.sh"), "backup", cwd=repo, timeout=DEPLOY_TIMEOUT)
    if code:
        return "prod backup failed, so nothing was touched:\n" + tail(out)
    session_id = time.strftime("%Y-%m-%d-%H%M")
    stash = None
    if await dirty(repo):
        await git_ok(repo, "stash", "push", "-u", "-m", f"night session {session_id}: work in progress set aside")
        stash = await git_ok(repo, "rev-parse", "stash@{0}")
    state.data["session"] = {
        "id": session_id,
        "branch": f"night/{session_id}",
        "branch_created": False,
        "base": await head(repo),
        "stash": stash,
        "backup": tail(out, 3),
        "started_at": time.time(),
        "reports": [],
        "commits": [],
        "notes": [],
    }
    state.save()
    return None


async def restore_stash(repo: Path, stash: str | None) -> str:
    if not stash:
        return "there was no work in progress to restore"
    code, listing = await git(repo, "stash", "list", "--format=%gd %H")
    ref = next((line.split()[0] for line in listing.splitlines() if line.endswith(stash)), None)
    if not ref:
        return f"the stash {stash[:10]} is no longer in `git stash list` -- nothing restored"
    code, out = await git(repo, "stash", "pop", ref)
    if code:
        return f"your work in progress did not re-apply cleanly (it conflicts with a night fix); it is still safe in `git stash list` as {stash[:10]}:\n{tail(out, 10)}"
    return "your work in progress is back in the working copy"


def close_session(state: NightState, outcome: str, detail: str) -> dict:
    session = state.session
    session.update({"outcome": outcome, "outcome_detail": detail, "finished_at": time.time()})
    state.data["history"] = ([session] + state.data["history"])[:20]
    state.data["session"] = None
    # Reports are switched back on by the person, not left on into the day.
    state.data["accept_reports"] = False
    state.save()
    return session


async def session_diff(repo: Path, session: dict) -> str:
    if not session.get("branch_created"):
        return ""
    code, out = await git(repo, "diff", f"{session['base']}..{session['branch']}")
    return out[:DIFF_LIMIT] + (f"\n… diff truncated at {DIFF_LIMIT} chars" if len(out) > DIFF_LIMIT else "")
