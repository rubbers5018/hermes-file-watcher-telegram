#!/usr/bin/env python3
"""
Hermes Agent File-Watcher Skill
================================
Watches the ~/.hermes/ directory for file changes and pushes real-time
notifications to a Telegram webhook route.

Architecture:
    - Uses watchdog for robust cross-platform filesystem monitoring
    - Debounces rapid-fire events to avoid notification spam
    - Loads configuration from ~/.hermes/config.yaml
    - Reads Telegram secrets from ~/.hermes/.env
    - Supports filtered file patterns and ignore lists
    - Structured logging compatible with Hermes execution loop

Author: Hermes Agent Stack
License: MIT
Python: >=3.11
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import requests
import yaml
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirModifiedEvent,
    DirMovedEvent,
    FileClosedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Structured logging — matches Hermes execution loop conventions
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HERMES-WATCHER] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger: logging.Logger = logging.getLogger("hermes_file_watcher")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_PATH: Path = Path.home() / ".hermes" / "config.yaml"
ENV_PATH: Path = Path.home() / ".hermes" / ".env"
WATCH_ROOT: Path = Path.home() / ".hermes"
DEBOUNCE_SECONDS: float = 2.0
TELEGRAM_API_BASE: str = "https://api.telegram.org/bot{token}/sendMessage"


class EventAction(str, Enum):
    """Normalized action types for Telegram messages."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------
@dataclass
class WatcherConfig:
    """Runtime configuration synthesized from Hermes config files."""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_secret: str = ""
    watch_root: Path = field(default_factory=lambda: WATCH_ROOT)
    recursive: bool = True
    debounce_seconds: float = DEBOUNCE_SECONDS
    enabled: bool = True
    # Patterns
    include_patterns: List[str] = field(default_factory=lambda: ["*"])
    ignore_patterns: List[str] = field(default_factory=lambda: [
        "*.tmp", "*.cache", "*.pyc", "__pycache__/*",
        "*.log", ".git/*", "venv/*", ".venv/*", "node_modules/*",
    ])
    # Filters
    max_message_length: int = 4000
    notify_on_create: bool = True
    notify_on_modify: bool = True
    notify_on_delete: bool = True
    notify_on_move: bool = True

    @classmethod
    def from_hermes_config(cls) -> "WatcherConfig":
        """Load and merge configuration from Hermes config.yaml and .env."""
        cfg = cls()

        # 1. Parse ~/.hermes/.env for secrets
        env_vars: Dict[str, str] = {}
        if ENV_PATH.exists():
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    env_vars[key.strip()] = val.strip().strip('"').strip("'")

        cfg.telegram_bot_token = env_vars.get("TELEGRAM_BOT_TOKEN", "")
        cfg.telegram_chat_id = env_vars.get("TELEGRAM_CHAT_ID", "")
        cfg.telegram_webhook_secret = env_vars.get("TELEGRAM_WEBHOOK_SECRET", "")

        # 2. Parse ~/.hermes/config.yaml for route configuration
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data: Dict[str, Any] = yaml.safe_load(f) or {}

                platforms = data.get("platforms", {})
                webhooks = platforms.get("webhooks", {})

                if not webhooks.get("enabled", False):
                    cfg.enabled = False
                    logger.warning("Webhooks are disabled in config.yaml")

                routes = webhooks.get("routes", {})
                telegram_route = routes.get("telegram_updates", {})
                cfg.telegram_webhook_secret = telegram_route.get(
                    "secret", cfg.telegram_webhook_secret
                )

                # Optional: override watch root from config
                if "watch_root" in webhooks:
                    cfg.watch_root = Path(webhooks["watch_root"]).expanduser()

            except yaml.YAMLError as exc:
                logger.error("Failed to parse %s: %s", CONFIG_PATH, exc)
        else:
            logger.warning("Hermes config not found at %s — using defaults", CONFIG_PATH)

        # 3. Validate
        if not cfg.telegram_bot_token:
            logger.error("TELEGRAM_BOT_TOKEN not set in ~/.hermes/.env")
        if not cfg.telegram_chat_id:
            logger.error("TELEGRAM_CHAT_ID not set in ~/.hermes/.env")

        return cfg


# ---------------------------------------------------------------------------
# Debounce timer
# ---------------------------------------------------------------------------
class Debouncer:
    """Simple per-path debouncer to coalesce rapid-fire events."""

    def __init__(self, delay: float) -> None:
        self.delay: float = delay
        self._timers: Dict[str, threading.Timer] = {}
        self._lock: threading.Lock = threading.Lock()

    def call(self, key: str, fn: Callable[[], None]) -> None:
        """Schedule *fn* to run after *delay*; cancel any pending call for *key*."""
        with self._lock:
            old_timer = self._timers.pop(key, None)
            if old_timer is not None:
                old_timer.cancel()

            timer = threading.Timer(self.delay, fn)
            self._timers[key] = timer
            timer.start()


