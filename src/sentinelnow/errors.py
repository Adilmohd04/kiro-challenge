"""Typed exceptions for SentinelNow. Never raise bare Exception."""


class SentinelNowError(Exception):
    """Base class for all SentinelNow errors."""


class PolicyDenied(SentinelNowError):
    """Raised when a write action is denied by policy."""


class ApprovalRequired(SentinelNowError):
    """Raised when a write action requires human approval before proceeding."""


class KillSwitchEngaged(SentinelNowError):
    """Raised when the kill switch is active and all writes are blocked."""


class InvalidRequest(SentinelNowError):
    """Raised when a request or tool input fails boundary validation.

    Surfaced from the pydantic boundary before any instance I/O so the
    instance is never touched. The message names the invalid field.
    """


class MissingCredential(SentinelNowError):
    """Raised when a required ServiceNow credential env var is missing or empty.

    References the variable by name only (e.g. ``SN_PASSWORD``); never embeds
    the value, even masked or hashed.
    """


class InstanceUnreachable(SentinelNowError):
    """Raised when the real ServiceNow instance cannot be reached.

    No mutation occurs when this is raised.
    """


class InstanceOperationFailed(SentinelNowError):
    """Raised when a read or write fails after a connection is established.

    The pre-operation state of the instance is preserved.
    """
