"""Contract between the transport (server.py) and the tools (tools/*).

Owned by the scaffold. Neither side changes these shapes without the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.auth.credentials import CredentialMinter
from app.auth.grants import GrantStore
from app.auth.ratelimit import RateLimiter
from app.auth.types import Resolution
from app.config import Settings

# JSON-RPC error codes (HANDOFF §6.2)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020

# Mandatory MCP headers (lowercased; API Gateway preserves case, we normalise)
H_PROTOCOL_VERSION = "mcp-protocol-version"
H_METHOD = "mcp-method"
H_NAME = "mcp-name"


@dataclass
class AuditTrail:
    """What a tool call depended on, for the §12.7 / AS-10 audit line.

    Tools call ``ToolContext.note`` with each grant resolution they relied on; the
    transport reads ``grants_used`` after the handler returns.
    """

    grants_used: list[tuple[str, str]] = field(default_factory=list)

    def note(self, resolution: Resolution) -> None:
        for g in resolution.grants_used:
            pair = (g.node, g.permission.value)
            if pair not in self.grants_used:
                self.grants_used.append(pair)


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler may use. Identity comes from the validated token
    via the authorizer context — never from tool arguments (AS-6).

    Attributes:
        subject: Token ``sub``.
        scopes: Scopes carried by the token (already checked by the transport for
            this tool's required scope; tools do not re-check scope).
        grants: Grant store (read-only role).
        minter: Credential minter — the only way to obtain an S3 client.
        settings: Runtime settings.
        request_id: Gateway request id for logging.
        log: Powertools logger (or any object with ``info``/``warning``/``error``).
        limiter: Per-subject rate limiter (§12.6); a no-op when unconfigured.
        audit: Mutable trail of the grants this call depended on (AS-10).
    """

    subject: str
    scopes: frozenset[str]
    grants: GrantStore
    minter: CredentialMinter
    settings: Settings
    request_id: str = ""
    log: Any = None
    limiter: RateLimiter | None = None
    audit: AuditTrail = field(default_factory=AuditTrail)

    def require(self, path: str, needed: Any) -> Resolution:
        """``grants.require`` that also records the grants used. Tools should call
        this rather than ``ctx.grants.require`` directly."""
        resolution = self.grants.require(self.subject, path, needed)
        self.audit.note(resolution)
        return resolution

    @property
    def actor(self) -> str:
        """OKF actor string for this user (§3.4): ``human:<subject>``."""
        return f"human:{self.subject}"


ToolHandler = Callable[[ToolContext, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Tool:
    """One MCP tool.

    Attributes:
        name: Wire name, e.g. ``read_article``.
        description: Shown to the agent. Written as in HANDOFF §10.
        input_schema: JSON Schema with ``$defs`` **inlined** (§10.1 — clients do not
            resolve cross-document refs).
        output_schema: JSON Schema for ``structuredContent`` on success, or ``None``.
        scope: Required scope (``wiki.read`` / ``wiki.write``) or ``None`` for
            ``resolve_reference``.
        handler: ``(ctx, arguments) -> structured result``. Raises ``ToolError`` on
            any §10.14 outcome; never returns an error shape.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    scope: str | None
    handler: ToolHandler

    def descriptor(self) -> dict[str, Any]:
        """Shape returned by ``tools/list``."""
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.output_schema is not None:
            d["outputSchema"] = self.output_schema
        return d


__all__ = [
    "AuditTrail",
    "HEADER_MISMATCH",
    "H_METHOD",
    "H_NAME",
    "H_PROTOCOL_VERSION",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "Tool",
    "ToolContext",
    "ToolHandler",
]
