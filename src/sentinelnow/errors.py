"""Typed exceptions for SentinelNow. Never raise bare Exception."""


class SentinelNowError(Exception):
    """Base class for all SentinelNow errors."""


class PolicyDenied(SentinelNowError):
    """Raised when a write action is denied by policy."""


class ApprovalRequired(SentinelNowError):
    """Raised when a write action requires human approval before proceeding."""


class KillSwitchEngaged(SentinelNowError):
    """Raised when the kill switch is active and all writes are blocked."""
