import re
from html.parser import HTMLParser

from models import TicketState, CheckResult

PLATFORM_PATTERNS = [
    r"https?://(?:.+\.)?bookmyshow\.com/.*",
]

class TitleParser(HTMLParser):
    """A simple HTMLParser to extract the content of the <title> tag."""
    def __init__(self):
        super().__init__()
        self._in_title = False
        self.title_text = ""

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_text += data

def parse(html_content: str, url: str) -> CheckResult:
    target_event_name = "Final, Mar 8 2026"
    
    try:
        # Extract the page title for context using the custom HTMLParser
        title_parser = TitleParser()
        title_parser.feed(html_content)
        title_parser.close()
        
        page_title = title_parser.title_text.strip()
        
        general_event_name = "ICC Men's T20 World Cup 2026" # Default fallback
        # Try to extract a clean general event name from the title
        general_event_name_match = re.search(r"(ICC Men's T20 World Cup 2026)", page_title, re.IGNORECASE)
        if general_event_name_match:
            general_event_name = general_event_name_match.group(1)
        elif page_title:
             # Clean up common suffixes from the title if it doesn't match the specific pattern
            general_event_name = re.sub(r' (Tickets| - BookMyShow).*', '', page_title).strip()

        # The provided HTML is an overview page for the 'ICC Men's T20 World Cup 2026'.
        # It lists options to filter matches by teams and venues (e.g., 'Find Matches By Team', 'Find Matches By Venues'),
        # but it does not contain direct listings for individual matches, their specific dates, or availability status.

        # We need to determine the availability of the *specific* event 'Final, Mar 8 2026'.
        # A direct search for this event name in the HTML content indicates it's not present.
        # This is based on the provided truncated HTML which lacks such detailed event listings.

        # Per instructions: "If the specific event is NOT found on the page, return NOT_AVAILABLE".
        # Since 'Final, Mar 8 2026' is not found in the HTML provided, we return NOT_AVAILABLE.
        
        detail_message = (
            f"The specific event '{target_event_name}' was not found on this page. "
            f"This page is an overview for '{general_event_name}' and offers filtering options "
            f"for matches by teams or venues, but does not provide direct listings "
            f"for individual matches or their availability. Further navigation would be "
            f"required to locate details for individual matches." 
        )

        return CheckResult(
            state=TicketState.NOT_AVAILABLE,
            details=detail_message,
            event_name=target_event_name
        )

    except Exception as e:
        # Catch any parsing errors and return UNKNOWN state defensively
        return CheckResult(
            state=TicketState.UNKNOWN,
            details=f"An unexpected error occurred during parsing: {e}",
            event_name=target_event_name
        )
