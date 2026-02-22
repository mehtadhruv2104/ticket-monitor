"""Telegram + macOS desktop notifications."""

import subprocess

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_telegram(message: str) -> bool:
    """Send a message via Telegram bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured — skipping notification")
        return False
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.ok:
            log.info("Telegram message sent")
            return True
        # If Markdown parsing failed, retry as plain text
        if resp.status_code == 400 and "can't parse entities" in resp.text:
            log.info("Markdown parse failed, retrying as plain text")
            resp = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            if resp.ok:
                log.info("Telegram message sent (plain text)")
                return True
        log.warning("Telegram API error %s: %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


def send_macos_notification(title: str, message: str) -> None:
    """Trigger a macOS desktop notification with sound."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        log.debug("macOS notification triggered")
    except Exception as exc:
        log.debug("macOS notification failed: %s", exc)
