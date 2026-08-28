#!/usr/bin/env python
"""Undo the asset prefix block: rewrite every file under MEDIA_DIR without it.

The rollback for core/asset_prefix.py, written before the format was first let
near the real library -- the media in it is not reproducible.

    backend/.venv/bin/python scripts/strip_asset_prefix.py --dry-run
    backend/.venv/bin/python scripts/strip_asset_prefix.py --apply

Safe to interrupt and safe to run twice: each file is rewritten through a
temporary in the same directory and renamed into place, and a file with no
magic is left completely untouched.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import get_settings  # noqa: E402
from app.core import asset_prefix  # noqa: E402


def has_prefix(path: Path) -> bool:
    with path.open("rb") as f:
        return asset_prefix.parse_header(f.read(asset_prefix.HEADER_SIZE)) is not None


def strip(path: Path) -> None:
    tmp = path.with_name(f".{path.name}.strip-tmp")
    try:
        with path.open("rb") as src, tmp.open("wb") as dst:
            src.seek(asset_prefix.PREFIX_SIZE)
            while chunk := src.read(4 * 1024 * 1024):
                dst.write(chunk)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(get_settings().media_dir)
    prefixed = total = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.startswith(".") and name.endswith("-tmp"):
                continue  # an interrupted rewrite, not an asset
            path = Path(dirpath) / name
            total += 1
            try:
                if not has_prefix(path):
                    continue
            except OSError as exc:
                print(f"  ! {path}: {exc}")
                continue
            prefixed += 1
            if args.apply:
                strip(path)
                print(f"  stripped {path.relative_to(root)}")

    verb = "stripped" if args.apply else "would strip"
    print(f"{verb} {prefixed} of {total} files under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
