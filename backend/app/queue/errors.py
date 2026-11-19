"""Exceptions a task handler raises to tell the worker *what kind* of outcome this is.

Anything else a handler raises is treated as a transient failure and retried with backoff (REQUIREMENTS_AUTONOMY §E).
"""

from __future__ import annotations

from datetime import datetime


class JobError(Exception):
    """Base class for queue control-flow exceptions."""


class RetryableJobError(JobError):
    """Transient failure. ``retry_after`` (seconds) carries a provider's ``Retry-After`` and is always honoured."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentJobError(JobError):
    """Retrying cannot help (bad input, 4xx that is our fault). Goes straight to the dead-letter list."""


class ParkJob(JobError):
    """Not a failure: the task can't run *yet* (provider rate-limited / exhausted, waiting for a human).

    The job goes back to the queue for ``until`` / ``delay_seconds`` **without consuming an attempt** — so a quota
    that resets tomorrow never dead-letters a healthy task (REQUIREMENTS_AUTONOMY §A: pause only the affected task).
    """

    def __init__(
        self,
        reason: str,
        *,
        until: datetime | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        super().__init__(reason)
        if (until is None) == (delay_seconds is None):
            raise ValueError("pass exactly one of `until` or `delay_seconds`")
        if until is not None and until.tzinfo is None:
            raise ValueError("`until` must be timezone-aware")
        self.reason = reason
        self.until = until
        self.delay_seconds = delay_seconds


class JobCancelled(JobError):
    """Raised by a handler that noticed ``cancel_requested`` and stopped cleanly."""


class LeaseLostError(JobError):
    """This worker no longer owns the job (lease expired and another worker took it). Never apply results."""
