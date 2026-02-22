"""Page fetching: curl_cffi (Tier 1) + Playwright fallback (Tier 2)."""

import re
from typing import Optional, Tuple

from curl_cffi import requests as curl_requests

from config import log

CLOUDFLARE_SIGNALS = [
    "Just a moment",
    "Attention Required",
    "cf-browser-verification",
]


def fetch_page_curl(url: str, allow_non_200: bool = False) -> Optional[str]:
    """Fetch page via curl_cffi (impersonates Chrome TLS fingerprint)."""
    try:
        resp = curl_requests.get(url, impersonate="chrome", timeout=20)
        html = resp.text

        if resp.status_code in (403, 503) or any(s in html for s in CLOUDFLARE_SIGNALS):
            log.warning("Cloudflare block via curl_cffi (HTTP %s) for %s", resp.status_code, url)
            return None

        if resp.status_code != 200:
            if allow_non_200 and html and len(html) > 1000:
                log.info("HTTP %s from %s but got %d bytes of content", resp.status_code, url, len(html))
                return html
            log.warning("HTTP %s from %s", resp.status_code, url)
            return None

        return html
    except Exception as exc:
        log.warning("curl_cffi request failed for %s: %s", url, exc)
        return None


def fetch_page_playwright(url: str) -> Optional[str]:
    """Fetch page via Playwright browser with stealth settings."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — cannot use Tier 2")
        return None

    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=-2400,-2400",
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

            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)

            title = page.title()
            if "Attention Required" in title or "Just a moment" in title:
                log.info("Playwright: still blocked by Cloudflare for %s", url)
                browser.close()
                return None

            content = page.content()
            browser.close()
            return content
    except Exception as exc:
        log.warning("Playwright fetch failed for %s: %s", url, exc)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        return None


def fetch_page(url: str, allow_non_200: bool = False) -> Tuple[Optional[str], str]:
    """Fetch a page, trying curl_cffi first then Playwright.

    Returns (html_or_none, tier_used).
    """
    html = fetch_page_curl(url, allow_non_200=allow_non_200)
    if html is not None:
        return html, "curl_cffi"

    log.info("curl_cffi failed for %s, falling back to Playwright", url)
    html = fetch_page_playwright(url)
    if html is not None:
        return html, "Playwright"

    return None, "Failed"


def strip_html_noise(html: str) -> str:
    """Strip <style>, <script>, <svg>, and <noscript> tags + content for Claude analysis."""
    cleaned = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Collapse whitespace
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned
