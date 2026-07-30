#!/usr/bin/env bash
# Root-only half of the deploy. Meant to be invoked exclusively via
# `sudo deploy/root-deploy.sh [deploy|backup]` (see deploy/deploy.sh, which
# does the non-root half first) or directly through the NOPASSWD sudoers rule
# described in deploy/README.md. Not meant to be run as yourself directly --
# every step here needs to write into /opt/comfy-orchestrator, which is
# owned by the `orchestrator` system user, not `keresh`.
#
# Modes:
#   deploy (default) -- sync dev -> prod, migrate, restart.
#   backup           -- snapshot the DB + media + .env, then exit without
#                       touching prod. Run this before anything structural
#                       (a migration that alters tracks/nodes, a refactor of
#                       the track ordering): the generated assets and the
#                       finished charts in the DB are not reproducible.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "root-deploy.sh must run as root (via sudo)." >&2
  exit 1
fi

DEV=/home/keresh/comfy-orchestrator
PROD=/opt/comfy-orchestrator

MODE="${1:-deploy}"
# ~/media is a separate 466 GB btrfs disk (/dev/sdb). Two reasons it beats the
# conventional /var/backups: /var is its own ~8 GB LV that also holds
# /var/lib/postgresql/17/main, so growing snapshots there would eventually fill
# it and take the database down -- the exact failure this insures against; and
# sdb is a *different physical disk* from the sda that holds both the live
# media and the database, so these snapshots also survive that drive dying.
BACKUP_ROOT="${BACKUP_ROOT:-/home/keresh/media/comfy-orchestrator-backups}"
# ~440 MB per snapshot as of 2026-07-31, almost all of it media. On a disk with
# 213 GB free, deeper history is nearly free -- and depth is what saves you when
# corruption goes unnoticed for a few days.
BACKUP_KEEP="${BACKUP_KEEP:-10}"

