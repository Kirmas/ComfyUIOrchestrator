"""Locating the Blender binary -- the one thing live scenes (core/blender_scene.py)
and the agent task (core/agent_task.py) share.

Blender is an optional system dependency. Where it is not found, the Scenes
page and its API report `available: false` and the agent task leaves Blender out
of its instructions, rather than offering something that can only fail.
"""
import os
import shutil

from app.config import get_settings


def find_blender_binary() -> str | None:
    """BLENDER_PATH (.env) wins; then PATH; then the conventional /opt/blender
    unpack location of the official tarball."""
    for candidate in (get_settings().blender_path, shutil.which("blender"), "/opt/blender/blender"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None
