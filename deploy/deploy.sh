#!/usr/bin/env bash
# Full dev -> prod deploy. Run as yourself (keresh), no sudo on this half --
# it only touches the dev copy. The one privileged step (writing into
# /opt/comfy-orchestrator + restarting the service) is delegated to
# root-deploy.sh over sudo; see deploy/README.md for the one-time sudoers
# setup that makes that call passwordless.
set -euo pipefail

DEV=/home/keresh/comfy-orchestrator

echo "==> building frontend"
cd "$DEV/frontend"
npm ci
npm run build

echo "==> handing off to root-deploy.sh"
sudo "$DEV/deploy/root-deploy.sh"
