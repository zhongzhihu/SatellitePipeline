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
    """Return the newest contiguous window that safely advances the timeline.

    A cold start can publish its first available frame. Once a timeline exists,
    only publish the contiguous extension of its latest frame; this keeps
    overlapping runs from skipping a frame when a newer run finishes first.
    """
    if not available:
        return None

    if published_latest is None:
        newest = max(available)
        count = 1
        while count < MAX_FRAME_COUNT and newest - FRAME_STEP * count in available:
            count += 1
        return frame_window(newest, count)

    if published_latest not in available:
        return None

    newest = published_latest
    while newest + FRAME_STEP in available:
        newest += FRAME_STEP
    if newest == published_latest:
        return None

    count = 1
    while count < MAX_FRAME_COUNT and newest - FRAME_STEP * count in available:
        count += 1
    return frame_window(newest, count)
