"""Configuration: .env loading, logging setup, constants."""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Polling
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
BACKOFF_FACTOR = 2
MAX_BACKOFF = 600  # 10 minutes

# Paths
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.db")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("ticket-monitor")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)

_file = logging.FileHandler("monitor.log")
_file.setLevel(logging.DEBUG)
_file.setFormatter(_fmt)

log.addHandler(_console)
log.addHandler(_file)
