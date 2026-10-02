"""Helpers for publishing ten-minute satellite timelines."""

from __future__ import annotations

from datetime import datetime, timedelta

FRAME_STEP = timedelta(minutes=10)
MAX_FRAME_COUNT = 6


def frame_window(newest: datetime, count: int = MAX_FRAME_COUNT) -> list[datetime]:
    """Return consecutive frame times ending at ``newest``, oldest first."""
    if count < 1:
        raise ValueError("count must be positive")
    if newest.minute % 10 or newest.second or newest.microsecond:
        raise ValueError("Satellite frame timestamps must align to ten-minute steps")
    return [newest - FRAME_STEP * (count - index - 1) for index in range(count)]


def missing_frames(target: datetime, available: set[datetime]) -> list[datetime]:
    """Return missing frame times in the six-frame window ending at ``target``."""
    return [when for when in frame_window(target) if when not in available]


def fill_deadline(target: datetime, lag_minutes: int, owner_timeout_minutes: int) -> datetime:
    """Return when the run for ``target`` may take over missing frames."""
    return target + timedelta(minutes=lag_minutes + owner_timeout_minutes) - FRAME_STEP


def publishable_window(available: set[datetime], published_latest: datetime | None) -> list[datetime] | None:
    """Return the newest complete six-frame window after ``published_latest``."""
    for newest in sorted(available, reverse=True):
        if published_latest is not None and newest <= published_latest:
            return None
        window = frame_window(newest)
        if all(when in available for when in window):
            return window
    return None
