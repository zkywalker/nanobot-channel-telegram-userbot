#!/usr/bin/env python3
"""Uninstall telegram_client channel from nanobot.

Removes symlinks created by install.py.  No source files are modified.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent


def find_nanobot_dir(explicit: str | None = None) -> Path:
    """Find the nanobot source directory."""
    if explicit:
        p = Path(explicit)
        if (p / "nanobot" / "channels").is_dir():
            return p
        raise FileNotFoundError(f"Not a valid nanobot directory: {p}")

    sibling = SKILL_DIR.parent / "nanobot"
    if (sibling / "nanobot" / "channels").is_dir():
        return sibling

    try:
        import nanobot
        pkg_dir = Path(nanobot.__file__).parent.parent
        if (pkg_dir / "nanobot" / "channels").is_dir():
            return pkg_dir
    except ImportError:
        pass

    raise FileNotFoundError(
        "Cannot find nanobot source directory. "
        "Use --nanobot-dir to specify it explicitly."
    )


def _remove_symlink(target: Path) -> None:
    """Remove a single symlink."""
    if target.is_symlink():
        target.unlink()
        print(f"  Removed: {target.name}")
    elif target.exists():
        print(f"  Warning: {target.name} exists but is not a symlink. Skipping.")
    else:
        print(f"  Not found: {target.name}")


def remove_symlinks(nanobot_dir: Path) -> None:
    """Remove channel module and utils symlinks."""
    channels_dir = nanobot_dir / "nanobot" / "channels"
    _remove_symlink(channels_dir / "telegram_client.py")
    _remove_symlink(channels_dir / "telegram_client_utils.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Uninstall telegram_client channel from nanobot")
    parser.add_argument("--nanobot-dir", type=str, default=None, help="Path to nanobot source directory")
    args = parser.parse_args()

    print("Uninstalling telegram_client channel...\n")

    try:
        nanobot_dir = find_nanobot_dir(args.nanobot_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found nanobot at: {nanobot_dir}\n")

    print("1. Removing symlinks...")
    remove_symlinks(nanobot_dir)

    print("\nDone! Session files in ~/.nanobot/ are preserved.")


if __name__ == "__main__":
    main()
