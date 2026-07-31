#!/usr/bin/env bash
# Full dev -> prod deploy. Run as yourself (keresh), no sudo on this half --
# it only touches the dev copy. The one privileged step (writing into
# /opt/comfy-orchestrator + restarting the service) is delegated to
# root-deploy.sh over sudo; see deploy/README.md for the one-time sudoers
# setup that makes that call passwordless.
#
# Usage:
#   deploy.sh          build + sync + migrate + restart
#   deploy.sh backup   snapshot DB + media + .env, then stop (no deploy)
set -euo pipefail

DEV=/home/keresh/comfy-orchestrator
MODE="${1:-deploy}"

case "$MODE" in
  backup)
    # Nothing to build -- go straight to the privileged half. Worth running on
    # its own before any structural change; the finished charts in the DB and
    # the generated media are not reproducible.
    exec sudo "$DEV/deploy/root-deploy.sh" backup
    ;;
  deploy) ;;
  *)
    echo "usage: deploy.sh [deploy|backup]" >&2
    exit 2
    ;;
esac

echo "==> building frontend"
cd "$DEV/frontend"
npm ci
# Cap node's heap. This box has ~2 GB total and runs Jellyfin/Plex alongside,
# so an unbounded V8 heap gets the build OOM-killed partway through (exit 137/
# 144) -- which had already happened more than once. A smaller ceiling just
# makes it collect garbage sooner; the build itself takes about the same time.
NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=448}" npm run build

echo "==> handing off to root-deploy.sh"
sudo "$DEV/deploy/root-deploy.sh" deploy
