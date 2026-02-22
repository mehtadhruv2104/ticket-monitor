#!/usr/bin/env python3
"""BookMyShow T20 WC 2026 Ticket Monitor — polls for semi-final ticket availability and sends Telegram alerts."""

import enum
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
EVENT_URL = os.getenv(
    "EVENT_URL",
    "https://in.bookmyshow.com/sports/india-icc-men-s-t20-wc-2026/ET00473676",
)
EVENT_CODE = os.getenv("EVENT_CODE", "ET00473676")

TELEGRAM_API = "https://api.telegram.org/bot{}".format(TELEGRAM_BOT_TOKEN)

# Semi-final search terms (case-insensitive)
SEMI_FINAL_KEYWORDS = ["semi", "semi-final", "semifinal", "semi final"]
WANKHEDE_KEYWORDS = ["wankhede", "mumbai"]
# Dates: Mar 4 and Mar 5 in various formats BMS might use
SEMI_FINAL_DATE_PATTERNS = [
    "04 mar", "05 mar", "mar 04", "mar 05",
    "tue, 04 mar", "wed, 05 mar",
    "4 mar", "5 mar", "march 4", "march 5",
]

# Backoff settings
BACKOFF_FACTOR = 2
MAX_BACKOFF = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("bms-monitor")
log.setLevel(logging.DEBUG)

fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(fmt)

file_handler = logging.FileHandler("monitor.log")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(fmt)

log.addHandler(console_handler)
log.addHandler(file_handler)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State(enum.Enum):
    UNKNOWN = "UNKNOWN"              # Could not fetch page
    NO_SEMIS_LISTED = "NO_SEMIS"     # Page loaded but no semi-final matches found
    COMING_SOON = "COMING_SOON"      # Semi-final listed but not bookable yet
    AVAILABLE = "AVAILABLE"           # Tickets are bookable!
    SOLD_OUT = "SOLD_OUT"            # Sold out


previous_state = State.UNKNOWN  # type: State
check_count = 0  # type: int
start_time = datetime.now()  # type: datetime
running = True  # type: bool

# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------


