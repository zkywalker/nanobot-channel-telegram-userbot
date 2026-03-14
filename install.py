#!/usr/bin/env python3
"""Install telegram_userbot channel into nanobot.

Creates symlinks so nanobot can discover the channel module via pkgutil.
Automatically finds the nanobot site-packages directory (works with uv tool,
pip, and editable installs). Also installs Python dependencies.

Usage:
    python install.py                          # Auto-detect everything
    python install.py --nanobot-dir /path/to/nanobot  # Explicit path (source dir)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
CHANNEL_SRC = SKILL_DIR / "channel" / "telegram_userbot.py"
UTILS_SRC = SKILL_DIR / "channel" / "utils.py"

DEPENDENCIES = ["telethon", "pysocks"]


def _find_nanobot_package_dir() -> Path | None:
    """Find the nanobot package directory via import (works for any install method)."""
    try:
        # Use the same Python that nanobot runs with
        result = subprocess.run(
            ["nanobot", "--version"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None

    # Find the Python interpreter nanobot uses
    import shutil
    nanobot_bin = shutil.which("nanobot")
    if not nanobot_bin:
        return None

    nanobot_path = Path(nanobot_bin).resolve()
    # Read the shebang to find the Python interpreter
    try:
        with open(nanobot_path) as f:
            first_line = f.readline().strip()
        if first_line.startswith("#!"):
            python_bin = first_line[2:].strip()
        else:
            return None
    except (OSError, UnicodeDecodeError):
        return None

    # Ask that Python where nanobot is installed
    try:
        result = subprocess.run(
            [python_bin, "-c", "import nanobot; print(nanobot.__file__)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # nanobot/__init__.py -> nanobot/
            pkg_dir = Path(result.stdout.strip()).parent
            if (pkg_dir / "channels").is_dir():
                return pkg_dir
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def find_channels_dirs(explicit: str | None = None) -> list[Path]:
    """Find all nanobot channels directories that need symlinks.

    Returns a list of channels directories. Typically one (site-packages),
    but may include the source directory if --nanobot-dir is specified.
    """
    dirs: list[Path] = []

    # Explicit source directory
    if explicit:
        p = Path(explicit)
        channels = p / "nanobot" / "channels"
        if channels.is_dir():
            dirs.append(channels)
        else:
            raise FileNotFoundError(f"Not a valid nanobot directory: {p}")

    # Auto-detect installed package location
    pkg_dir = _find_nanobot_package_dir()
    if pkg_dir:
        channels = pkg_dir / "channels"
        if channels.is_dir() and channels not in dirs:
            dirs.append(channels)

    # Try sibling directory as fallback
    sibling_channels = SKILL_DIR.parent / "nanobot" / "nanobot" / "channels"
    if sibling_channels.is_dir() and sibling_channels not in dirs:
        dirs.append(sibling_channels)

    if not dirs:
        raise FileNotFoundError(
            "Cannot find nanobot channels directory.\n"
            "Make sure nanobot is installed (nanobot --version), or use --nanobot-dir."
        )

    return dirs


def _create_symlink(source: Path, target: Path) -> None:
    """Create a single symlink, handling existing files."""
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == source.resolve():
            print(f"    Already exists: {target.name}")
            return
        print(f"    Replacing: {target.name}")
        target.unlink()

    target.symlink_to(source)
    print(f"    Created: {target.name} -> {source}")


def install_symlinks(channels_dir: Path) -> None:
    """Create symlinks for channel module and utils."""
    _create_symlink(CHANNEL_SRC, channels_dir / "telegram_userbot.py")
    _create_symlink(UTILS_SRC, channels_dir / "telegram_userbot_utils.py")


def install_dependencies() -> None:
    """Install Python dependencies into nanobot's environment."""
    import shutil
    nanobot_bin = shutil.which("nanobot")
    if not nanobot_bin:
        print("  Warning: nanobot not found, install dependencies manually:")
        print(f"    pip install {' '.join(DEPENDENCIES)}")
        return

    # Find the Python interpreter nanobot uses
    nanobot_path = Path(nanobot_bin).resolve()
    try:
        with open(nanobot_path) as f:
            first_line = f.readline().strip()
        if first_line.startswith("#!"):
            python_bin = first_line[2:].strip()
        else:
            python_bin = sys.executable
    except (OSError, UnicodeDecodeError):
        python_bin = sys.executable

    # Try uv pip first (for uv-managed environments)
    uv_bin = shutil.which("uv")
    if uv_bin:
        try:
            result = subprocess.run(
                [uv_bin, "pip", "install", "--python", python_bin] + DEPENDENCIES,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                for dep in DEPENDENCIES:
                    print(f"    Installed: {dep}")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Fallback: pip via the nanobot Python
    try:
        result = subprocess.run(
            [python_bin, "-m", "pip", "install"] + DEPENDENCIES,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            for dep in DEPENDENCIES:
                print(f"    Installed: {dep}")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Last resort: system pip
    pip_bin = shutil.which("pip") or shutil.which("pip3")
    if pip_bin:
        try:
            result = subprocess.run(
                [pip_bin, "install"] + DEPENDENCIES,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                for dep in DEPENDENCIES:
                    print(f"    Installed: {dep}")
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    print("  Warning: could not auto-install dependencies. Install manually:")
    print(f"    pip install {' '.join(DEPENDENCIES)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install telegram_userbot channel into nanobot")
    parser.add_argument("--nanobot-dir", type=str, default=None, help="Path to nanobot source directory")
    args = parser.parse_args()

    print("Installing telegram_userbot channel...\n")

    # Step 1: Install dependencies
    print("1. Installing dependencies...")
    install_dependencies()

    # Step 2: Create symlinks
    print("\n2. Creating symlinks...")
    try:
        channels_dirs = find_channels_dirs(args.nanobot_dir)
    except FileNotFoundError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)

    for channels_dir in channels_dirs:
        print(f"  -> {channels_dir}")
        install_symlinks(channels_dir)

    print("\nDone! Next steps:")
    print("  1. python auth.py --api-id YOUR_ID --phone +YOUR_PHONE")
    print('  2. Add to ~/.nanobot/config.json:')
    print('     "channels": {')
    print('       "telegramUserbot": {')
    print('         "enabled": true,')
    print('         "apiId": YOUR_API_ID,')
    print('         "apiHash": "YOUR_API_HASH",')
    print('         "sessionName": "nanobot_userbot",')
    print('         "allowFrom": ["*"]')
    print("       }")
    print("     }")
    print("  3. nanobot gateway")


if __name__ == "__main__":
    main()
