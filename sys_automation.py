#!/usr/bin/env python3
"""
Hermes Self-Registering System Automation Skill
================================================
A cron-compatible system maintenance skill that integrates with the Hermes
Agent backend core loop. Provides automated workspace cleanup and file-state
management alongside the file-watcher telegram integration.

Intended path: ~/.hermes/skills/sys_automation.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

# Set up structured logging to match Hermes execution loop stdout/stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HERMES-SKILL] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger: logging.Logger = logging.getLogger("hermes_automation")

# ---------------------------------------------------------------------------
# Skill registry decorator — agentskills.io compatible
# ---------------------------------------------------------------------------

def register_tool(name: str, description: str) -> Callable:
    """Decorator for mimicking the Hermes / agentskills.io auto-registry pattern."""
    def decorator(func: Callable) -> Callable:
        func.__tool_name__ = name  # type: ignore[attr-defined]
        func.__tool_desc__ = description  # type: ignore[attr-defined]
        return func
    return decorator


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@register_tool(
    name="execute_cron_sync",
    description="Maintains remote workspace file states and performs scheduled system maintenance cleanup.",
)
def execute_cron_sync(target_directory: str = "~/.hermes") -> Dict[str, Any]:
    """
    Performs a systematic state evaluation. Separates core system data
    from ephemeral user-session execution caches.
    """
    expanded_path: Path = Path(target_directory).expanduser()

    if not expanded_path.is_dir():
        logger.error("Target path %s does not exist", expanded_path)
        return {"status": "error", "message": "Directory target missing."}

    try:
        # Prevent wiping core database states or secrets at rest
        protected_files: set[str] = {".env", "config.yaml", "config.yml", "memory.db"}
        protected_dirs: set[str] = {".git", "venv", ".venv", "node_modules"}
        removed_count: int = 0
        removed_items: List[str] = []

        for root, dirs, files in os.walk(expanded_path):
            # Skip protected directories
            dirs[:] = [d for d in dirs if d not in protected_dirs]

            for file in files:
                if file.endswith(".tmp") or file == "cache.json":
                    if file not in protected_files:
                        full_path = Path(root) / file
                        full_path.unlink()
                        removed_count += 1
                        removed_items.append(str(full_path))

        logger.info(
            "Cron execution cleared %d temporary session structures from %s",
            removed_count,
            expanded_path,
        )
        return {
            "status": "success",
            "cleared_files": removed_count,
            "items": removed_items,
        }

    except Exception as exc:
        logger.exception("Fail-stop triggered inside runtime worker execution loop")
        return {"status": "fail-stop", "reason": str(exc)}


@register_tool(
    name="file_watcher_validate_env",
    description="Validate that the Hermes .env file contains all required secrets.",
)
def file_watcher_validate_env() -> Dict[str, Any]:
    """Check ~/.hermes/.env for required Telegram and dashboard credentials."""
    env_path: Path = Path.home() / ".hermes" / ".env"
    required: Dict[str, str] = {
        "TELEGRAM_BOT_TOKEN": "Telegram Bot API token from @BotFather",
        "TELEGRAM_CHAT_ID": "Target Telegram chat ID for notifications",
    }
    found: Dict[str, str] = {}
    missing: Dict[str, str] = {}

    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as fh:
            env_vars: Dict[str, str] = {}
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env_vars[key.strip()] = val.strip().strip('"').strip("'")

        for key, description in required.items():
            if key in env_vars and env_vars[key]:
                found[key] = env_vars[key][:10] + "***"  # Masked
            else:
                missing[key] = description
    else:
        for key, description in required.items():
            missing[key] = description

    return {
        "status": "valid" if not missing else "invalid",
        "found": found,
        "missing": missing,
        "env_file_path": str(env_path),
        "env_file_exists": env_path.exists(),
    }


@register_tool(
    name="file_watcher_test_telegram",
    description="Send a test message to the configured Telegram chat.",
)
def file_watcher_test_telegram() -> Dict[str, Any]:
    """Verify Telegram Bot API connectivity by sending a test notification."""
    import requests  # noqa: F811 (local import for standalone usage)

    env_path: Path = Path.home() / ".hermes" / ".env"
    token: str = ""
    chat_id: str = ""

    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if key.strip() == "TELEGRAM_BOT_TOKEN":
                    token = val
                elif key.strip() == "TELEGRAM_CHAT_ID":
                    chat_id = val

    if not token or not chat_id:
        return {"status": "error", "message": "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in ~/.hermes/.env"}

    url: str = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": "🧪 *Hermes File-Watcher Test*\n\nYour Telegram integration is working correctly!",
        "parse_mode": "Markdown",
    }

    try:
        resp: requests.Response = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return {"status": "success", "message": "Test notification sent successfully", "telegram_response": resp.json()}
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram test failed: %s", exc)
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg: str = sys.argv[1]
        if arg == "validate":
            print(file_watcher_validate_env())
        elif arg == "test":
            print(file_watcher_test_telegram())
        elif arg == "cron":
            target: str = sys.argv[2] if len(sys.argv) > 2 else "~/.hermes"
            print(execute_cron_sync(target))
        else:
            print(execute_cron_sync(arg))
    else:
        print(execute_cron_sync("~/.hermes"))
