"""Shared status constants and small data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Result statuses (see README for meanings).
ADDED = "added"
ALREADY_IN_LIBRARY = "already_in_library"
UNAVAILABLE = "unavailable"
INVALID_URL = "invalid_url"
DUPLICATE_URL = "duplicate_url"
LOGIN_REQUIRED = "login_required"
TIMEOUT = "timeout"
NETWORK_ERROR = "network_error"
UI_CHANGED = "ui_changed"
VERIFICATION_FAILED = "verification_failed"
FAILED = "failed"
PENDING = "pending"  # report-only: valid URL with no result yet

# Verified-successful outcomes. Never overwritten by a later failure.
SUCCESS = frozenset({ADDED, ALREADY_IN_LIBRARY})
# Outcomes that need no further work by default.
FINAL = SUCCESS | {UNAVAILABLE}
# Outcomes that `retry-failed` will pick up.
UNRESOLVED = frozenset(
    {LOGIN_REQUIRED, TIMEOUT, NETWORK_ERROR, UI_CHANGED, VERIFICATION_FAILED, FAILED}
)
ALL_STATUSES = FINAL | UNRESOLVED | {INVALID_URL, DUPLICATE_URL, PENDING}


def now_iso() -> str:
    """Local time, timezone-aware ISO 8601, second precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Outcome:
    """Result of one processing attempt for one URL."""

    status: str
    details: str = ""
    http_status: int | None = None
    click_attempted: bool = False
    retryable: bool = False
    retry_after: float | None = None
