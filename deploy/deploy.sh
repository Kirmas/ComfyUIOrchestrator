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
# Cap node's heap -- this is what actually keeps the build from being
# OOM-killed on this box. Measured peaks: tsc -b ~335 MB, vite build ~496 MB,
# the two together ~515 MB, against 459 MB available (swap already full) the
# time it died. The ceiling doesn't lower the work, it just makes V8 collect
# sooner instead of growing into ground it hasn't got.
#
# Note it is NOT about output chunk size: splitting model-viewer/three.js into
# its own lazy chunk measured 496 MB either way. Where the 496 actually goes,
# measured by building each case:
#
#   168 MB  vite/rollup/esbuild + node, building a 3-module hello-world
#   +125 MB  this whole app (React, 172 modules, ~340 kB of output)
#   +203 MB  three.js, which reaches us as ONE ~1.2 MB prebuilt ESM file
#
# So two thirds of it is the toolchain existing at all, plus one 3D engine --
# not the size of what we emit. The big single module is the expensive part
# (its AST and scope analysis dwarf the source), which is why chunking the
# *output* changes nothing. If build memory ever needs to come down for real,
# the lever is keeping three.js out of the module graph entirely (ship
# model-viewer's prebuilt file from public/ and load it with a script tag),
# not making our bundle smaller.
NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=448}" npm run build

echo "==> handing off to root-deploy.sh"
sudo "$DEV/deploy/root-deploy.sh" deploy