# ---------------------------------------------------------------------------
# Hermes-compatible change handler
# ---------------------------------------------------------------------------
class HermesChangeHandler(FileSystemEventHandler):
    """
    watchdog event handler that normalizes filesystem events and pushes
    structured notifications to the Telegram Bot API.
    """

    def __init__(self, config: WatcherConfig) -> None:
        self.cfg: WatcherConfig = config
        self.debouncer: Debouncer = Debouncer(config.debounce_seconds)
        self._seen_hashes: Set[str] = set()
        self._hash_lock: threading.Lock = threading.Lock()
        super().__init__()

    # -- event routing ------------------------------------------------------

    def on_created(self, event: FileSystemEvent) -> None:
        if not self.cfg.notify_on_create:
            return
        self._handle_event(event, EventAction.CREATED)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not self.cfg.notify_on_modify:
            return
        self._handle_event(event, EventAction.MODIFIED)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not self.cfg.notify_on_delete:
            return
        self._handle_event(event, EventAction.DELETED)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not self.cfg.notify_on_move:
            return
        self._handle_event(event, EventAction.MOVED)

    # -- core logic ---------------------------------------------------------

    def _should_ignore(self, path: str) -> bool:
        """Check path against include/ignore patterns."""
        for pat in self.cfg.ignore_patterns:
            if re.search(fnmatch_to_regex(pat), path):
                return True
        for pat in self.cfg.include_patterns:
            if re.search(fnmatch_to_regex(pat), path):
                return False
        return bool(self.cfg.include_patterns)

    def _handle_event(self, event: FileSystemEvent, action: EventAction) -> None:
        src: str = getattr(event, "src_path", "")
        if self._should_ignore(src):
            return

        # Deduplicate via content hash for modify events
        if action == EventAction.MODIFIED and not event.is_directory:
            try:
                file_hash = _file_hash(src)
                with self._hash_lock:
                    if file_hash in self._seen_hashes:
                        return
                    self._seen_hashes.add(file_hash)
                    # Prevent unbounded growth
                    if len(self._seen_hashes) > 10_000:
                        self._seen_hashes.clear()
            except OSError:
                pass

        key: str = f"{action.value}:{src}"
        self.debouncer.call(key, lambda: self._notify(event, action))

    def _notify(self, event: FileSystemEvent, action: EventAction) -> None:
        """Build and send the Telegram notification."""
        if not self.cfg.telegram_bot_token or not self.cfg.telegram_chat_id:
            logger.debug("Telegram credentials not configured — skipping notification")
            return

        message: str = self._format_message(event, action)
        if len(message) > self.cfg.max_message_length:
            message = message[: self.cfg.max_message_length - 3] + "..."

        payload: Dict[str, Any] = {
            "chat_id": self.cfg.telegram_chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }

        url: str = TELEGRAM_API_BASE.format(token=self.cfg.telegram_bot_token)

        try:
            resp: requests.Response = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            logger.info("Telegram notification sent: %s — %s", action.value, event.src_path)
        except requests.exceptions.RequestException as exc:
            logger.error("Failed to send Telegram notification: %s", exc)

    # -- message formatting -------------------------------------------------

    def _format_message(self, event: FileSystemEvent, action: EventAction) -> str:
        emoji_map: Dict[EventAction, str] = {
            EventAction.CREATED: "🟢",
            EventAction.MODIFIED: "🟡",
            EventAction.DELETED: "🔴",
            EventAction.MOVED: "🔵",
            EventAction.UNKNOWN: "⚪",
        }
        emoji: str = emoji_map.get(action, "⚪")
        event_type: str = "📁 Dir" if event.is_directory else "📄 File"

        lines: List[str] = [
            f"*{emoji} Hermes File Watcher*",
            f"",
            f"*Action:* `{action.value.upper()}`",
            f"*Type:* {event_type}",
            f"*Path:* `{event.src_path}`",
        ]

        if action == EventAction.MOVED and hasattr(event, "dest_path"):
            lines.append(f"*Destination:* `{event.dest_path}`")

        lines.append(f"*Time:* `{time.strftime('%Y-%m-%d %H:%M:%S')}`")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fnmatch_to_regex(pat: str) -> str:
    """Convert a shell-style glob to a regex pattern."""
    i, n = 0, len(pat)
    res = ""
    while i < n:
        c = pat[i]
        i += 1
        if c == "*":
            res += ".*"
        elif c == "?":
            res += "."
        elif c == "[":
            j = i
            if j < n and pat[j] == "!":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                res += "\\["
            else:
                stuff = pat[i:j].replace("\\", "\\\\")
                i = j + 1
                if stuff[0] == "!":
                    stuff = "^" + stuff[1:]
                elif stuff[0] == "^":
                    stuff = "\\" + stuff
                res += "[" + stuff + "]"
        else:
            res += re.escape(c)
    return res + "\Z(?ms)"


