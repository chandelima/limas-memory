"""Central configuration: reads environment variables (and the .env at the project root)."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Model slug on OpenRouter (override with OPENROUTER_MODEL if needed)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna")

# Permanent memory folder (Markdown only) and its git remote
MEMORY_DIR = Path(os.getenv("MEMORY_DIR", ROOT / "memory")).resolve()
MEMORY_GIT_REMOTE = os.getenv("MEMORY_GIT_REMOTE", "")
# Commit identity and token (PAT) for pushing over HTTPS. The token is not written
# to .git/config or to the remote; it is sent from memory only, on each push.
MEMORY_GIT_USER_NAME = os.getenv("MEMORY_GIT_USER_NAME", "")
MEMORY_GIT_USER_EMAIL = os.getenv("MEMORY_GIT_USER_EMAIL", "")
MEMORY_GIT_TOKEN = os.getenv("MEMORY_GIT_TOKEN", "")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Language of the fixed bot messages: detected from the user's first message, translated by the
# model and saved here (delete the file to detect it again)
LANGUAGE_FILE = Path(os.getenv("LANGUAGE_FILE", ROOT / "data" / "language.json")).resolve()

# Conversation history (JSON, outside memory/ and not versioned) and the Curator trigger
CONVERSATION_FILE = Path(os.getenv("CONVERSATION_FILE", ROOT / "data" / "conversation.json")).resolve()
CURATOR_IDLE_SECONDS = float(os.getenv("CURATOR_IDLE_SECONDS", "600"))

# Telegram bot: BotFather token and the numeric ID of the only authorized user
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_allowed = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
TELEGRAM_ALLOWED_USER_ID = int(_allowed) if _allowed.isdigit() else None

# Attachments (vouchers, photos): model that reads documents and OpenRouter PDF engine
# (the file registry lives in MEMORY_DIR/attachments/, as Markdown)
DOCUMENT_MODEL = os.getenv("DOCUMENT_MODEL", "")  # empty = uses OPENROUTER_MODEL
# Web search tool of the chat agent (OpenRouter `web` plugin; Parallel is the cheapest engine)
WEB_SEARCH = os.getenv("WEB_SEARCH", "on").strip().lower() not in ("off", "0", "false", "no", "")
WEB_SEARCH_ENGINE = os.getenv("WEB_SEARCH_ENGINE", "parallel")
WEB_SEARCH_MODE = os.getenv("WEB_SEARCH_MODE", "turbo")  # Parallel: turbo/fast (US$ 0.001), basic/advanced (US$ 0.005)
WEB_SEARCH_MAX_RESULTS = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "3") or 3)
WEB_SEARCH_MODEL = os.getenv("WEB_SEARCH_MODEL", "")  # empty = OPENROUTER_MODEL

PDF_ENGINE = os.getenv("PDF_ENGINE", "")  # empty = OpenRouter chooses; or cloudflare-ai, mistral-ocr, native

# Morning briefing on Telegram: "HH:MM" (machine time zone), "HH:MM-03" (ISO 8601: time glued to
# the UTC offset) or "HH:MM America/Recife" (zone name, follows daylight saving);
# "off" disables it. Plus the file that records the day it was last sent
BRIEFING_TIME = os.getenv("BRIEFING_TIME", "06:00")
BRIEFING_STATE_FILE = Path(os.getenv("BRIEFING_STATE_FILE", ROOT / "data" / "briefing.json")).resolve()

# Logs: console level (DEBUG, INFO, WARNING, ERROR) and the Telegram channel that receives
# warnings (numeric ID like -1001234567890 or @channelname; empty = no channel) with its minimum level
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "")
LOG_CHANNEL_HEADER = os.getenv("LOG_CHANNEL_HEADER", "")  # optional: identifies the app in a shared channel
LOG_CHANNEL_LEVEL = os.getenv("LOG_CHANNEL_LEVEL", "WARNING")
