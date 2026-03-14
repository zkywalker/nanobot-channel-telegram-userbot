#!/usr/bin/env python3
"""Uninstall telegram_userbot channel from nanobot.

Removes symlinks created by install.py.  No source files are modified.
Session files in ~/.nanobot/ are preserved.

Usage:
    python uninstall.py                          # Auto-detect everything
    python uninstall.py --nanobot-dir /path/to/nanobot  # Explicit path
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent

SYMLINK_NAMES = ["telegram_userbot.py", "telegram_userbot_utils.py"]


def _find_nanobot_package_dir() -> Path | None:
    """Find the nanobot package directory via import."""
    import shutil
    nanobot_bin = shutil.which("nanobot")
    if not nanobot_bin:
        return None

    nanobot_path = Path(nanobot_bin).resolve()
    try:
        with open(nanobot_path) as f:
            first_line = f.readline().strip()
        if first_line.startswith("#!"):
            python_bin = first_line[2:].strip()
        else:
            return None
    except (OSError, UnicodeDecodeError):
        return None

    try:
        result = subprocess.run(
            [python_bin, "-c", "import nanobot; print(nanobot.__file__)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            pkg_dir = Path(result.stdout.strip()).parent
            if (pkg_dir / "channels").is_dir():
                return pkg_dir
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def find_channels_dirs(explicit: str | None = None) -> list[Path]:
    """Find all nanobot channels directories that may have our symlinks."""
    dirs: list[Path] = []

    if explicit:
        p = Path(explicit)
        channels = p / "nanobot" / "channels"
        if channels.is_dir():
            dirs.append(channels)
        else:
            raise FileNotFoundError(f"Not a valid nanobot directory: {p}")

    pkg_dir = _find_nanobot_package_dir()
    if pkg_dir:
        channels = pkg_dir / "channels"
        if channels.is_dir() and channels not in dirs:
            dirs.append(channels)

    sibling_channels = SKILL_DIR.parent / "nanobot" / "nanobot" / "channels"
    if sibling_channels.is_dir() and sibling_channels not in dirs:
        dirs.append(sibling_channels)

    if not dirs:
        raise FileNotFoundError(
            "Cannot find nanobot channels directory.\n"
            "Make sure nanobot is installed, or use --nanobot-dir."
        )

    return dirs


def _remove_symlink(target: Path) -> None:
    """Remove a single symlink."""
    if target.is_symlink():
        target.unlink()
        print(f"    Removed: {target.name}")
    elif target.exists():
        print(f"    Warning: {target.name} exists but is not a symlink. Skipping.")
    else:
        print(f"    Not found: {target.name} (already removed)")


def remove_symlinks(channels_dir: Path) -> None:
    """Remove channel module and utils symlinks."""
    for name in SYMLINK_NAMES:
        _remove_symlink(channels_dir / name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Uninstall telegram_userbot channel from nanobot")
    parser.add_argument("--nanobot-dir", type=str, default=None, help="Path to nanobot source directory")
    args = parser.parse_args()

    print("Uninstalling telegram_userbot channel...\n")

    try:
        channels_dirs = find_channels_dirs(args.nanobot_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Removing symlinks...")
    for channels_dir in channels_dirs:
        print(f"  -> {channels_dir}")
        remove_symlinks(channels_dir)

    print("\nDone! Session files in ~/.nanobot/ are preserved.")


if __name__ == "__main__":
    main()
