#!/usr/bin/env bash
# Root-only half of the deploy. Meant to be invoked exclusively via
# `sudo deploy/root-deploy.sh` (see deploy/deploy.sh, which does the
# non-root half first) or directly through the NOPASSWD sudoers rule
# described in deploy/README.md. Not meant to be run as yourself directly --
# every step here needs to write into /opt/comfy-orchestrator, which is
# owned by the `orchestrator` system user, not `keresh`.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "root-deploy.sh must run as root (via sudo)." >&2
  exit 1
fi

DEV=/home/keresh/comfy-orchestrator
PROD=/opt/comfy-orchestrator

echo "==> syncing backend"
cp -r "$DEV/backend/app" "$PROD/backend/"
cp -r "$DEV/backend/requirements.txt" "$PROD/backend/"
# alembic/versions was never synced here before (2026-07-17 incident: a new
# migration's file only ever existed in the dev copy, so `alembic upgrade
# head` on prod had nothing new to apply and silently no-opped -- the app
# then started against a schema missing the column its own models.py
# declared, 500ing on every Node query). Sync the whole alembic/ directory,
# not just versions/, in case env.py/script.py.mako ever change too.
cp -r "$DEV/backend/alembic" "$PROD/backend/"
chown -R orchestrator:orchestrator "$PROD/backend/app" "$PROD/backend/requirements.txt" "$PROD/backend/alembic"

echo "==> installing backend deps"
sudo -u orchestrator "$PROD/backend/.venv/bin/pip" install -q -r "$PROD/backend/requirements.txt"

echo "==> running migrations"
cd "$PROD/backend"
sudo -u orchestrator ./.venv/bin/alembic upgrade head

if [[ -d "$DEV/frontend/dist" ]]; then
  echo "==> syncing frontend"
  rm -rf "$PROD/frontend/dist"
  cp -r "$DEV/frontend/dist" "$PROD/frontend/"
  chown -R orchestrator:orchestrator "$PROD/frontend/dist"
fi

echo "==> restarting service"
systemctl restart comfy-orchestrator-api
sleep 1
systemctl status comfy-orchestrator-api --no-pager -l
