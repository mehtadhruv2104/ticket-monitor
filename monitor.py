#!/usr/bin/env python3
"""Universal Ticket Monitor — AI-powered plugin system.

Usage:
    python monitor.py add <url>                          # Analyze page, create/reuse plugin, add to watchlist
    python monitor.py add <url> --watch 'description'    # Track a specific event on the page
    python monitor.py list                               # Show all watched URLs + current state
    python monitor.py remove <url>                       # Remove from watchlist
    python monitor.py run                                # Start polling all watched URLs
"""

import signal
import sys
import time
from datetime import datetime

from config import BACKOFF_FACTOR, MAX_BACKOFF, POLL_INTERVAL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log
from fetcher import fetch_page, strip_html_noise
from models import CheckResult, TicketState
from notifier import send_macos_notification, send_telegram
import plugin_loader
import watchlist
from analyzer import generate_plugin

running = True


def handle_shutdown(signum, frame):
    global running
    running = False
    log.info("Shutdown signal received")


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(url: str, watch_for: str = "") -> None:
    """Add a URL to the watchlist. Generates a plugin if needed."""
    # Check if already watched
    existing = watchlist.get(url)
    if existing:
        print(f"Already watching: {url} (plugin: {existing.plugin_name})")
        return

    # When --watch is specified, always generate a fresh plugin tailored to the query
    if not watch_for:
        plugin = plugin_loader.find_plugin_for_url(url)
        if plugin:
            print(f"Matched existing plugin: {plugin.name}")
            html, tier = fetch_page(url)
            if html:
                try:
                    result = plugin.parse_fn(html, url)
                    print(f"Smoke test: {result.state.value} — {result.details}")
                except Exception as exc:
                    print(f"Warning: plugin parse failed on smoke test: {exc}")
            watchlist.add(url, plugin.name, getattr(plugin, "event_name", ""))
            print(f"Added to watchlist with plugin '{plugin.name}'")
            return

    # No existing plugin or --watch specified — generate one
    if watch_for:
        print(f"Watching for: {watch_for}")
    else:
        print(f"No existing plugin matches {url}")
    print("Fetching page...")

    html, tier = fetch_page(url, allow_non_200=True)
    if html is None:
        print(f"ERROR: Could not fetch {url} (tried curl_cffi + Playwright)")
        sys.exit(1)

    print(f"Fetched via {tier} ({len(html)} bytes)")
    print("Generating plugin with AI...")

    cleaned_html = strip_html_noise(html)
    result = generate_plugin(url, cleaned_html, watch_for=watch_for)

    if not result:
        print("ERROR: Failed to generate a plugin. Check logs for details.")
        sys.exit(1)

    platform_name = result["platform_name"]
    plugin_code = result["plugin_code"]
    event_name = result.get("event_name", "")
    confidence = result.get("confidence", 0)
    notes = result.get("notes", "")

    print(f"Generated plugin: {platform_name} (confidence: {confidence})")
    if notes:
        print(f"Strategy: {notes}")

    # Save the plugin
    plugin_loader.save_plugin(platform_name, plugin_code)

    # Smoke test: load and run against the fetched HTML
    plugin = plugin_loader.reload_plugin(platform_name)
    if not plugin:
        print("ERROR: Generated plugin failed to load after saving")
        sys.exit(1)

    try:
        check_result = plugin.parse_fn(html, url)
        print(f"Smoke test: {check_result.state.value} — {check_result.details}")
    except Exception as exc:
        print(f"Warning: smoke test parse failed: {exc}")
        print("Plugin saved but may need manual fixing.")

    # Add to watchlist
    watchlist.add(url, platform_name, event_name)
    print(f"Added to watchlist: {url}")


def cmd_list() -> None:
    """Show all watched URLs."""
    entries = watchlist.list_all()
    if not entries:
        print("Watchlist is empty. Use 'python monitor.py add <url>' to add a URL.")
        return

    print(f"{'URL':<70} {'Plugin':<20} {'State':<15} {'Last Check'}")
    print("-" * 130)
    for e in entries:
        last_check = e.last_check[:19] if e.last_check else "never"
        print(f"{e.url:<70} {e.plugin_name:<20} {e.last_state:<15} {last_check}")


def cmd_remove(url: str) -> None:
    """Remove a URL from the watchlist."""
    if watchlist.remove(url):
        print(f"Removed: {url}")
    else:
        print(f"Not found in watchlist: {url}")


