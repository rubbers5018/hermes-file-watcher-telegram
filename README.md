# Hermes File-Watcher Telegram Skill

> A production-grade Hermes Agent skill that watches the `~/.hermes/` directory for file changes and pushes real-time notifications to Telegram.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Hermes Agent](https://img.shields.io/badge/Hermes-Agent-black.svg)](https://github.com/NousResearch/hermes-agent)

---

## Architecture Overview

```
+-------------+     file event      +-------------------+     HTTPS      +-----------+
| ~/.hermes/  +--------------------> | file_watcher_     +---------------> | Telegram  |
|  directory  |                     | telegram.py       |                | Bot API   |
+-------------+                     +-------------------+                +-----------+
                                         ^
                                         | loads
                              +----------+-----------+
                              | ~/.hermes/config.yaml |
                              | ~/.hermes/.env        |
                              +----------------------+
```

**Key Features:**
- **Debounced events** — Coalesces rapid-fire changes to avoid Telegram rate limits
- **Content deduplication** — SHA-256 hash tracking prevents duplicate modify notifications
- **Pattern filtering** — Include/exclude globs for fine-grained control
- **Graceful shutdown** — SIGINT/SIGTERM handling for clean daemon exits
- **Hermes-native** — Compatible with `agentskills.io` self-registration patterns
- **Structured logging** — Output format matches the Hermes execution loop

---

## Quick Start

### 1. Clone & Install

```bash
# Clone this skill repository
git clone https://github.com/rubbers5018/hermes-file-watcher-telegram.git
cd hermes-file-watcher-telegram

# Install dependencies (use uv if available, otherwise pip)
uv pip install -r requirements.txt
# or: pip install -r requirements.txt
```

### 2. Configure Secrets

```bash
# Copy the environment template to your Hermes directory
cp .env.example ~/.hermes/.env
chmod 600 ~/.hermes/.env

# Edit ~/.hermes/.env and fill in your values:
#   TELEGRAM_BOT_TOKEN=your_bot_token_from_BotFather
#   TELEGRAM_CHAT_ID=your_chat_id
```

**Get your credentials:**
- **Bot Token**: Message [@BotFather](https://t.me/BotFather) on Telegram
- **Chat ID**: Message [@userinfobot](https://t.me/userinfobot) on Telegram

### 3. Configure Hermes Webhook Route

Merge the contents of `config.yaml` into your `~/.hermes/config.yaml`:

```bash
cat config.yaml >> ~/.hermes/config.yaml
# Then edit: replace YOUR_WEBHOOK_SECRET_HERE with a strong random secret
```

### 4. Validate & Run

```bash
# Validate configuration
python sys_automation.py validate

# Send a test Telegram message
python sys_automation.py test

# Start the file-watcher daemon
python file_watcher_telegram.py
```

---

## File Reference

| File | Purpose |
|------|---------|
| `file_watcher_telegram.py` | Main daemon — watches filesystem, sends Telegram notifications |
| `sys_automation.py` | Self-registering Hermes skill with cron sync + test tools |
| `config.yaml` | Hermes `config.yaml` snippet for webhook routes |
| `.env.example` | Template for `~/.hermes/.env` secrets |
| `requirements.txt` | Python dependencies |

---

## Hermes Skill Integration

Install as a native Hermes skill:

```bash
# Copy skill into Hermes skill directory
cp file_watcher_telegram.py ~/.hermes/skills/
cp sys_automation.py ~/.hermes/skills/

# Register with Hermes
hermes skills register file_watcher_telegram
hermes skills register sys_automation
```

### Available Tool Calls

Once registered, invoke via the Hermes agent loop:

```
file_watcher_start()                    # Start the watcher daemon
file_watcher_validate_config()          # Validate .env + config.yaml
file_watcher_test_telegram()            # Send a test notification
execute_cron_sync("~/.hermes")          # Run workspace cleanup
```

---

## Validation & Deployment Commands

```bash
# --- Setup verification ---------------------------------------------------

# 1. Check Python version (>=3.11 required)
python --version

# 2. Install dependencies via uv (recommended Hermes package manager)
uv pip install watchdog requests pyyaml

# 3. Validate configuration files
python sys_automation.py validate

# 4. Test Telegram connectivity
python sys_automation.py test

# --- Staging --------------------------------------------------------------

# 5. Start the watcher in the foreground (for testing)
python file_watcher_telegram.py

# 6. Or run as a background daemon via Hermes cron
hermes cron add --name file-watcher --command "python ~/.hermes/skills/file_watcher_telegram.py" --interval 1m

# --- systemd service (Linux/WSL) ------------------------------------------

# Create a systemd user unit for persistent operation
cat > ~/.config/systemd/user/hermes-file-watcher.service << 'EOF'
[Unit]
Description=Hermes File-Watcher Telegram Skill
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.hermes/skills/file_watcher_telegram.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable hermes-file-watcher.service
systemctl --user start hermes-file-watcher.service
journalctl --user -u hermes-file-watcher -f
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Yes | Target chat ID from [@userinfobot](https://t.me/userinfobot) |
| `TELEGRAM_WEBHOOK_SECRET` | No | Secret for payload validation |

Optional dashboard auth variables are documented in `.env.example`.

---

## Configuration Options

Edit `~/.hermes/config.yaml` under the `file_watcher:` key:

```yaml
file_watcher:
  debounce_seconds: 2.0       # Event coalescing window
  include_patterns: ["*"]     # Watch all files by default
  ignore_patterns:            # Skip temp/cache directories
    - "*.tmp"
    - "*.cache"
    - "*.pyc"
    - "__pycache__/*"
    - "*.log"
    - ".git/*"
```

---

## Notification Format

Telegram messages are formatted in Markdown:

```
*🟡 Hermes File Watcher*

*Action:* `MODIFIED`
*Type:* 📄 File
*Path:* `/home/user/.hermes/config.yaml`
*Time:* `2026-06-25 04:45:12`
```

Emoji legend:
- 🟢 Created
- 🟡 Modified
- 🔴 Deleted
- 🔵 Moved

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `TELEGRAM_BOT_TOKEN not set` | Run `cp .env.example ~/.hermes/.env` and fill in your token |
| `403 Forbidden` from Telegram | Check that your bot token is valid and the bot can message the chat |
| `Chat not found` | Verify `TELEGRAM_CHAT_ID` — use [@userinfobot](https://t.me/userinfobot) |
| Duplicate notifications | This is normal — the debouncer coalesces them within `debounce_seconds` |
| Watcher not starting | Check `config.yaml` — `platforms.webhooks.enabled` must be `true` |

---

## License

MIT (C) Nous Research / Hermes Agent Stack
