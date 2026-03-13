"""Telegram Client API channel using Telethon (MTProto user account).

This channel allows nanobot to operate as a regular Telegram user account
instead of a bot. It uses the Telethon library to connect via MTProto protocol.

Architecture:
- Standalone utility functions live in channel/utils.py and are
  framework-agnostic — import them directly for openclaw or other integrations.
- The TelegramClientChannel class integrates with nanobot's BaseChannel.
  For other frameworks, only the nanobot imports (bottom of import section)
  need to be replaced.

WARNING: Using a user account for automated messaging may violate Telegram's
Terms of Service. Use a dedicated secondary account. Your account could be
banned or restricted. See https://core.telegram.org/api/terms
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger

try:
    from telethon import TelegramClient, events
    from telethon.errors import (
        FloodWaitError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.sessions import StringSession
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        MessageEntityMention,
        MessageEntityMentionName,
        ReactionEmoji,
    )
except ImportError:
    raise ImportError(
        "telethon is required for the telegram_client channel. "
        "Install it with: pip install telethon"
    )

# --- Shared utilities (framework-agnostic, reusable by openclaw) ---
from nanobot.channels.telegram_client_utils import (
    TELEGRAM_MAX_MESSAGE_LEN,
    TELEGRAM_REPLY_CONTEXT_MAX_LEN,
    detect_media_type,
    get_extension,
    markdown_to_telegram_html,
    parse_proxy_url,
    split_message,
)

# --- nanobot integration imports (replace these for other frameworks) ---
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir

# ---------------------------------------------------------------------------
# Plugin config (self-contained — no need to patch nanobot's schema.py)
# ---------------------------------------------------------------------------

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class TelegramClientConfig(BaseModel):
    """Telegram Client API (user account) channel configuration."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool = False
    api_id: int = 0  # From https://my.telegram.org
    api_hash: str = ""  # From https://my.telegram.org
    session_name: str = "nanobot_client"  # SQLite session file name (without .session)
    session_string: str = ""  # Pre-auth StringSession (alternative to file-based)
    phone: str = ""  # Phone number for interactive auth (e.g. +8613800138000)
    proxy: str | None = None  # Proxy URL: socks5://host:port or http://host:port
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention"] = "mention"
    reply_to_message: bool = False
    reaction_emoji: str = ""  # React to incoming messages (e.g. "👀", "👍"). Empty = disabled
    auto_disclosure: str = ""  # Text appended to every outbound message (e.g. "[AI]")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SESSION_NAME = "telegram_client"
# Messages older than this (seconds) at startup are considered stale
STALE_MESSAGE_THRESHOLD = 60
# Max recent messages to fetch for context
MAX_HISTORY_CONTEXT = 20


# ---------------------------------------------------------------------------
# TelegramClientChannel
# ---------------------------------------------------------------------------


