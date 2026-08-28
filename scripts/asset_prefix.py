#!/usr/bin/env python
"""Maintenance for the asset prefix block (see docs/asset-prefix-format.md).

Normally nothing here ever needs running: storage.put_object writes the prefix
when an asset is created, and there is deliberately no lazy "build it on read"
path in the app. These are the two out-of-band directions.

    backend/.venv/bin/python scripts/asset_prefix.py --status
    backend/.venv/bin/python scripts/asset_prefix.py --backfill
    backend/.venv/bin/python scripts/asset_prefix.py --strip

`--backfill` builds a prefix for any file that lacks one -- what a media
directory restored from a backup taken before this format existed would need.
`--strip` is the rollback, removing the block and leaving the original file
exactly as it was.

Both are safe to interrupt and safe to run twice: each file is rewritten through
a temporary in the same directory and renamed into place, and a file already in
the wanted state is not touched at all.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core import asset_prefix  # noqa: E402
from app.core.asset_preview import build_preview, wants_preview  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import AssetKind  # noqa: E402


def has_prefix(path: Path) -> bool:
    with path.open("rb") as f:
        return asset_prefix.parse_header(f.read(asset_prefix.HEADER_SIZE)) is not None


def _replace(path: Path, write) -> None:
    tmp = path.with_name(f".{path.name}.prefix-tmp")
    try:
        with tmp.open("wb") as dst:
            write(dst)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def strip(path: Path) -> str:
    def write(dst):
        with path.open("rb") as src:
            src.seek(asset_prefix.PREFIX_SIZE)
            while chunk := src.read(4 * 1024 * 1024):
                dst.write(chunk)

    _replace(path, write)
    return "stripped"


def backfill(path: Path) -> str:
    """Kind isn't knowable from the file, and the one distinction that would
    matter here (image vs mask) doesn't change the preview anyway -- both are
    rasters with the same descriptor layout. A mesh or anything undecodable is
    filtered out by wants_preview before we read it."""
    if not wants_preview(AssetKind.image, path):
        return "skipped (nothing to downscale)"
    data = path.read_bytes()
    built = build_preview(AssetKind.image, data, asset_prefix.PREVIEW_CAPACITY)
    if built is None:
        return "skipped (no preview)"
    block = asset_prefix.build_prefix(AssetKind.image, *built)
    _replace(path, lambda dst: (dst.write(block), dst.write(data)))
    return f"prefixed ({built[0].__len__()} B preview)"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="report only, change nothing")
    group.add_argument("--backfill", action="store_true", help="add a prefix to files without one")
    group.add_argument("--strip", action="store_true", help="remove the prefix (rollback)")
    args = parser.parse_args()

    root = Path(get_settings().media_dir)
    with_prefix = without = 0
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.startswith(".") and name.endswith("-tmp"):
                continue  # an interrupted rewrite, not an asset
            path = Path(dirpath) / name
            try:
                present = has_prefix(path)
            except OSError as exc:
                print(f"  ! {path}: {exc}")
                continue
            if present:
                with_prefix += 1
            else:
                without += 1
            if args.strip and present:
                print(f"  {path.relative_to(root)}: {strip(path)}")
            elif args.backfill and not present:
                print(f"  {path.relative_to(root)}: {backfill(path)}")

    print(f"{with_prefix} with a prefix, {without} without, under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
