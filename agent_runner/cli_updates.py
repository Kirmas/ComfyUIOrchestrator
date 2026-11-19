"""Is each agent CLI up to date, and update it from the page.

Every CLI the runner drives has its own `update` subcommand and a public place
that names its newest release; each `Provider` declares both
(`update_args`, `latest_version_url`) and this module only compares and runs.
The newest version is cached (LATEST_TTL) so opening the Agent tab doesn't hit
the release servers on every poll; the installed one is read fresh each time
with `<cli> --version`, so it reflects an update done from a terminal too.

A chat already mid-turn keeps the process it started with; the new version is
picked up by the next process that chat launches. The page warns about that
before updating, it doesn't refuse: the CLIs install side by side (Claude) or
replace files a running process already has open, which is harmless on Linux.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.error
import urllib.request

LATEST_TTL = 3600
UPDATE_TIMEOUT = 600
VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_latest: dict[str, tuple[float, str | None]] = {}
_last_update: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}


def _parse(version: str | None) -> tuple[int, ...] | None:
    m = VERSION_RE.search(version or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


async def installed_version(provider) -> str | None:
    if not provider.binary:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            provider.binary, "--version",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except (OSError, asyncio.TimeoutError):
        return None
    m = VERSION_RE.search(out.decode(errors="replace"))
    return m.group(0) if m else None


def _fetch_latest(provider) -> str | None:
    request = urllib.request.Request(provider.latest_version_url, headers={"User-Agent": "comfy-agent-runner"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return provider.parse_latest_version(response.read().decode(errors="replace"))


async def latest_version(provider, refresh: bool = False) -> str | None:
    if not provider.latest_version_url:
        return None
    cached = _latest.get(provider.id)
    if cached and not refresh and time.time() - cached[0] < LATEST_TTL:
        return cached[1]
    try:
        version = await asyncio.to_thread(_fetch_latest, provider)
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        # Offline / registry hiccup: keep the previous answer if there is one.
        return cached[1] if cached else None
    _latest[provider.id] = (time.time(), version)
    return version


async def status(provider, refresh: bool = False) -> dict:
    installed, latest = await asyncio.gather(installed_version(provider), latest_version(provider, refresh))
    a, b = _parse(installed), _parse(latest)
    return {
        "provider": provider.id,
        "installed": installed,
        "latest": latest,
        "outdated": bool(a and b and a < b),
        "updating": _locks.get(provider.id, asyncio.Lock()).locked(),
        "last_update": _last_update.get(provider.id),
    }


async def run_update(provider) -> dict:
    """Run the CLI's own update command; returns the result also kept for
    the next status read. Refuses a second concurrent update of one CLI."""
    lock = _locks.setdefault(provider.id, asyncio.Lock())
    if lock.locked():
        raise RuntimeError("An update of this CLI is already running")
    async with lock:
        before = await installed_version(provider)
        try:
            proc = await asyncio.create_subprocess_exec(
                provider.binary, *provider.update_args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), UPDATE_TIMEOUT)
            code = proc.returncode
            output = out.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            code, output = -1, f"Timed out after {UPDATE_TIMEOUT} s"
        except OSError as exc:
            code, output = -1, repr(exc)
        after = await installed_version(provider)
        result = {
            "at": time.time(),
            "ok": code == 0,
            "exit_code": code,
            "before": before,
            "after": after,
            # The tail is what says why it failed; the full log can be long.
            "output": output[-4000:],
        }
        _last_update[provider.id] = result
        return result