class TelegramClientChannel(BaseChannel):
    """Telegram channel using Telethon (Client API / MTProto).

    Operates as a user account. Supports private chats, groups, supergroups,
    and forum topics.

    Compared to the Bot API channel, this channel additionally supports:
    - Message edit handling
    - Read receipts (blue double-check)
    - Emoji reactions to acknowledge messages
    - Message history retrieval (Client API exclusive)
    - Message forwarding between chats
    - Message deletion
    - Scheduled messages
    - Connection health monitoring with auto-reconnect

    WARNING: Automated use of user accounts may violate Telegram ToS.
    Use a dedicated secondary account.
    """

    name = "telegram_client"
    display_name = "Telegram Client"
    config_class = TelegramClientConfig  # Used by nanobot's plugin channel loader

    def __init__(self, config: Any, bus: MessageBus):
        super().__init__(config, bus)
        self._client: TelegramClient | None = None
        self._me: Any | None = None
        self._me_id: int | None = None
        self._me_username: str | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._media_group_buffers: dict[str, dict] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        self._message_threads: dict[tuple[str, int], int] = {}
        self._start_time: float = 0.0  # For filtering stale messages
        # Track sent message IDs for deletion support
        self._sent_messages: dict[str, list[int]] = {}  # chat_id -> [msg_ids]

    # ---- session / auth / lifecycle --------------------------------------

    def _build_session(self) -> Any:
        """Build a Telethon session object from config."""
        session_string = getattr(self.config, "session_string", "")
        if session_string:
            logger.info("Using StringSession for Telegram Client")
            return StringSession(session_string)

        session_name = getattr(self.config, "session_name", DEFAULT_SESSION_NAME)
        session_dir = self._get_session_dir()
        session_path = str(session_dir / session_name)
        logger.info("Using SQLite session: {}.session", session_path)
        return session_path

    @staticmethod
    def _get_session_dir() -> Path:
        """Get session directory with secure permissions."""
        session_dir = Path.home() / ".nanobot"
        session_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(session_dir), 0o700)
        except OSError:
            pass
        return session_dir

    def _build_proxy(self) -> dict | None:
        """Parse proxy from config."""
        proxy_val = getattr(self.config, "proxy", None)
        if isinstance(proxy_val, str):
            return parse_proxy_url(proxy_val)
        if isinstance(proxy_val, dict):
            return proxy_val
        return None

    async def _async_input(self, prompt: str) -> str:
        """Non-blocking input() that doesn't freeze the event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: input(prompt).strip())

    async def start(self) -> None:
        """Start the Telegram client and begin listening for messages."""
        api_id = getattr(self.config, "api_id", 0)
        api_hash = getattr(self.config, "api_hash", "")
        if not api_id or not api_hash:
            logger.error(
                "Telegram Client: api_id and api_hash are required. "
                "Get them from https://my.telegram.org"
            )
            return

        self._running = True
        self._start_time = time.time()
        session = self._build_session()
        proxy = self._build_proxy()

        client_kwargs: dict[str, Any] = {
            "flood_sleep_threshold": 10,  # Auto-sleep on FloodWait < 10s
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        self._client = TelegramClient(session, api_id, api_hash, **client_kwargs)

        # Connect and authenticate
        phone = getattr(self.config, "phone", "")
        try:
            await self._client.connect()

            if not await self._client.is_user_authorized():
                if not phone:
                    logger.error(
                        "Telegram Client: not authenticated and no phone number configured. "
                        "Run auth.py first or set 'phone' in config."
                    )
                    await self._client.disconnect()
                    self._running = False
                    return

                logger.info("Telegram Client: sending verification code to {}...", phone)
                await self._client.send_code_request(phone)

                code = await self._async_input(
                    f"Enter the verification code sent to {phone}: "
                )
                try:
                    await self._client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    import getpass
                    loop = asyncio.get_event_loop()
                    password = await loop.run_in_executor(
                        None, lambda: getpass.getpass("2FA password: ")
                    )
                    await self._client.sign_in(password=password)

                logger.info("Telegram Client: authenticated successfully")

        except FloodWaitError as e:
            logger.error("Telegram Client: flood wait {} seconds. Try again later.", e.seconds)
            await self._client.disconnect()
            self._running = False
            return
        except Exception as e:
            logger.error("Telegram Client: authentication failed: {}", e)
            if self._client.is_connected():
                await self._client.disconnect()
            self._running = False
            return

        # Secure session file permissions
        self._secure_session_files()

        # Get own identity for mention detection
        self._me = await self._client.get_me()
        self._me_id = self._me.id
        self._me_username = getattr(self._me, "username", None)
        logger.info(
            "Telegram Client: logged in as {} (ID: {})",
            self._me_username or self._me.first_name,
            self._me_id,
        )

        # Register event handlers
        self._client.add_event_handler(self._on_new_message, events.NewMessage)
        self._client.add_event_handler(self._on_message_edited, events.MessageEdited)
        self._client.add_event_handler(self._on_callback_query, events.CallbackQuery)

        logger.info("Telegram Client channel started")

        # Keep alive with connection health check
        try:
            while self._running:
                await asyncio.sleep(5)
                if self._client and not self._client.is_connected():
                    logger.warning("Telegram Client: connection lost, attempting reconnect...")
                    try:
                        await self._client.connect()
                        if await self._client.is_user_authorized():
                            logger.info("Telegram Client: reconnected successfully")
                        else:
                            logger.error("Telegram Client: session expired, cannot reconnect")
                            self._running = False
                    except Exception as e:
                        logger.error("Telegram Client: reconnect failed: {}", e)
                        await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    def _secure_session_files(self) -> None:
        """Set restrictive permissions on session files."""
        session_name = getattr(self.config, "session_name", DEFAULT_SESSION_NAME)
        session_file = Path.home() / ".nanobot" / f"{session_name}.session"
        if session_file.exists():
            try:
                os.chmod(str(session_file), 0o600)
            except OSError:
                pass

    async def stop(self) -> None:
        """Stop the Telegram client."""
        self._running = False

        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        for task in self._media_group_tasks.values():
            task.cancel()
        self._media_group_tasks.clear()
        self._media_group_buffers.clear()

        if self._client:
            logger.info("Stopping Telegram Client...")
            await self._client.disconnect()
            self._client = None

    # ---- allowlist / sender identity -------------------------------------

    def is_allowed(self, sender_id: str) -> bool:
        """Check allowlist with support for id|username format."""
        if super().is_allowed(sender_id):
            return True

        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            return False
        if "*" in allow_list:
            return True  # Explicit wildcard

        sender_str = str(sender_id)
        if sender_str.count("|") != 1:
            return False

        sid, username = sender_str.split("|", 1)
        if not sid.isdigit() or not username:
            return False

        return sid in allow_list or username in allow_list

    @staticmethod
    def _sender_id_str(user: Any) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        username = getattr(user, "username", None)
        return f"{sid}|{username}" if username else sid

    # ---- typing indicator ------------------------------------------------

    def _start_typing(self, chat_id: str) -> None:
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Send 'typing' action until cancelled. Uses a single long-lived context."""
        if not self._client:
            return
        try:
            entity = int(chat_id)
            # Telethon's action() context manager handles periodic re-sending internally
            cancel_event = asyncio.Event()
            async with self._client.action(entity, "typing", delay=4):
                # Block until cancelled — the context manager keeps typing alive
                try:
                    await cancel_event.wait()
                except asyncio.CancelledError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    # ---- group / mention detection ---------------------------------------

    def _has_mention(self, raw_text: str, entities: list | None) -> bool:
        """Check if the message mentions our user (case-insensitive, including caption entities)."""
        if not self._me_username:
            return False

        handle_lower = f"@{self._me_username}".lower()

        # Raw text check (case-insensitive)
        if handle_lower in (raw_text or "").lower():
            return True

        # Entity check
        for entity in entities or []:
            if isinstance(entity, MessageEntityMention):
                mention_text = raw_text[entity.offset: entity.offset + entity.length]
                if mention_text.lower() == handle_lower:
                    return True
            elif isinstance(entity, MessageEntityMentionName):
                if entity.user_id == self._me_id:
                    return True

        return False

    async def _is_for_me(self, event: Any) -> bool:
        """Determine if a group message should be processed by this channel."""
        if event.is_private:
            return True

        group_policy = getattr(self.config, "group_policy", "mention")
        if group_policy == "open":
            return True

        raw_text = event.raw_text or ""
        msg = event.message

        # Check mention in text entities
        if self._has_mention(raw_text, getattr(msg, "entities", None)):
            return True

        # Check mention in caption entities (media messages with captions)
        caption = getattr(msg, "message", "") or ""
        caption_entities = getattr(msg, "entities", None)
        # In Telethon, for media messages, entities are caption_entities on the raw TL object
        raw_msg = getattr(msg, "original_update", msg)
        if hasattr(raw_msg, "message") and hasattr(raw_msg.message, "entities"):
            tl_entities = raw_msg.message.entities
            if tl_entities and tl_entities is not getattr(msg, "entities", None):
                if self._has_mention(caption, tl_entities):
                    return True

        # Check if replying to our message
        if event.is_reply:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.sender_id == self._me_id:
                    return True
            except Exception:
                pass

        return False

    # ---- command handling (parity with Bot channel) ----------------------

    async def _handle_command(
        self, event: Any, sender_id: str, chat_id: str, command: str
    ) -> bool:
        """Handle built-in commands. Returns True if command was handled locally."""
        cmd = command.split()[0].lower().split("@")[0]  # Strip @username suffix

        if cmd == "/start":
            name = self._me_username or "nanobot"
            await self._send_text(
                int(chat_id),
                f"Hi! I'm <b>{name}</b> running as a user account.\n\n"
                f"Send me a message and I'll respond.\n"
                f"Use /help to see available commands.",
            )
            return True

        if cmd == "/help":
            await self._send_text(
                int(chat_id),
                "<b>Available commands:</b>\n"
                "\u2022 /new \u2014 Start a new conversation\n"
                "\u2022 /stop \u2014 Stop the current task\n"
                "\u2022 /restart \u2014 Restart the bot\n"
                "\u2022 /help \u2014 Show this help message",
            )
            return True

        # /new, /stop, /restart → forward to message bus
        if cmd in ("/new", "/stop", "/restart"):
            return False  # Let the normal message flow handle it

        return False

    # ---- media handling --------------------------------------------------

    def _get_media_dir(self) -> Path:
        """Get media download directory. Override-friendly for other frameworks."""
        return get_media_dir("telegram_client")

    async def _download_event_media(
        self, event_or_msg: Any, *, add_failure_content: bool = False
    ) -> tuple[list[str], list[str]]:
        """Download media from an event/message. Returns (media_paths, content_parts)."""
        if not self._client:
            return [], []

        msg = getattr(event_or_msg, "message", event_or_msg)
        if not getattr(msg, "media", None):
            return [], []

        # Detect media type, including previously-ignored types
        media_type = "file"
        if getattr(msg, "photo", None):
            media_type = "image"
        elif getattr(msg, "voice", None):
            media_type = "voice"
        elif getattr(msg, "audio", None):
            media_type = "audio"
        elif getattr(msg, "video", None):
            media_type = "video"
        elif getattr(msg, "video_note", None):
            media_type = "video"
        elif getattr(msg, "gif", None):
            media_type = "animation"
        elif getattr(msg, "sticker", None):
            # Don't download sticker, but provide content tag
            sticker = msg.sticker
            alt = getattr(sticker, "alt", None) or ""
            return [], [f"[sticker: {alt}]" if alt else "[sticker]"]
        elif getattr(msg, "document", None):
            media_type = "file"
        elif getattr(msg, "poll", None):
            question = getattr(msg.poll, "question", None)
            q_text = getattr(question, "text", str(question)) if question else "?"
            return [], [f"[poll: {q_text}]"]
        elif getattr(msg, "geo", None) or getattr(msg, "venue", None):
            geo = getattr(msg, "geo", None) or getattr(getattr(msg, "venue", None), "geo", None)
            if geo:
                return [], [f"[location: {geo.lat}, {geo.long}]"]
            return [], ["[location]"]
        elif getattr(msg, "contact", None):
            c = msg.contact
            name = f"{getattr(c, 'first_name', '')} {getattr(c, 'last_name', '')}".strip()
            phone = getattr(c, "phone_number", "")
            info = f"{name} {phone}".strip() if phone else (name or "unknown")
            return [], [f"[contact: {info}]"]
        else:
            return [], []

        try:
            media_dir = self._get_media_dir()
            file_path = await self._client.download_media(msg, file=str(media_dir))
            if not file_path:
                if add_failure_content:
                    return [], [f"[{media_type}: download failed]"]
                return [], []

            path_str = str(file_path)

            if media_type in ("voice", "audio"):
                transcription = await self.transcribe_audio(file_path)
                if transcription:
                    logger.info("Transcribed {}: {}...", media_type, transcription[:50])
                    return [path_str], [f"[transcription: {transcription}]"]
                return [path_str], [f"[{media_type}: {path_str}]"]

            return [path_str], [f"[{media_type}: {path_str}]"]
        except Exception as e:
            logger.warning("Failed to download media: {}", e)
            if add_failure_content:
                return [], [f"[{media_type}: download failed]"]
            return [], []

    @staticmethod
    def _extract_reply_context(reply_msg: Any) -> str | None:
        """Extract text from the message being replied to (handles text + caption)."""
        if not reply_msg:
            return None
        # Telethon: .text includes caption for media messages; .raw_text is without formatting
        text = getattr(reply_msg, "text", None) or getattr(reply_msg, "message", None) or ""
        if len(text) > TELEGRAM_REPLY_CONTEXT_MAX_LEN:
            text = text[:TELEGRAM_REPLY_CONTEXT_MAX_LEN] + "..."
        return f"[Reply to: {text}]" if text else None

    # ---- topic / thread tracking -----------------------------------------

    @staticmethod
    def _derive_topic_session_key(event: Any) -> str | None:
        """Derive topic-scoped session key for forum/group topics."""
        msg = getattr(event, "message", event)
        reply_to = getattr(msg, "reply_to", None)
        if not reply_to:
            return None
        forum_topic = getattr(reply_to, "forum_topic", False)
        if not forum_topic:
            return None
        topic_id = getattr(reply_to, "reply_to_top_id", None) or getattr(
            reply_to, "reply_to_msg_id", None
        )
        if topic_id is None:
            return None
        chat_id = event.chat_id
        return f"telegram_client:{chat_id}:topic:{topic_id}"

    def _get_thread_id(self, chat_id: str, message_id: int | None) -> int | None:
        """Look up cached topic thread ID for outbound routing."""
        if message_id is None:
            return None
        return self._message_threads.get((chat_id, message_id))

    def _remember_thread_context(self, event: Any) -> None:
        """Cache topic thread id by chat/message id for follow-up replies."""
        msg = getattr(event, "message", event)
        reply_to = getattr(msg, "reply_to", None)
        if not reply_to:
            return
        topic_id = getattr(reply_to, "reply_to_top_id", None)
        if topic_id is None:
            return
        key = (str(event.chat_id), msg.id)
        self._message_threads[key] = topic_id
        if len(self._message_threads) > 2000:
            self._message_threads.pop(next(iter(self._message_threads)))

    # ---- metadata --------------------------------------------------------

    @staticmethod
    def _build_message_metadata(event: Any, sender: Any) -> dict[str, Any]:
        """Build metadata payload for inbound messages."""
        msg = getattr(event, "message", event)
        is_private = event.is_private if hasattr(event, "is_private") else True
        reply_to = getattr(msg, "reply_to", None)
        reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None

        return {
            "message_id": msg.id,
            "user_id": sender.id if sender else None,
            "username": getattr(sender, "username", None),
            "first_name": getattr(sender, "first_name", None),
            "is_group": not is_private,
            "message_thread_id": getattr(
                reply_to, "reply_to_top_id", None
            ) if reply_to else None,
            "is_forum": bool(reply_to and getattr(reply_to, "forum_topic", False)),
            "reply_to_message_id": reply_to_msg_id,
        }

    # ---- inbound message handler -----------------------------------------

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Handle incoming messages from Telegram."""
        # Skip messages from ourselves
        if event.sender_id == self._me_id:
            return

        # Drop stale messages from before startup
        msg_date = getattr(event.message, "date", None)
        if msg_date and self._start_time:
            msg_timestamp = msg_date.timestamp()
            if msg_timestamp < self._start_time - STALE_MESSAGE_THRESHOLD:
                return

        # Get sender info
        sender = await event.get_sender()
        if not sender:
            return

        sender_id = self._sender_id_str(sender)
        chat_id = str(event.chat_id)

        self._remember_thread_context(event)

        # Recognize slash commands
        raw_text = event.raw_text or ""

        # Group message filtering
        if not await self._is_for_me(event):
            return

        # Handle built-in commands (/start, /help)
        if raw_text.startswith("/") and len(raw_text) > 1:
            handled = await self._handle_command(event, sender_id, chat_id, raw_text)
            if handled:
                await self._mark_read(event)
                return

        # Send reaction to acknowledge receipt (configurable)
        await self._react_to_message(event)

        # Build content
        content_parts: list[str] = []
        media_paths: list[str] = []

        if raw_text:
            content_parts.append(raw_text)

        # Download media
        current_media, current_media_parts = await self._download_event_media(
            event, add_failure_content=True
        )
        media_paths.extend(current_media)
        content_parts.extend(current_media_parts)
        if current_media:
            logger.debug("Downloaded message media to {}", current_media[0])

        # Reply context
        if event.is_reply:
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg:
                    reply_ctx = self._extract_reply_context(reply_msg)
                    reply_media, reply_media_parts = await self._download_event_media(reply_msg)
                    if reply_media:
                        media_paths = reply_media + media_paths
                    tag = reply_ctx or (
                        f"[Reply to: {reply_media_parts[0]}]" if reply_media_parts else None
                    )
                    if tag:
                        content_parts.insert(0, tag)
            except Exception as e:
                logger.debug("Failed to get reply message: {}", e)

        content = "\n".join(content_parts) if content_parts else "[empty message]"
        logger.debug("Telegram Client message from {}: {}...", sender_id, content[:50])

        metadata = self._build_message_metadata(event, sender)
        session_key = self._derive_topic_session_key(event)

        # Media group buffering
        media_group_id = getattr(event.message, "grouped_id", None)
        if media_group_id:
            key = f"{chat_id}:{media_group_id}"
            if key not in self._media_group_buffers:
                self._media_group_buffers[key] = {
                    "sender_id": sender_id, "chat_id": chat_id,
                    "contents": [], "media": [],
                    "metadata": metadata, "session_key": session_key,
                }
                self._start_typing(chat_id)
            buf = self._media_group_buffers[key]
            if content and content != "[empty message]":
                buf["contents"].append(content)
            buf["media"].extend(media_paths)
            if key not in self._media_group_tasks:
                self._media_group_tasks[key] = asyncio.create_task(
                    self._flush_media_group(key)
                )
            return

        self._start_typing(chat_id)

        # Mark message as read (send read receipt)
        await self._mark_read(event)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    async def _on_message_edited(self, event: events.MessageEdited.Event) -> None:
        """Handle edited messages — treat as a new message with [edited] tag."""
        if event.sender_id == self._me_id:
            return
        if not await self._is_for_me(event):
            return

        sender = await event.get_sender()
        if not sender:
            return

        raw_text = event.raw_text or ""
        if not raw_text:
            return  # Only handle text edits

        sender_id = self._sender_id_str(sender)
        chat_id = str(event.chat_id)
        content = f"[edited message] {raw_text}"
        metadata = self._build_message_metadata(event, sender)
        session_key = self._derive_topic_session_key(event)

        self._start_typing(chat_id)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=[],
            metadata=metadata,
            session_key=session_key,
        )

    async def _on_callback_query(self, event: events.CallbackQuery.Event) -> None:
        """Handle inline button callback queries (if we ever send inline keyboards)."""
        try:
            await event.answer()
        except Exception:
            pass

    async def _mark_read(self, event: Any) -> None:
        """Send read receipt for the message."""
        if not self._client:
            return
        try:
            await self._client.send_read_acknowledge(event.chat_id, event.message)
        except Exception:
            pass  # Non-critical — silently ignore

    async def _react_to_message(self, event: Any) -> None:
        """React to incoming message with emoji if configured.

        Config option: reaction_emoji (str, e.g. "👀" or "👍")
        Set to empty string or omit to disable.
        """
        if not self._client:
            return
        emoji = getattr(self.config, "reaction_emoji", "")
        if not emoji:
            return
        try:
            await self._client(SendReactionRequest(
                peer=event.chat_id,
                msg_id=event.message.id,
                reaction=[ReactionEmoji(emoticon=emoji)],
            ))
        except Exception as e:
            logger.debug("Failed to react to message: {}", e)

    async def _flush_media_group(self, key: str) -> None:
        """Wait briefly, then forward buffered media-group as one turn."""
        try:
            await asyncio.sleep(0.6)
            buf = self._media_group_buffers.pop(key, None)
            if not buf:
                return
            content = "\n".join(buf["contents"]) or "[empty message]"
            await self._handle_message(
                sender_id=buf["sender_id"], chat_id=buf["chat_id"],
                content=content, media=list(dict.fromkeys(buf["media"])),
                metadata=buf["metadata"], session_key=buf.get("session_key"),
            )
        finally:
            self._media_group_tasks.pop(key, None)

    # ---- Client API exclusive capabilities -------------------------------

    async def get_message_history(
        self,
        chat_id: int | str,
        limit: int = MAX_HISTORY_CONTEXT,
        offset_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieve recent message history from a chat.

        Client API exclusive — Bot API cannot access message history.
        Useful for providing conversation context to the AI.

        Returns list of dicts with keys: id, sender_id, text, date, media_type.
        """
        if not self._client:
            return []

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id
        messages: list[dict[str, Any]] = []

        try:
            async for msg in self._client.iter_messages(
                entity, limit=limit, offset_id=offset_id
            ):
                media_type = None
                if getattr(msg, "photo", None):
                    media_type = "photo"
                elif getattr(msg, "voice", None):
                    media_type = "voice"
                elif getattr(msg, "video", None):
                    media_type = "video"
                elif getattr(msg, "document", None):
                    media_type = "document"

                messages.append({
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "text": msg.text or "",
                    "date": msg.date.isoformat() if msg.date else None,
                    "media_type": media_type,
                    "is_outgoing": msg.out,
                })
        except FloodWaitError as fw:
            logger.warning("Flood wait {} seconds fetching history", fw.seconds)
        except Exception as e:
            logger.warning("Failed to fetch message history: {}", e)

        return messages

    async def forward_message(
        self,
        from_chat_id: int | str,
        to_chat_id: int | str,
        message_ids: list[int],
    ) -> bool:
        """Forward messages between chats.

        Client API exclusive — much more flexible than Bot API forwarding.
        Can forward from any accessible chat, including channels and groups.
        """
        if not self._client:
            return False

        from_entity = int(from_chat_id) if isinstance(from_chat_id, str) else from_chat_id
        to_entity = int(to_chat_id) if isinstance(to_chat_id, str) else to_chat_id

        try:
            await self._client.forward_messages(to_entity, message_ids, from_entity)
            return True
        except FloodWaitError as fw:
            logger.error("Flood wait {} seconds forwarding messages", fw.seconds)
        except Exception as e:
            logger.error("Failed to forward messages: {}", e)
        return False

    async def delete_messages(
        self,
        chat_id: int | str,
        message_ids: list[int],
        revoke: bool = True,
    ) -> bool:
        """Delete messages from a chat.

        Client API exclusive — can delete own messages in any chat,
        and others' messages in groups where we are admin.
        revoke=True deletes for everyone.
        """
        if not self._client:
            return False

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id

        try:
            await self._client.delete_messages(entity, message_ids, revoke=revoke)
            return True
        except Exception as e:
            logger.error("Failed to delete messages: {}", e)
        return False

    async def send_scheduled(
        self,
        chat_id: int | str,
        text: str,
        schedule_time: datetime,
    ) -> bool:
        """Send a scheduled message.

        Client API exclusive — Bot API has no scheduling support.
        The message will be delivered at the specified time.
        """
        if not self._client:
            return False

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id

        try:
            html = markdown_to_telegram_html(text)
            await self._client.send_message(
                entity, html, parse_mode="html",
                schedule=schedule_time,
            )
            return True
        except Exception as e:
            logger.error("Failed to send scheduled message: {}", e)
        return False

    async def pin_message(
        self,
        chat_id: int | str,
        message_id: int,
        notify: bool = False,
    ) -> bool:
        """Pin a message in a chat.

        Works in groups/channels where we have admin rights.
        """
        if not self._client:
            return False

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id

        try:
            await self._client.pin_message(entity, message_id, notify=notify)
            return True
        except Exception as e:
            logger.error("Failed to pin message: {}", e)
        return False

    async def search_messages(
        self,
        chat_id: int | str,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search messages in a chat.

        Client API exclusive — Bot API has no search capability.
        """
        if not self._client:
            return []

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id
        results: list[dict[str, Any]] = []

        try:
            async for msg in self._client.iter_messages(
                entity, search=query, limit=limit
            ):
                results.append({
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "text": msg.text or "",
                    "date": msg.date.isoformat() if msg.date else None,
                })
        except Exception as e:
            logger.warning("Failed to search messages: {}", e)

        return results

    async def get_participants(
        self,
        chat_id: int | str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get participants of a group/channel.

        Client API exclusive — can access full participant list.
        """
        if not self._client:
            return []

        entity = int(chat_id) if isinstance(chat_id, str) else chat_id
        participants: list[dict[str, Any]] = []

        try:
            async for user in self._client.iter_participants(entity, limit=limit):
                participants.append({
                    "id": user.id,
                    "username": getattr(user, "username", None),
                    "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None),
                    "is_bot": getattr(user, "bot", False),
                })
        except Exception as e:
            logger.warning("Failed to get participants: {}", e)

        return participants

    async def get_dialogs(self, limit: int = 30) -> list[dict[str, Any]]:
        """List recent conversations/dialogs.

        Client API exclusive — Bot API has no concept of dialogs.
        Returns list of chats ordered like the official Telegram app.
        """
        if not self._client:
            return []

        dialogs: list[dict[str, Any]] = []

        try:
            async for dialog in self._client.iter_dialogs(limit=limit):
                dialogs.append({
                    "id": dialog.id,
                    "name": dialog.name,
                    "is_group": dialog.is_group,
                    "is_channel": dialog.is_channel,
                    "unread_count": dialog.unread_count,
                    "last_message": dialog.message.text[:100] if dialog.message and dialog.message.text else None,
                    "date": dialog.date.isoformat() if dialog.date else None,
                })
        except Exception as e:
            logger.warning("Failed to get dialogs: {}", e)

        return dialogs

    # ---- outbound message sending ----------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram Client API."""
        if not self._client:
            logger.warning("Telegram Client not running")
            return

        if not msg.metadata.get("_progress", False):
            self._stop_typing(msg.chat_id)

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error("Invalid chat_id: {}", msg.chat_id)
            return

        # Resolve reply_to and topic thread
        reply_to_msg_id = msg.metadata.get("message_id") if getattr(
            self.config, "reply_to_message", False
        ) else None

        # Forum topic routing: use cached thread ID for correct topic
        message_thread_id = msg.metadata.get("message_thread_id")
        if message_thread_id is None and reply_to_msg_id is not None:
            message_thread_id = self._get_thread_id(msg.chat_id, reply_to_msg_id)

        # In Telethon, to send to a topic, reply_to must be the topic's root message ID
        effective_reply_to = reply_to_msg_id
        if message_thread_id and not effective_reply_to:
            effective_reply_to = message_thread_id

        # Append AI disclosure if configured
        disclosure = getattr(self.config, "auto_disclosure", "")

        # Send media files
        for media_path in msg.media or []:
            try:
                media_type = detect_media_type(media_path)
                send_kwargs: dict[str, Any] = {
                    "reply_to": effective_reply_to,
                }
                # Voice messages need explicit flag
                if media_type == "voice":
                    send_kwargs["voice_note"] = True
                # Video messages — send with supports_streaming for inline playback
                elif media_type == "video":
                    send_kwargs["supports_streaming"] = True
                sent = await self._client.send_file(chat_id, media_path, **send_kwargs)
                self._track_sent_message(msg.chat_id, sent.id)
            except FloodWaitError as fw:
                logger.error("Flood wait {} seconds sending media to {}", fw.seconds, chat_id)
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                logger.error("Failed to send media {}: {}", media_path, e)
                try:
                    await self._client.send_message(
                        chat_id, f"[Failed to send: {filename}]",
                        reply_to=effective_reply_to,
                    )
                except Exception:
                    pass

        # Send text content
        if msg.content and msg.content != "[empty message]":
            text = msg.content
            if disclosure:
                text = f"{text}\n\n{disclosure}"
            for chunk in split_message(text, TELEGRAM_MAX_MESSAGE_LEN):
                sent_id = await self._send_text(chat_id, chunk, effective_reply_to)
                if sent_id:
                    self._track_sent_message(msg.chat_id, sent_id)

    def _track_sent_message(self, chat_id: str, msg_id: int) -> None:
        """Track sent message IDs for potential deletion later."""
        if chat_id not in self._sent_messages:
            self._sent_messages[chat_id] = []
        self._sent_messages[chat_id].append(msg_id)
        # Keep only last 100 per chat
        if len(self._sent_messages[chat_id]) > 100:
            self._sent_messages[chat_id] = self._sent_messages[chat_id][-100:]

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
    ) -> int | None:
        """Send text message with HTML formatting, falling back to plain text.

        Returns the sent message ID, or None on failure.
        """
        if not self._client:
            return None
        try:
            html = markdown_to_telegram_html(text)
            sent = await self._client.send_message(
                chat_id, html, parse_mode="html",
                reply_to=reply_to, link_preview=False,
            )
            return sent.id
        except FloodWaitError as fw:
            logger.error("Flood wait: {} seconds. Message to {} delayed.", fw.seconds, chat_id)
            # Telethon auto-sleeps if flood_sleep_threshold is set; message already sent on retry
            return None
        except Exception as e:
            logger.warning("HTML send failed, falling back to plain text: {}", e)
            try:
                sent = await self._client.send_message(
                    chat_id, text, reply_to=reply_to, link_preview=False,
                )
                return sent.id
            except FloodWaitError as fw:
                logger.error("Flood wait: {} seconds. Message to {} dropped.", fw.seconds, chat_id)
            except Exception as e2:
                logger.error("Error sending Telegram Client message: {}", e2)
        return None