def _file_hash(path: str, blocksize: int = 65536) -> str:
    """Return SHA-256 hex digest of file contents."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(blocksize), b""):
            hasher.update(block)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
_shutdown_event: threading.Event = threading.Event()


def _on_signal(signum: int, _frame: Any) -> None:
    logger.info("Received signal %d — initiating graceful shutdown", signum)
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the file-watcher daemon."""
    logger.info("=" * 60)
    logger.info("Hermes File-Watcher Skill v1.0.0")
    logger.info("=" * 60)

    # Load configuration
    config: WatcherConfig = WatcherConfig.from_hermes_config()

    if not config.enabled:
        logger.error("File watcher is disabled in config.yaml — exiting")
        sys.exit(1)

    if not config.telegram_bot_token or not config.telegram_chat_id:
        logger.error("Telegram credentials missing — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in ~/.hermes/.env")
        sys.exit(1)

    logger.info("Watch root: %s", config.watch_root.resolve())
    logger.info("Recursive: %s", config.recursive)
    logger.info("Debounce: %.1fs", config.debounce_seconds)

    # Ensure watch root exists
    if not config.watch_root.exists():
        logger.error("Watch root does not exist: %s", config.watch_root)
        sys.exit(1)

    # Register signal handlers
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Start observer
    event_handler: HermesChangeHandler = HermesChangeHandler(config)
    observer: Observer = Observer()
    observer.schedule(
        event_handler,
        str(config.watch_root),
        recursive=config.recursive,
    )
    observer.start()
    logger.info("Observer started — press Ctrl+C to stop")

    # Block until shutdown
    try:
        while not _shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt caught")
    finally:
        observer.stop()
        observer.join()
        logger.info("Observer stopped — goodbye")


# ---------------------------------------------------------------------------
# Hermes skill self-registration decorator (agentskills.io compatible)
# ---------------------------------------------------------------------------

def register_tool(name: str, description: str) -> Callable:
    """Decorator mimicking the Hermes / agentskills.io auto-registry pattern."""
    def decorator(func: Callable) -> Callable:
        func.__tool_name__ = name  # type: ignore[attr-defined]
        func.__tool_desc__ = description  # type: ignore[attr-defined]
        return func
    return decorator


@register_tool(
    name="file_watcher_start",
    description="Start the filesystem watcher daemon that pushes notifications to Telegram.",
)
def file_watcher_start() -> Dict[str, str]:
    """Entry point for Hermes agent tool invocation."""
    try:
        main()
        return {"status": "success", "message": "File watcher daemon exited cleanly"}
    except SystemExit as exc:
        return {"status": "error", "code": str(exc.code)}
    except Exception as exc:
        logger.exception("Unexpected error in file watcher")
        return {"status": "error", "message": str(exc)}


@register_tool(
    name="file_watcher_validate_config",
    description="Validate Hermes config.yaml and .env for the file-watcher skill.",
)
def file_watcher_validate_config() -> Dict[str, Any]:
    """Validate that all required configuration is present."""
    config: WatcherConfig = WatcherConfig.from_hermes_config()
    errors: List[str] = []
    if not config.telegram_bot_token:
        errors.append("TELEGRAM_BOT_TOKEN missing in ~/.hermes/.env")
    if not config.telegram_chat_id:
        errors.append("TELEGRAM_CHAT_ID missing in ~/.hermes/.env")
    if not CONFIG_PATH.exists():
        errors.append(f"config.yaml not found at {CONFIG_PATH}")
    if not config.enabled:
        errors.append("webhooks.enabled is false in config.yaml")

    # Quick Telegram API test
    api_ok: bool = False
    if config.telegram_bot_token and config.telegram_chat_id:
        url: str = f"https://api.telegram.org/bot{config.telegram_bot_token}/getMe"
        try:
            resp: requests.Response = requests.get(url, timeout=10)
            api_ok = resp.status_code == 200
        except requests.exceptions.RequestException:
            pass

    return {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "telegram_api_reachable": api_ok,
        "watch_root": str(config.watch_root),
    }


if __name__ == "__main__":
    main()
