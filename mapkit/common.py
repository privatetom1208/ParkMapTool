"""Small shared helpers: cancellation and progress plumbing."""

from __future__ import annotations

import threading
from typing import Callable, Optional

# progress(fraction 0..1, message)
ProgressFn = Optional[Callable[[float, str], None]]


class Cancelled(Exception):
    """Raised inside long-running work when the user presses Cancel."""


def check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def sub_progress(progress: ProgressFn, lo: float, hi: float) -> ProgressFn:
    """Map a 0..1 progress callback onto the lo..hi slice of a parent callback."""
    if progress is None:
        return None

    def inner(frac: float, msg: str) -> None:
        progress(lo + (hi - lo) * max(0.0, min(1.0, frac)), msg)

    return inner


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"
