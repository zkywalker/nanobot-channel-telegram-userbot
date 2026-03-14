#!/usr/bin/env python3
"""Install telegram_userbot channel into nanobot.

Creates symlinks so nanobot can discover the channel module via pkgutil.
No source-code patching required — the channel declares its own config class,
and nanobot's ChannelManager builds it from the raw JSON dict.

Usage:
    python install.py                          # Auto-detect nanobot location
    python install.py --nanobot-dir /path/to/nanobot  # Explicit path
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
CHANNEL_SRC = SKILL_DIR / "channel" / "telegram_userbot.py"
UTILS_SRC = SKILL_DIR / "channel" / "utils.py"


def find_nanobot_dir(explicit: str | None = None) -> Path:
    """Find the nanobot source directory."""
    if explicit:
        p = Path(explicit)
        if (p / "nanobot" / "channels").is_dir():
            return p
        raise FileNotFoundError(f"Not a valid nanobot directory: {p}")

    # Try sibling directory
    sibling = SKILL_DIR.parent / "nanobot"
    if (sibling / "nanobot" / "channels").is_dir():
        return sibling

    # Try to find via pip
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


def _create_symlink(source: Path, target: Path) -> None:
    """Create a single symlink, handling existing files."""
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source.resolve():
            print(f"  Symlink already exists: {target.name}")
            return
        print(f"  Warning: {target.name} already exists, replacing...")
        target.unlink()

    target.symlink_to(source)
    print(f"  Created: {target.name} -> {source}")


def install_symlinks(nanobot_dir: Path) -> None:
    """Create symlinks for channel module and utils."""
    channels_dir = nanobot_dir / "nanobot" / "channels"
    _create_symlink(CHANNEL_SRC, channels_dir / "telegram_userbot.py")
    _create_symlink(UTILS_SRC, channels_dir / "telegram_userbot_utils.py")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install telegram_userbot channel into nanobot")
    parser.add_argument("--nanobot-dir", type=str, default=None, help="Path to nanobot source directory")
    args = parser.parse_args()

    print("Installing telegram_userbot channel...\n")

    try:
        nanobot_dir = find_nanobot_dir(args.nanobot_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found nanobot at: {nanobot_dir}\n")

    print("1. Creating symlinks...")
    install_symlinks(nanobot_dir)

    print("\n2. Install dependencies:")
    print("   pip install telethon pysocks")

    print("\nDone! Next steps:")
    print("  1. pip install telethon pysocks")
    print("  2. python auth.py --api-id YOUR_ID --phone +YOUR_PHONE")
    print('  3. Add to nanobot config.json:')
    print('     "channels": {')
    print('       "telegramUserbot": {')
    print('         "enabled": true,')
    print('         "apiId": YOUR_API_ID,')
    print('         "apiHash": "YOUR_API_HASH",')
    print('         "sessionName": "nanobot_userbot",')
    print('         "allowFrom": ["*"]')
    print("       }")
    print("     }")
    print("  4. nanobot gateway")


if __name__ == "__main__":
    main()