do_backup() {
  local stamp dest media_dir prev
  stamp=$(date +%Y%m%d-%H%M%S)
  dest="$BACKUP_ROOT/$stamp"

  # Ask the app itself where things are instead of duplicating the defaults
  # from backend/app/config.py -- MEDIA_DIR/DATABASE_URL can be overridden in
  # prod's .env, which only the orchestrator user can read, and a backup that
  # quietly snapshots the wrong directory is worse than no backup. shlex.quote
  # so a password with shell metacharacters can't break out of the eval.
  local vars
  vars=$(cd "$PROD/backend" && sudo -u orchestrator ./.venv/bin/python - <<'PY'
import shlex
from urllib.parse import urlparse, unquote
from app.config import get_settings

s = get_settings()
url = s.database_url
for driver in ("+asyncpg", "+psycopg2", "+psycopg"):
    url = url.replace(driver, "")
u = urlparse(url)
out = {
    "MEDIA_DIR": s.media_dir,
    "IS_PG": "1" if url.startswith("postgres") else "0",
    "PGHOST": u.hostname or "localhost",
    "PGPORT": str(u.port or 5432),
    "PGUSER": unquote(u.username or ""),
    "PGPASSWORD": unquote(u.password or ""),
    "PGDATABASE": (u.path or "").lstrip("/"),
}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
PY
  )
  eval "$vars"
  # The pg_dump block unsets PGPASSWORD from the environment as soon as it's
  # done; the media cross-check below still needs it to count asset rows.
  local PGPASSWORD_SAVED="${PGPASSWORD:-}"

  mkdir -p "$dest"
  # Contains prod's .env (API token, DB password) -- 700 so it isn't world
  # readable. Owned by keresh rather than root: this is keresh's own disk and
  # keresh should be able to inspect a snapshot without sudo, which costs
  # nothing security-wise since keresh has full sudo and already knows both
  # secrets.
  chown keresh:keresh "$BACKUP_ROOT" "$dest"
  chmod 700 "$BACKUP_ROOT" "$dest"

  echo "==> backing up to $dest"

  if [[ "$IS_PG" == "1" ]]; then
    echo "  -> database ($PGDATABASE)"
    export PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
    # -Fc: compressed custom format, restorable whole or table-by-table via
    # pg_restore. Written to a .part first so an interrupted dump can never be
    # mistaken for a complete one.
    pg_dump -Fc -f "$dest/db.dump.part"
    mv "$dest/db.dump.part" "$dest/db.dump"
    unset PGPASSWORD
    # Prove the archive actually parses rather than trusting pg_dump's exit
    # code alone -- a backup is only worth what a restore can read back out.
    if ! pg_restore --list "$dest/db.dump" > "$dest/db.contents.txt"; then
      echo "  !! dump did not parse -- treating this backup as failed" >&2
      exit 1
    fi
    echo "     $(grep -c 'TABLE DATA' "$dest/db.contents.txt") tables with data"
  else
    echo "  -> database: not postgres ($PGDATABASE), skipping pg_dump" >&2
  fi

  if [[ -d "$MEDIA_DIR" ]]; then
    echo "  -> media ($MEDIA_DIR)"
    # Assets are immutable (storage.put_object always mints a fresh uuid; a
    # native re-run deletes the old rows/files rather than rewriting them), so
    # hardlinking unchanged files against the previous snapshot is safe and
    # makes each extra snapshot nearly free. rsync isn't installed here today
    # -- fall back to a plain copy, and start using --link-dest automatically
    # if it ever is.
    prev=$(find "$BACKUP_ROOT" -maxdepth 2 -type d -name media -not -path "$dest/*" 2>/dev/null | sort | tail -1)
    if command -v rsync >/dev/null 2>&1 && [[ -n "$prev" ]]; then
      rsync -a --link-dest="$prev" "$MEDIA_DIR/" "$dest/media/"
    elif command -v rsync >/dev/null 2>&1; then
      rsync -a "$MEDIA_DIR/" "$dest/media/"
    else
      cp -a "$MEDIA_DIR" "$dest/media"
    fi
    # Cross-check against the DB in the same breath: a snapshot with fewer
    # files than asset rows is missing pictures, and that is only visible now,
    # not on the day it gets restored.
    local files rows
    files=$(find "$dest/media" -type f | wc -l)
    rows=$(PGPASSWORD="$PGPASSWORD_SAVED" psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -t -A \
             -c "select count(*) from assets;" 2>/dev/null || echo "?")
    echo "     $files files backed up, $rows asset rows in the DB"
    if [[ "$rows" != "?" && "$files" -lt "$rows" ]]; then
      echo "  !! fewer media files than asset rows -- some assets have no file" >&2
    fi
  else
    echo "  -> media: $MEDIA_DIR does not exist, skipping" >&2
  fi

  if [[ -f "$PROD/backend/.env" ]]; then
    echo "  -> .env"
    cp -a "$PROD/backend/.env" "$dest/env"
    # cp -a keeps prod's mode (644); this copy holds the API token and the DB
    # password, so don't rely solely on the parent dir being 700.
    chmod 600 "$dest/env"
  fi

  # A backup nobody knows how to restore isn't a backup.
  cat > "$dest/RESTORE.txt" <<EOF
comfy-orchestrator snapshot $stamp

Contents
  db.dump   pg_dump -Fc of database "$PGDATABASE"
  media/    copy of $MEDIA_DIR
  env       copy of $PROD/backend/.env (secrets -- root-only)

Restore the database (DESTRUCTIVE -- drops and recreates every object):
  systemctl stop comfy-orchestrator-api
  sudo -u postgres pg_restore --clean --if-exists \\
      -h $PGHOST -p $PGPORT -U $PGUSER -d $PGDATABASE "$dest/db.dump"

Restore the media files:
  systemctl stop comfy-orchestrator-api
  rm -rf "$MEDIA_DIR" && cp -a "$dest/media" "$MEDIA_DIR"
  chown -R orchestrator:orchestrator "$MEDIA_DIR"

Then: systemctl start comfy-orchestrator-api

The DB and media must be restored together -- an assets row whose file is
missing renders as a broken cell, and a file with no row is invisible.
EOF

  # Retention: prune oldest first. Guard with -r so an empty list is a no-op.
  local -a old
  mapfile -t old < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | sort | head -n "-$BACKUP_KEEP")
  if (( ${#old[@]} )); then
    echo "==> pruning $(( ${#old[@]} )) old snapshot(s), keeping $BACKUP_KEEP"
    printf '%s\0' "${old[@]}" | xargs -0 -r rm -rf
  fi

  # Everything under $dest was written by root; hand the whole snapshot over.
  chown -R keresh:keresh "$dest"

  echo "==> backup complete"
  du -sh "$dest"
  df -h "$BACKUP_ROOT" | tail -1
}

case "$MODE" in
  backup)
    do_backup
    exit 0
    ;;
  deploy) ;;
  *)
    echo "usage: root-deploy.sh [deploy|backup]" >&2
    exit 2
    ;;
esac

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
