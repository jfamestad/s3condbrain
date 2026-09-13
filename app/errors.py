"""Error types shared by transport, tools and storage.

Two levels, deliberately (HANDOFF §6.5, §10.14):

* ``HttpError`` — emitted by the transport *before* any tool runs. Authentication
  is the gateway's (401); scope failure is ours (403 + ``insufficient_scope``).
* ``ToolError`` — an MCP tool error (``isError: true``) with ``structuredContent``
  matching the §10.14 envelope. Grant denials, conflicts, not-found, bad input.
"""

from __future__ import annotations

from typing import Any


class HttpError(Exception):
    """A transport-level HTTP response that bypasses JSON-RPC.

    Args:
        status: HTTP status code.
        body: JSON-serialisable body, or a string.
        headers: Extra response headers.
    """

    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body if body is not None else {"error": "request refused"}
        self.headers = headers or {}


class InsufficientScope(HttpError):
    """HTTP 403 with the RFC 6750 ``insufficient_scope`` challenge (§6.5 row 2)."""

    def __init__(self, required_scope: str, resource_metadata_url: str) -> None:
        challenge = (
            f'Bearer error="insufficient_scope", scope="{required_scope}", '
            f'resource_metadata="{resource_metadata_url}"'
        )
        super().__init__(
            403,
            {"error": "insufficient_scope", "required_scope": required_scope},
            {"WWW-Authenticate": challenge},
        )
        self.required_scope = required_scope


class ToolError(Exception):
    """An error returned inside a tool result (§10.14).

    Args:
        status: One of 400, 403, 404, 409, 429, 500.
        code: One of the §10.14 codes.
        message: Written for a person; says what to do next where there is something to do.
        **extra: Optional envelope fields — ``current_version``, ``current_body``, ``retry_after``.
    """

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def structured(self) -> dict[str, Any]:
        """Envelope shape for ``structuredContent``."""
        return {"status": self.status, "code": self.code, "message": self.message, **self.extra}


# Convenience constructors — keep call sites short and the codes consistent.


def bad_request(message: str) -> ToolError:
    return ToolError(400, "bad_request", message)


def forbidden(message: str = "You do not have access to this path. Ask an owner.") -> ToolError:
    return ToolError(403, "forbidden", message)


def not_found(message: str = "No such path.") -> ToolError:
    return ToolError(404, "not_found", message)


def conflict(message: str, **extra: Any) -> ToolError:
    return ToolError(409, "conflict", message, **extra)


def exists(message: str = "Something already occupies this path.") -> ToolError:
    return ToolError(409, "exists", message)


def archived(message: str, current_version: str) -> ToolError:
    return ToolError(409, "archived", message, current_version=current_version)


def retired_pointer(message: str = "This path was vacated by a move and is retired.") -> ToolError:
    return ToolError(409, "retired_pointer", message)


def internal(message: str = "The server could not complete the request.") -> ToolError:
    return ToolError(500, "internal", message)
