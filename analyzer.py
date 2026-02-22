"""Gemini API integration: analyze HTML, generate plugins, validate code."""

import ast
import json
import re
from typing import Dict, List, Optional

from google import genai

from config import GEMINI_API_KEY, log

# Imports allowed inside generated plugins
ALLOWED_IMPORTS = {"re", "json", "html", "html.parser", "urllib.parse", "models"}

# Forbidden function calls
FORBIDDEN_CALLS = {"exec", "eval", "compile", "__import__", "open", "getattr", "setattr", "delattr"}

GENERATE_PROMPT = """\
You are a plugin generator for a universal ticket monitoring system.

Given the HTML of a ticketing page and its URL, generate a Python plugin that can parse the page to determine ticket availability.

The plugin MUST export exactly these two things:

1. PLATFORM_PATTERNS = [...]  — a list of regex strings that match URLs for this platform

2. def parse(html: str, url: str) -> CheckResult:
   CRITICAL: the parse function MUST accept exactly two positional arguments: html (str) and url (str).

Available imports (ONLY these are allowed):
- re, json, html, html.parser, urllib.parse
- from models import TicketState, CheckResult

The ONLY valid TicketState values are:
- TicketState.UNKNOWN
- TicketState.NOT_AVAILABLE
- TicketState.COMING_SOON
- TicketState.AVAILABLE
- TicketState.SOLD_OUT
Do NOT use any other TicketState values. There is no NOT_TARGET_EVENT or any other value.

CheckResult constructor — ONLY these keyword arguments exist:
- state: TicketState (required)
- details: str (human-readable summary, default "")
- event_name: Optional[str] (name of the event, default None)
Do NOT pass confidence, notes, or any other keyword to CheckResult.

Example of correct usage:
  return CheckResult(state=TicketState.AVAILABLE, details="Tickets on sale", event_name="Concert Name")

Guidelines:
- Look for booking buttons, "sold out" text, "coming soon" text, price listings, etc.
- Be defensive — if parsing fails, return CheckResult(state=TicketState.UNKNOWN)
- PLATFORM_PATTERNS should match the domain broadly (not just this specific URL)
- Do NOT import os, subprocess, sys, or any other module not listed above
- Do NOT use exec, eval, open, or any dangerous functions
%s
Respond with a JSON object (no markdown fencing):
{
  "platform_name": "short_snake_case_name",
  "plugin_code": "full Python source code",
  "event_name": "name of the event from the page",
  "confidence": 0.0-1.0,
  "notes": "brief explanation of parsing strategy"
}

URL: %s

HTML (truncated):
%s
"""

FIX_PROMPT = """\
The plugin code you generated has validation errors. Please fix them and respond with the same JSON format.

Errors:
%s

Previous code:
```python
%s
```

Respond with the same JSON format as before (no markdown fencing):
{
  "platform_name": "...",
  "plugin_code": "fixed Python source code",
  "event_name": "...",
  "confidence": 0.0-1.0,
  "notes": "..."
}
"""


def validate_plugin_code(code: str) -> List[str]:
    """AST-based validation of generated plugin code. Returns list of errors."""
    errors = []

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    has_patterns = False
    has_parse = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    errors.append(f"Forbidden import: {alias.name}")

        if isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    errors.append(f"Forbidden import: from {node.module}")

        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in FORBIDDEN_CALLS:
                errors.append(f"Forbidden call: {name}()")

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PLATFORM_PATTERNS":
                    has_patterns = True

        if isinstance(node, ast.FunctionDef) and node.name == "parse":
            has_parse = True

    if not has_patterns:
        errors.append("Missing PLATFORM_PATTERNS assignment")
    if not has_parse:
        errors.append("Missing parse() function definition")

    return errors


def _parse_response(text: str) -> Optional[Dict]:
    """Extract JSON from the model's response, handling markdown fencing."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def generate_plugin(url: str, html: str, watch_for: str = "", max_retries: int = 2) -> Optional[Dict]:
    """Use Gemini to generate a plugin for the given URL and HTML.

    Returns dict with keys: platform_name, plugin_code, event_name, confidence, notes
    Or None on failure.
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set — cannot generate plugins")
        return None

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Truncate HTML to fit context window
    truncated_html = html[:500_000]

    # Build the watch-for context block
    if watch_for:
        watch_block = (
            f"\nIMPORTANT — The user is specifically watching for: {watch_for}\n"
            "The plugin must specifically track THIS event/item. The page may list many events.\n"
            "- If the specific event is NOT found on the page, return NOT_AVAILABLE\n"
            "- If found but marked 'coming soon' or 'notify me', return COMING_SOON\n"
            "- If found and bookable, return AVAILABLE\n"
            "- If found but sold out, return SOLD_OUT\n"
            "- Include the matching event details in CheckResult.details\n"
        )
    else:
        watch_block = ""

    prompt = GENERATE_PROMPT % (watch_block, url, truncated_html)

    for attempt in range(1 + max_retries):
        try:
            log.info("Calling Gemini API (attempt %d/%d)...", attempt + 1, 1 + max_retries)

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )

            result = _parse_response(response.text)
            if not result:
                log.warning("Could not parse Gemini response as JSON")
                if attempt < max_retries:
                    prompt = FIX_PROMPT % ("Response was not valid JSON", response.text[:2000])
                continue

            code = result.get("plugin_code", "")
            errors = validate_plugin_code(code)

            if not errors:
                log.info(
                    "Plugin generated: %s (confidence: %s)",
                    result.get("platform_name"),
                    result.get("confidence"),
                )
                return result

            log.warning("Validation errors in generated plugin: %s", errors)
            if attempt < max_retries:
                prompt = FIX_PROMPT % ("\n".join(errors), code)

        except Exception as exc:
            log.error("Gemini API error: %s", exc)
            return None

    log.error("Failed to generate valid plugin after %d attempts", 1 + max_retries)
    return None
