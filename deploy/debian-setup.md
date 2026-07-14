# Deploying on Debian (no containers)

Targets Debian 12 (bookworm) or newer. Everything runs as a single native OS
process (no Docker, no separate queue/object-storage services) — see
`deploy/comfy-orchestrator-api.service`.

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip postgresql nodejs npm curl
```

Debian's `nodejs` package is recent enough (18+) for Vite. If your Debian release ships an older Node, use [NodeSource's setup script](https://github.com/nodesource/distributions) instead of `apt install nodejs`.

`postgresql` starts and enables itself on install via systemd; verify with `systemctl status postgresql`. Skip it entirely if you're using the SQLite fallback (step 3).

## 2. Dedicated service user + install directory

```bash
sudo useradd --system --create-home --home-dir /opt/comfy-orchestrator --shell /usr/sbin/nologin orchestrator
sudo mkdir -p /opt/comfy-orchestrator
sudo chown orchestrator:orchestrator /opt/comfy-orchestrator
```

Copy the repo (`backend/`, `frontend/`, `deploy/`) into `/opt/comfy-orchestrator/`, owned by `orchestrator`.

## 3. Database

```bash
sudo -u postgres psql -c "CREATE USER orchestrator WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE orchestrator OWNER orchestrator;"
```

Match the password to `DATABASE_URL` in `.env`.

**No Postgres available?** Set `DATABASE_URL=sqlite+aiosqlite:///./orchestrator.db` in `.env` instead and skip this section — the schema and app run unchanged on SQLite (see the commented-out line in `.env.example`). It's a single file, no service to install; fine for one local user.

## 4. Backend

```bash
cd /opt/comfy-orchestrator/backend
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp ../deploy/.env.example .env   # then edit .env with real values
./.venv/bin/alembic upgrade head
```

Generated assets (images, 3D meshes, uploads) are stored under `MEDIA_DIR` (default `./media` relative to `backend/`) and served back out by the API itself — no object-storage server needed. Point `MEDIA_DIR` at a path with enough disk space and back it up like any other data directory.

## 5. Frontend

```bash
cd /opt/comfy-orchestrator/frontend
npm ci
npm run build
```

`FRONTEND_DIST_DIR` in `.env` should point at `/opt/comfy-orchestrator/frontend/dist` — the API process serves it directly, so no nginx is required for MVP (you can still put one in front later for TLS termination).

## 6. systemd service

```bash
sudo cp deploy/comfy-orchestrator-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comfy-orchestrator-api
```

One process handles the API, the WebSocket progress feed, and generation dispatch (an in-process asyncio job queue — see `app/core/queue.py`) — nothing else to run or monitor. Check logs with `journalctl -u comfy-orchestrator-api -f`.

If a job is mid-generation when the process restarts (deploy, crash), it's left in `running` state and needs a manual re-generate — there's no separate durable queue to resume it automatically. Fine for interactive single-user use; if that ever becomes a real problem, the fix is re-introducing a persistent queue, not something to pre-build now.

## 7. Point it at your ComfyUI instance(s)

Once the API is up, register each ComfyUI instance and its capabilities through the admin UI (or `POST /api/backends`, `POST /api/capabilities` directly) — no redeploy needed, per SPEC section 2.3.

## 8. Firewall / TLS

Out of scope for MVP. If exposing beyond localhost/LAN, put a reverse proxy (nginx/Caddy) in front for TLS and keep the API bound to `127.0.0.1` (see `--host` in the systemd unit).
