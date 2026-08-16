# Typed exceptions mapped from the service's {error, code} responses + HTTP status.

from typing import Any


class ServiceError(Exception):
    """Base for all SDK errors. Carries the service's structured error fields."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class BadRequest(ServiceError):
    """400 — malformed request."""


class AuthError(ServiceError):
    """401 — bad/expired service or session token."""


class NotFound(ServiceError):
    """404 — unknown session id / route."""


class ServiceUnavailable(ServiceError):
    """5xx response, or a network error after retries were exhausted."""


_STATUS_MAP: dict[int, type[ServiceError]] = {
    400: BadRequest,
    401: AuthError,
    404: NotFound,
}


def error_for(status: int, body: dict[str, Any] | None) -> ServiceError | None:
    """Return the typed exception for a >= 400 status, or None for success."""
    if status < 400:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    message = body.get("error") if isinstance(body, dict) else None
    cls = _STATUS_MAP.get(status)
    if cls is None:
        cls = ServiceUnavailable if status >= 500 else ServiceError
    return cls(message or f"HTTP {status}", code=code, status=status)
