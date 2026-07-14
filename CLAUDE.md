# ComfyUI Orchestrator — context for Claude Code

Full product spec: [SPEC.md](SPEC.md) — read it for architecture, data model, and what's explicitly out of MVP scope.

## Where this code runs

This working copy (`~/comfy-orchestrator`) is the **development copy**, owned by `keresh`, editable directly (no sudo). It is separate from the **live production copy** at `/opt/comfy-orchestrator`, owned by the `orchestrator` system user, run as systemd unit `comfy-orchestrator-api`. The two are not symlinked or synced automatically — deploying means copying dev → prod on purpose (see below).

There is no git repo yet (deliberately deferred by the user). Until one exists, "what changed" has to be tracked by memory/conversation, not `git diff` — don't assume git history is available.

## Deploying a change (dev copy → live service)

`deploy/deploy.sh` scripts this (build frontend, sync backend + frontend
into `/opt/comfy-orchestrator`, `pip install`, `alembic upgrade head`,
restart the unit). It shells out to `deploy/root-deploy.sh` via `sudo` for
the privileged half. See `deploy/README.md` for the one-time sudoers setup
that makes that `sudo` call passwordless for `keresh` — once that's in
place, Claude Code can run `deploy/deploy.sh` directly without the user
typing a password each time. Until that setup is done (or on a box where it
hasn't been applied), `sudo` there prompts interactively and Claude Code
(no TTY) can't answer it — hand the script, or the manual steps below, to
the user to run themselves.

Manual equivalent, if you ever need to do it by hand instead of via the script:

```bash
sudo cp -r ~/comfy-orchestrator/backend/app /opt/comfy-orchestrator/backend/
sudo cp -r ~/comfy-orchestrator/backend/requirements.txt /opt/comfy-orchestrator/backend/
sudo chown -R orchestrator:orchestrator /opt/comfy-orchestrator/backend/app /opt/comfy-orchestrator/backend/requirements.txt

# only if requirements.txt changed:
sudo -u orchestrator /opt/comfy-orchestrator/backend/.venv/bin/pip install -r /opt/comfy-orchestrator/backend/requirements.txt

# only if DB models/migrations changed:
cd /opt/comfy-orchestrator/backend && sudo -u orchestrator ./.venv/bin/alembic upgrade head

# only if frontend changed:
cd ~/comfy-orchestrator/frontend && npm ci && npm run build
sudo rm -rf /opt/comfy-orchestrator/frontend/dist
sudo cp -r ~/comfy-orchestrator/frontend/dist /opt/comfy-orchestrator/frontend/
sudo chown -R orchestrator:orchestrator /opt/comfy-orchestrator/frontend/dist

sudo systemctl restart comfy-orchestrator-api
sudo systemctl status comfy-orchestrator-api
```

## Server specifics (don't rediscover these)

- Debian 13 (trixie), single-box deploy: FastAPI/uvicorn serves the API, the WebSocket progress feed, *and* the built frontend (`frontend/dist`) from one process — no nginx/redis/minio, see `deploy/debian-setup.md` for why.
- `backend/.env` (not in git, lives only at `/opt/comfy-orchestrator/backend/.env`): Postgres DB `orchestrator`/user `orchestrator`, password `2505` (home LAN only, intentionally simple). `API_TOKEN` is a random bearer token, not a login — this is single-user, no auth flow.
- `deploy/comfy-orchestrator-api.service` binds `--host 0.0.0.0` on the live unit so it's reachable from other LAN devices at `http://192.168.0.3:8000/`. The checked-in repo copy of that file still says `127.0.0.1` (the doc's conservative default) — if you ever regenerate `/etc/systemd/system/comfy-orchestrator-api.service` from the repo file, reapply the `0.0.0.0` change or LAN access breaks silently.
- `backend/requirements.txt` includes `greenlet` — required internally by SQLAlchemy's async engine even with the `asyncpg` driver. It was missing originally and broke `alembic upgrade head` with `ValueError: the greenlet library is required`. Don't remove it.
- Mid-run job recovery: there's no durable queue (see `SPEC.md` and `recover_orphaned_jobs()` in `backend/app/worker/tasks.py`) — a restart mid-generation leaves jobs needing a manual re-roll. Expected, not a bug to fix reflexively.
