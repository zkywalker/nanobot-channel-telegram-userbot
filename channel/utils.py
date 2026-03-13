"""Standalone Telegram Client API utilities.

Framework-agnostic helpers that can be imported by nanobot, openclaw, or any
other integration. No nanobot imports here — keep this file self-contained.

Public API:
    - split_message(content, max_len) — split long text into chunks
    - markdown_to_telegram_html(text) — Markdown → Telegram-safe HTML
    - parse_proxy_url(proxy_url) — proxy URL → Telethon proxy dict
    - detect_media_type(path) — file extension → media type string
    - get_extension(media_type, mime_type, filename) — determine file extension
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_MAX_MESSAGE_LEN = 4000
TELEGRAM_REPLY_CONTEXT_MAX_LEN = 4000

# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------


def split_message(content: str, max_len: int = 4000) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks."""
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos <= 0:
            pos = cut.rfind(" ")
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


# ---------------------------------------------------------------------------
# Markdown → Telegram HTML converter
# ---------------------------------------------------------------------------


def _strip_md(s: str) -> str:
    """Strip markdown inline formatting from text."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.strip()


def _render_table_box(table_lines: list[str]) -> str:
    """Convert markdown pipe-table to compact aligned text for <pre> display."""

    def dw(s: str) -> int:
        return sum(
            2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            for c in s
        )

    rows: list[list[str]] = []
    has_sep = False
    for line in table_lines:
        cells = [_strip_md(c) for c in line.strip().strip("|").split("|")]
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            has_sep = True
            continue
        rows.append(cells)
    if not rows or not has_sep:
        return "\n".join(table_lines)

    ncols = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (ncols - len(r)))
    widths = [max(dw(r[c]) for r in rows) for c in range(ncols)]

    def dr(cells: list[str]) -> str:
        return "  ".join(
            f"{c}{' ' * (w - dw(c))}" for c, w in zip(cells, widths)
        )

    out = [dr(rows[0])]
    out.append("  ".join("\u2500" * w for w in widths))
    for row in rows[1:]:
        out.append(dr(row))
    return "\n".join(out)


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-safe HTML.

    Handles: bold, italic, strikethrough, inline code, code blocks,
    links, headers (→ plain), blockquotes (→ plain), lists, tables (→ pre).
    """
    if not text:
        return ""

    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", save_code_block, text)

    lines = text.split("\n")
    rebuilt: list[str] = []
    li = 0
    while li < len(lines):
        if re.match(r"^\s*\|.+\|", lines[li]):
            tbl: list[str] = []
            while li < len(lines) and re.match(r"^\s*\|.+\|", lines[li]):
                tbl.append(lines[li])
                li += 1
            box = _render_table_box(tbl)
            if box != "\n".join(tbl):
                code_blocks.append(box)
                rebuilt.append(f"\x00CB{len(code_blocks) - 1}\x00")
            else:
                rebuilt.extend(tbl)
        else:
            rebuilt.append(lines[li])
            li += 1
    text = "\n".join(rebuilt)

    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(
        r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text
    )
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^[-*]\s+", "\u2022 ", text, flags=re.MULTILINE)

    for i, code in enumerate(inline_codes):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


# ---------------------------------------------------------------------------
# Proxy parser
# ---------------------------------------------------------------------------


def parse_proxy_url(proxy_url: str | None) -> dict | None:
    """Parse a proxy URL string into Telethon's proxy dict format.

    Supports: socks5://host:port, socks5://user:pass@host:port,
              http://host:port, socks4://host:port
    """
    if not proxy_url:
        return None

    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    scheme_map = {"socks5": "socks5", "socks4": "socks4", "http": "http"}
    proxy_type = scheme_map.get(scheme)
    if not proxy_type:
        logger.error("Unsupported proxy scheme '{}', ignoring proxy", scheme)
        return None

    hostname = parsed.hostname
    port = parsed.port
    if not hostname:
        logger.error("Proxy URL missing hostname: {}", proxy_url)
        return None
    if port is not None and not (1 <= port <= 65535):
        logger.error("Proxy port out of range: {}", port)
        return None

    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": hostname,
        "port": port or 1080,
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------


def detect_media_type(path: str) -> str:
    """Guess media type from file extension."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("jpg", "jpeg", "png", "gif", "webp"):
        return "photo"
    if ext == "ogg":
        return "voice"
    if ext in ("mp3", "m4a", "wav", "aac"):
        return "audio"
    if ext in ("mp4", "avi", "mkv", "mov", "webm"):
        return "video"
    return "document"


def get_extension(
    media_type: str,
    mime_type: str | None,
    filename: str | None = None,
) -> str:
    """Get file extension based on media type, MIME type, or original filename."""
    if mime_type:
        ext_map = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a", "video/mp4": ".mp4",
        }
        if mime_type in ext_map:
            return ext_map[mime_type]
    type_map = {"photo": ".jpg", "voice": ".ogg", "audio": ".mp3", "video": ".mp4", "document": ""}
    if ext := type_map.get(media_type, ""):
        return ext
    if filename:
        return "".join(Path(filename).suffixes)
    return ""
