"""Shared types for the universal ticket monitor."""

import enum
from dataclasses import dataclass, field
from typing import Optional


class TicketState(enum.Enum):
    UNKNOWN = "UNKNOWN"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    COMING_SOON = "COMING_SOON"
    AVAILABLE = "AVAILABLE"
    SOLD_OUT = "SOLD_OUT"


@dataclass
class CheckResult:
    state: TicketState
    details: str = ""
    event_name: Optional[str] = None
    raw_states: dict = field(default_factory=dict)


@dataclass
class WatchEntry:
    url: str
    plugin_name: str
    event_name: str = ""
    added_at: str = ""
    last_state: str = "UNKNOWN"
    last_check: str = ""
    consecutive_failures: int = 0
