# Deploying dev → prod

`deploy/deploy.sh` builds the frontend and syncs `backend/app`,
`requirements.txt`, and `frontend/dist` into `/opt/comfy-orchestrator`,
then restarts `comfy-orchestrator-api`. It's the scripted version of the
manual steps in `CLAUDE.md`.

The privileged half (writing into `/opt/comfy-orchestrator`, which is owned
by the `orchestrator` system user, and restarting the systemd unit) lives in
`deploy/root-deploy.sh` and runs via `sudo`. By default that `sudo` prompts
for a password interactively — fine when you run `deploy.sh` yourself, but
it blocks Claude Code, which has no TTY to answer a password prompt from.

## One-time setup: passwordless sudo for this one script

This does **not** grant Claude (or anything running as `keresh`) any new
capability — `keresh` already has full sudo access, this just removes the
password prompt for one specific, readable script. Scope it to exactly that
script, not to `sudo` in general.

```bash
sudo visudo -f /etc/sudoers.d/comfy-orchestrator-deploy
```

Paste this single line, then save (`visudo` validates syntax before
writing, so a typo won't leave sudo broken):

```
keresh ALL=(root) NOPASSWD: /home/keresh/comfy-orchestrator/deploy/root-deploy.sh
```

Sanity check:

```bash
sudo -n /home/keresh/comfy-orchestrator/deploy/root-deploy.sh --help 2>&1 | head -1
```

`-n` fails fast instead of hanging if it would still prompt for a password.

## Usage

```bash
~/comfy-orchestrator/deploy/deploy.sh
```

Or, once the sudoers rule above is in place, ask Claude Code to run it —
it can invoke `deploy/deploy.sh` directly without you typing a password.

## Why the script isn't locked down further

`root-deploy.sh` is owned by `keresh` and editable by `keresh` (and so by
Claude Code running as `keresh`), same as everything else in this dev copy.
The NOPASSWD rule only saves a password prompt; it doesn't add a privilege
boundary that wasn't already there. If that ever stops being acceptable
(e.g. this box stops being single-user home LAN), tighten it by moving the
script to a root-owned, `keresh`-unwritable path and requiring a manual
`sudo cp` step whenever its contents change.