def send_telegram(message):
    # type: (str) -> bool
    """Send a message via Telegram bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not configured — skipping notification")
        return False
    try:
        resp = requests.post(
            "{}/sendMessage".format(TELEGRAM_API),
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
        log.warning("Telegram API error %s: %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as exc:
        log.warning("Telegram send failed: %s", exc)
        return False


def send_macos_notification(title, message):
    # type: (str, str) -> None
    """Trigger a macOS desktop notification with sound."""
    try:
        script = 'display notification "{}" with title "{}" sound name "Glass"'.format(
            message, title
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
        log.debug("macOS notification triggered")
    except Exception as exc:
        log.debug("macOS notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Page fetching — Tier 1 (requests) and Tier 2 (Playwright)
# ---------------------------------------------------------------------------


def fetch_page_curl():
    # type: () -> Optional[str]
    """Fetch event page via curl_cffi (impersonates Chrome TLS fingerprint)."""
    try:
        resp = curl_requests.get(
            EVENT_URL,
            impersonate="chrome",
            cookies={"Rgn": "|BOM|Mumbai|"},
            timeout=20,
        )
        html = resp.text

        # Check for Cloudflare block even with curl_cffi
        cloudflare_signals = [
            "Just a moment", "Attention Required", "cf-browser-verification",
        ]
        if resp.status_code in (403, 503) or any(s in html for s in cloudflare_signals):
            log.warning("Cloudflare block even with curl_cffi (HTTP %s)", resp.status_code)
            return None

        if resp.status_code != 200:
            log.warning("HTTP %s from BMS", resp.status_code)
            return None

        return html

    except Exception as exc:
        log.warning("curl_cffi request failed: %s", exc)
        return None


def fetch_page_playwright():
    # type: () -> Optional[str]
    """Fetch event page via Playwright browser with stealth settings."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — cannot use Tier 2")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=-2400,-2400",  # offscreen
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1920, "height": 1080},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(8000)

            title = page.title()
            if "Attention Required" in title or "Just a moment" in title:
                log.info("Playwright: still blocked by Cloudflare after wait")
                browser.close()
                return None

            content = page.content()
            browser.close()
            return content
    except Exception as exc:
        log.warning("Playwright fetch failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Availability detection
# ---------------------------------------------------------------------------


def extract_match_data(html):
    # type: (str) -> List[Dict]
    """Extract structured match data from BMS page JSON embedded in HTML."""
    matches = []
    # BMS embeds match data as JSON in script tags / data attributes
    # Pattern: objects with eventCode, venue, date, etc.
    for m in re.finditer(
        r'\{[^{}]*"eventCode"\s*:\s*"(ET\d+)"[^{}]*\}', html
    ):
        try:
            # Try to parse the JSON object
            raw = m.group(0)
            data = json.loads(raw)
            matches.append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return matches


def is_semi_final_match(match_data):
    # type: (Dict) -> bool
    """Check if a match data dict refers to a Wankhede semi-final."""
    text = json.dumps(match_data).lower()
    # Check for semi-final keywords
    has_semi = any(kw in text for kw in SEMI_FINAL_KEYWORDS)
    # Check for Wankhede/Mumbai
    has_wankhede = any(kw in text for kw in WANKHEDE_KEYWORDS)
    # Check for Mar 4/5 dates
    has_date = any(d in text for d in SEMI_FINAL_DATE_PATTERNS)
    # Match if it's explicitly a semi-final, OR if it's at Wankhede on the right dates
    return has_semi or (has_wankhede and has_date)


def detect_state(html):
    # type: (str) -> Tuple[State, str]
    """Determine semi-final ticket availability. Returns (state, details)."""
    lower = html.lower()

    # First: extract structured match data and look for semi-finals
    matches = extract_match_data(html)
    semi_matches = [m for m in matches if is_semi_final_match(m)]

    if semi_matches:
        details = []
        for m in semi_matches:
            venue = m.get("venue", "?")
            date = m.get("date", "?")
            code = m.get("eventCode", "?")
            details.append("{} | {} | {}".format(date, venue, code))
        detail_str = "; ".join(details)
        log.info("Found semi-final match data: %s", detail_str)

        # Check the HTML around these matches for status
        for m in semi_matches:
            code = m.get("eventCode", "")
            # Find the section of HTML near this event code
            idx = html.find(code)
            if idx != -1:
                # Look at ~2000 chars around this match
                context = html[max(0, idx - 1000):idx + 1000].lower()
                if "sold out" in context or "housefull" in context:
                    return State.SOLD_OUT, detail_str
                if "coming soon" in context or "notify me" in context:
                    return State.COMING_SOON, detail_str

        # If semi-final data exists but no clear status, check for book buttons
        # near the match section
        return State.AVAILABLE, detail_str

    # Fallback: no structured semi-final data found.
    # Do a broad text search for any semi-final mention
    text = re.sub(r'<[^>]+>', ' ', html)
    text_lower = text.lower()

    has_semi_text = any(kw in text_lower for kw in SEMI_FINAL_KEYWORDS)
    has_wankhede_text = any(kw in text_lower for kw in WANKHEDE_KEYWORDS)
    has_date_text = any(d in text_lower for d in SEMI_FINAL_DATE_PATTERNS)

    if has_semi_text or (has_wankhede_text and has_date_text):
        # Semi-final is mentioned but not in structured data — likely just appeared
        log.info("Semi-final text found in page (no structured data yet)")

        if "sold out" in text_lower:
            return State.SOLD_OUT, "semi-final text found (sold out)"
        if "coming soon" in text_lower:
            return State.COMING_SOON, "semi-final text found (coming soon)"
        return State.AVAILABLE, "semi-final text found"

    # Count total matches shown to give useful log context
    match_count = len(matches)
    # Extract displayed dates for context
    displayed_dates = re.findall(
        r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*\d{1,2}\s*(?:Feb|Mar|Apr)',
        text
    )
    unique_dates = sorted(set(displayed_dates))

    detail = "{} matches listed, dates: {}".format(match_count, ", ".join(unique_dates) if unique_dates else "none")
    return State.NO_SEMIS_LISTED, detail


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def check_availability():
    # type: () -> Tuple[State, str, str]
    """Run a single availability check. Returns (state, details, tier_used)."""
    # Tier 1: curl_cffi (fast, impersonates Chrome TLS fingerprint)
    html = fetch_page_curl()
    if html is not None:
        state, details = detect_state(html)
        return state, details, "curl_cffi"

    # Tier 2: Playwright (heavy but reliable)
    log.info("curl_cffi failed, falling back to Playwright")
    html = fetch_page_playwright()
    if html is not None:
        state, details = detect_state(html)
        return state, details, "Playwright"

    return State.UNKNOWN, "fetch failed", "Failed"


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def handle_shutdown(signum, frame):
    global running
    running = False
    log.info("Shutdown signal received")


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    # type: () -> None
    global previous_state, check_count, start_time, running

    start_time = datetime.now()

    # Check Playwright availability
    pw_available = False
    try:
        from playwright.sync_api import sync_playwright
        pw_available = True
    except ImportError:
        pass

    log.info("=" * 60)
    log.info("BMS T20 WC 2026 Semi-Final Ticket Monitor")
    log.info("Event: %s", EVENT_URL)
    log.info("Watching for: Semi-Finals at Wankhede (Mar 4-5)")
    log.info("Poll interval: %ss", POLL_INTERVAL)
    log.info("Playwright available: %s", pw_available)
    log.info("=" * 60)

    if not pw_available:
        log.warning(
            "Playwright not available. Install with: pip install playwright && playwright install chromium"
        )
        log.warning("BMS is behind Cloudflare — Playwright is required!")

    # Validate Telegram on startup
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        ok = send_telegram(
            "\U0001f3cf *BMS Monitor Started*\n"
            "Watching: [T20 WC 2026 Semi-Finals]({url})\n"
            "Polling every {interval}s".format(url=EVENT_URL, interval=POLL_INTERVAL)
        )
        if not ok:
            log.warning("Telegram test message failed — check your credentials")
    else:
        log.warning("Telegram not configured — will only log and show macOS notifications")

    current_backoff = POLL_INTERVAL

    while running:
        check_count += 1
        state, details, tier = check_availability()

        log.info(
            "Check #%d | %s (via %s) | %s",
            check_count, state.value, tier, details,
        )

        # ALERT: Tickets available!
        if state == State.AVAILABLE and previous_state != State.AVAILABLE:
            msg = (
                "\U0001f6a8\U0001f3cf *TICKETS AVAILABLE!*\n\n"
                "T20 WC 2026 Semi-Finals at Wankhede\n"
                "[BOOK NOW \u2192 BookMyShow]({url})\n\n"
                "{details}\n"
                "Detected at check #{n}"
            ).format(url=EVENT_URL, details=details, n=check_count)
            log.critical("TICKETS AVAILABLE — sending alerts!")
            send_telegram(msg)
            send_macos_notification(
                "BMS TICKETS AVAILABLE!",
                "T20 WC 2026 Semi-Finals — Go book now!",
            )

        # HEADS UP: Semi-finals just appeared on the page (even if Coming Soon)
        if state == State.COMING_SOON and previous_state == State.NO_SEMIS_LISTED:
            msg = (
                "\u26a0\ufe0f *Semi-Finals now listed on BMS!*\n\n"
                "Status: Coming Soon (not bookable yet)\n"
                "{details}\n\n"
                "Will alert you the moment booking opens.\n"
                "[View page]({url})"
            ).format(details=details, url=EVENT_URL)
            log.warning("Semi-finals appeared on BMS — Coming Soon")
            send_telegram(msg)
            send_macos_notification(
                "BMS: Semi-Finals Listed!",
                "Coming Soon — not bookable yet",
            )

        # Notify on other state changes
        if state != previous_state and state not in (State.AVAILABLE, State.COMING_SOON):
            if previous_state != State.UNKNOWN:
                log.info("State changed: %s -> %s", previous_state.value, state.value)
                send_telegram(
                    "\u2139\ufe0f State changed: {prev} \u2192 {curr}\n{details}".format(
                        prev=previous_state.value, curr=state.value, details=details
                    )
                )

        previous_state = state

        # Backoff logic
        if tier == "Failed":
            current_backoff = min(current_backoff * BACKOFF_FACTOR, MAX_BACKOFF)
            log.info("Backing off — next check in %ds", current_backoff)
        else:
            current_backoff = POLL_INTERVAL

        # Sleep in small increments so we can respond to shutdown signals
        sleep_until = time.time() + current_backoff
        while running and time.time() < sleep_until:
            time.sleep(1)

    # Shutdown summary
    uptime = datetime.now() - start_time
    log.info("=" * 60)
    log.info("Monitor stopped")
    log.info("Total checks: %d", check_count)
    log.info("Uptime: %s", str(uptime).split(".")[0])
    log.info("Last state: %s", previous_state.value)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
