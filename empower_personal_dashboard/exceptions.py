"""
exceptions.py — Custom exceptions for Empower Personal Dashboard API.
"""

from typing import Any, Dict, List, Optional


class EmpowerError(Exception):
    """Base exception for all Empower Personal Dashboard API errors."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        error_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.error_code = error_code

    def __str__(self) -> str:
        if self.error_code:
            return f"[Error {self.error_code}] {self.message}"
        return self.message


class RequireTwoFactorException(EmpowerError):
    """Raised when 2FA verification (SMS or Email) is required to proceed."""

    def __init__(
        self,
        message: str = "Two-factor authentication required.",
        available_methods: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__(message)
        self.available_methods = available_methods or []


class SessionExpiredError(EmpowerError):
    """Raised when saved session cookies or CSRF tokens have expired."""

    pass


class LoginFailedException(EmpowerError):
    """Raised when authentication credentials or 2FA verification fails."""

    pass


class ReconstructionWindowError(EmpowerError):
    """Raised when a transaction window cannot reconstruct a holdings snapshot.

    The Beancount growth-curve reconstruction derives each opening lot from
    ``snapshot - net_buys`` over the fetched transaction range. When that range
    ends before ``holdings.as_of_date``, trades after the range are omitted and
    the baseline silently absorbs them. The fetch-layer guard raises this under
    its ``"error"`` policy so the mismatch is surfaced instead of masked.
    """

    pass


class LedgerAppendError(EmpowerError):
    """Raised when appending to a ledger would broaden its window into the past.

    A prior export persists the earliest reconstructed window boundary. An append
    whose opening lot predates that boundary cannot revise the already-written
    opening lot, so the historical inventory would be wrong. The append is
    rejected rather than silently leaving the stale opening lot in place.
    """

    pass
