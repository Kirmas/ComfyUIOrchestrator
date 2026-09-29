#!/usr/bin/env bash
# One-time (and idempotent) setup of the agent runner as a user service.
#
#   agent_runner/install.sh <token>[,<token>...]
#
# The tokens are the API_TOKENs of the orchestrator instances allowed to use
# this runner (prod, and the dev one if you want dev chats against the same
# runner). They go into ~/.config/comfy-agent-runner/env (mode 600), never git.
# Other lines already in that file are kept -- notably RUNNER_DEV_ENABLED=1,
# the switch for dev chats (off unless that line is there).
#
# The service must survive logging out of SSH, which for a *user* unit needs
# lingering -- the one root step, run it yourself once:
#   sudo loginctl enable-linger keresh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="$HOME/.config/comfy-agent-runner"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -x "$HERE/.venv/bin/uvicorn" ]; then
  python3 -m venv "$HERE/.venv"
  "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
fi

mkdir -p "$CONF" "$UNIT_DIR"
if [ "${1:-}" ]; then
  umask 077
  touch "$CONF/env"
  { grep -v '^RUNNER_TOKENS=' "$CONF/env" || true; printf 'RUNNER_TOKENS=%s\n' "$1"; } > "$CONF/env.new"
  mv "$CONF/env.new" "$CONF/env"
elif [ ! -f "$CONF/env" ]; then
  echo "usage: install.sh <orchestrator-api-token>[,<token>...]  (no $CONF/env yet)" >&2
  exit 2
fi

ln -sf "$HERE/comfy-agent-runner.service" "$UNIT_DIR/comfy-agent-runner.service"
systemctl --user daemon-reload
systemctl --user enable comfy-agent-runner
systemctl --user restart comfy-agent-runner
sleep 1
systemctl --user --no-pager status comfy-agent-runner | head -5

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "!! Lingering is off: the runner stops when your last session ends."
  echo "   Run once:  sudo loginctl enable-linger $USER"
fi