def cmd_run() -> None:
    """Start polling all watched URLs."""
    global running

    entries = watchlist.list_all()
    if not entries:
        print("Watchlist is empty. Use 'python monitor.py add <url>' first.")
        return

    # Check Playwright availability
    pw_available = False
    try:
        from playwright.sync_api import sync_playwright
        pw_available = True
    except ImportError:
        pass

    log.info("=" * 60)
    log.info("Universal Ticket Monitor")
    log.info("Watching %d URLs", len(entries))
    log.info("Poll interval: %ss", POLL_INTERVAL)
    log.info("Playwright available: %s", pw_available)
    log.info("=" * 60)

    # Notify on Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url_list = "\n".join(f"- {e.url}" for e in entries)
        send_telegram(f"*Monitor Started*\nWatching {len(entries)} URLs:\n{url_list}\nPolling every {POLL_INTERVAL}s")

    # Track previous states for change detection
    prev_states: dict[str, str] = {e.url: e.last_state for e in entries}
    check_count = 0
    start_time = datetime.now()

    while running:
        # Re-read watchlist each iteration (allows add/remove from another terminal)
        entries = watchlist.list_all()
        if not entries:
            log.info("Watchlist empty, sleeping...")
            _interruptible_sleep(POLL_INTERVAL)
            continue

        check_count += 1

        for entry in entries:
            if not running:
                break

            plugin = plugin_loader.load_plugin(entry.plugin_name)
            if not plugin:
                log.warning("Plugin '%s' not found for %s — skipping", entry.plugin_name, entry.url)
                continue

            # Fetch
            html, tier = fetch_page(entry.url)
            if html is None:
                failures = watchlist.increment_failures(entry.url)
                backoff = min(POLL_INTERVAL * (BACKOFF_FACTOR ** failures), MAX_BACKOFF)
                log.warning(
                    "Check #%d | %s | FETCH FAILED (via %s) | failures: %d, backoff: %ds",
                    check_count, entry.url, tier, failures, backoff,
                )
                continue

            # Parse
            try:
                result = plugin.parse_fn(html, entry.url)
            except Exception as exc:
                log.error("Plugin '%s' crashed on %s: %s", entry.plugin_name, entry.url, exc)
                watchlist.update_state(entry.url, "UNKNOWN", reset_failures=False)
                continue

            state_str = result.state.value
            watchlist.update_state(entry.url, state_str)

            log.info(
                "Check #%d | %s | %s (via %s) | %s",
                check_count, entry.url, state_str, tier, result.details,
            )

            # State change notifications
            prev = prev_states.get(entry.url, "UNKNOWN")
            if state_str != prev:
                _handle_state_change(entry.url, prev, result)
                prev_states[entry.url] = state_str

        # Sleep between poll cycles
        _interruptible_sleep(POLL_INTERVAL)

    # Shutdown summary
    uptime = datetime.now() - start_time
    log.info("=" * 60)
    log.info("Monitor stopped")
    log.info("Total cycles: %d", check_count)
    log.info("Uptime: %s", str(uptime).split(".")[0])
    log.info("=" * 60)


def _handle_state_change(url: str, prev: str, result: CheckResult) -> None:
    """Send notifications on state changes."""
    state = result.state.value
    details = result.details
    event = result.event_name or url

    log.info("State changed for %s: %s -> %s", url, prev, state)

    if result.state == TicketState.AVAILABLE:
        msg = f"*TICKETS AVAILABLE!*\n\n{event}\n[BOOK NOW]({url})\n\n{details}"
        send_telegram(msg)
        send_macos_notification("TICKETS AVAILABLE!", event)
    elif result.state == TicketState.COMING_SOON and prev == "NOT_AVAILABLE":
        msg = f"*Event now listed!*\n\nStatus: Coming Soon\n{event}\n{details}\n\n[View page]({url})"
        send_telegram(msg)
        send_macos_notification("Event Listed!", f"{event} — Coming Soon")
    elif result.state == TicketState.SOLD_OUT:
        msg = f"*Sold Out*\n{event}\n{details}"
        send_telegram(msg)
    else:
        send_telegram(f"State changed: {prev} -> {state}\n{event}\n{details}")


def _interruptible_sleep(seconds: int) -> None:
    """Sleep in 1-second increments so we can respond to shutdown signals."""
    end = time.time() + seconds
    while running and time.time() < end:
        time.sleep(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "add":
        if len(sys.argv) < 3:
            print("Usage: python monitor.py add <url> [--watch 'description']")
            sys.exit(1)
        url = sys.argv[2]
        watch_for = ""
        if "--watch" in sys.argv:
            idx = sys.argv.index("--watch")
            if idx + 1 < len(sys.argv):
                watch_for = sys.argv[idx + 1]
            else:
                print("--watch requires a description argument")
                sys.exit(1)
        cmd_add(url, watch_for=watch_for)
    elif command == "list":
        cmd_list()
    elif command == "remove":
        if len(sys.argv) < 3:
            print("Usage: python monitor.py remove <url>")
            sys.exit(1)
        cmd_remove(sys.argv[2])
    elif command == "run":
        cmd_run()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
