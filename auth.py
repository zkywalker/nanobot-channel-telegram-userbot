#!/usr/bin/env python3
"""Telegram Client authentication helper.

First-time login tool that creates a persistent session file for the
telegram_client channel. Run this once before starting nanobot.

Usage:
    # Interactive login (saves .session file)
    python auth.py --api-id 12345 --phone +8613800138000

    # Export StringSession (for Docker / serverless)
    python auth.py --api-id 12345 --phone +8613800138000 --export-string

    # Custom session name (default: nanobot_client)
    python auth.py --api-id 12345 --phone +8613800138000 --session my_session

    # With proxy
    python auth.py --api-id 12345 --phone +8613800138000 --proxy socks5://127.0.0.1:1080
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

MAX_CODE_RETRIES = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Telegram Client API authentication helper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Get api_id and api_hash from https://my.telegram.org\n"
            "The session file will be saved to ~/.nanobot/<session_name>.session"
        ),
    )
    parser.add_argument("--api-id", type=int, required=True, help="API ID from my.telegram.org")
    parser.add_argument("--api-hash", type=str, default=None, help="API hash (will prompt securely if not provided)")
    parser.add_argument("--phone", type=str, required=True, help="Phone number with country code (e.g. +8613800138000)")
    parser.add_argument("--session", type=str, default="nanobot_client", help="Session name (default: nanobot_client)")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (e.g. socks5://127.0.0.1:1080)")
    parser.add_argument("--export-string", action="store_true", help="Export StringSession after login")
    return parser.parse_args()


async def authenticate(args: argparse.Namespace) -> None:
    try:
        from telethon import TelegramClient
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
        from telethon.sessions import StringSession
    except ImportError:
        print("Error: telethon is not installed. Run: pip install telethon", file=sys.stderr)
        sys.exit(1)

    # Prompt for api_hash securely if not provided via CLI
    api_hash = args.api_hash
    if not api_hash:
        api_hash = getpass.getpass("Enter API hash (input hidden): ").strip()
        if not api_hash:
            print("Error: API hash is required.", file=sys.stderr)
            sys.exit(1)

    # ToS reminder
    print("\n" + "=" * 60)
    print("IMPORTANT: Telegram Terms of Service")
    print("=" * 60)
    print("Using the Client API with a user account must comply with")
    print("Telegram's Terms of Service: https://core.telegram.org/api/terms")
    print("Automated abuse may result in account restrictions.")
    print("=" * 60)

    # Build session
    if args.export_string:
        session = StringSession()
    else:
        session_dir = Path.home() / ".nanobot"
        session_dir.mkdir(parents=True, exist_ok=True)
        session = str(session_dir / args.session)

    # Parse proxy
    proxy = None
    if args.proxy:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from channel.telegram_client import parse_proxy_url
            proxy = parse_proxy_url(args.proxy)
        except ImportError:
            from urllib.parse import urlparse
            parsed = urlparse(args.proxy)
            if parsed.scheme in ("socks5", "socks4", "http"):
                proxy = {
                    "proxy_type": parsed.scheme,
                    "addr": parsed.hostname or "127.0.0.1",
                    "port": parsed.port or 1080,
                    "rdns": True,
                }
                if parsed.username:
                    proxy["username"] = parsed.username
                if parsed.password:
                    proxy["password"] = parsed.password

    # Create client
    client_kwargs = {}
    if proxy:
        client_kwargs["proxy"] = proxy

    client = TelegramClient(session, args.api_id, api_hash, **client_kwargs)

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\nAlready authenticated as {me.first_name} (@{me.username}, ID: {me.id})")
    else:
        print(f"\nSending verification code to {args.phone}...")
        sent_code = await client.send_code_request(args.phone)

        # Retry loop for verification code
        me = None
        for attempt in range(1, MAX_CODE_RETRIES + 1):
            code = input(f"Enter the verification code (attempt {attempt}/{MAX_CODE_RETRIES}): ").strip()
            try:
                await client.sign_in(args.phone, code)
                break
            except PhoneCodeInvalidError:
                if attempt < MAX_CODE_RETRIES:
                    print("  Invalid code. Please try again.")
                    continue
                print("Error: Too many invalid code attempts.", file=sys.stderr)
                await client.disconnect()
                sys.exit(1)
            except PhoneCodeExpiredError:
                print("  Code expired. Requesting a new code...")
                sent_code = await client.send_code_request(args.phone)
                if attempt < MAX_CODE_RETRIES:
                    continue
                print("Error: Too many expired code attempts.", file=sys.stderr)
                await client.disconnect()
                sys.exit(1)
            except SessionPasswordNeededError:
                # 2FA enabled — prompt with getpass
                password = getpass.getpass("Two-factor authentication password (input hidden): ").strip()
                await client.sign_in(password=password)
                break

        me = await client.get_me()
        print(f"\nAuthenticated as {me.first_name} (@{me.username}, ID: {me.id})")

    # Secure session file permissions
    if not args.export_string:
        session_path = Path.home() / ".nanobot" / f"{args.session}.session"
        if session_path.exists():
            os.chmod(session_path, 0o600)

    if args.export_string:
        string_session = client.session.save()
        print(f"\n{'=' * 60}")
        print("StringSession (add to config as sessionString):")
        print(f"{'=' * 60}")
        print(string_session)
        print(f"{'=' * 60}")
        print("\nWARNING: Treat this string like a password!")
        print("Do NOT commit it to version control.")
    else:
        session_path = str(Path.home() / ".nanobot" / f"{args.session}.session")
        print(f"\nSession saved to: {session_path}")

    me = me or await client.get_me()
    print("\nYou can now configure nanobot with:")
    print('  "channels": {')
    print('    "telegramClient": {')
    print('      "enabled": true,')
    print(f'      "apiId": {args.api_id},')
    print(f'      "apiHash": "<your_api_hash>",')
    print(f'      "sessionName": "{args.session}",')
    print(f'      "allowFrom": ["{me.id}"]')
    print("    }")
    print("  }")

    await client.disconnect()


def main() -> None:
    args = parse_args()
    asyncio.run(authenticate(args))


if __name__ == "__main__":
    main()
