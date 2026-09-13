"""MCP transport over API Gateway REST proxy — HANDOFF §6, §9, build step 7.

Responsibilities, in order, per POST:

1. Method/route guards (GET/DELETE are mocked at the gateway; defend anyway → 405).
2. Origin validation: present-and-not-allowed → 403 (§6.2). Absent is fine.
3. Parse JSON-RPC body (single request; batches → -32600).
4. Mandatory-header agreement: a present ``Mcp-Method``/``Mcp-Name``/
   ``MCP-Protocol-Version`` that disagrees with the body → 400, ``-32020``.
   Absent headers are tolerated unless ``settings.strict_mcp_headers``.
   Values may arrive base64-sentinel encoded (``=?base64?…?=``) — decode first.
5. Dispatch: ``server/discover``, ``initialize`` (compat), ``notifications/initialized``
   (202, empty), ``ping``, ``tools/list``, ``tools/call``. Unknown → 404, ``-32601``.
6. For ``tools/call``: required scope check → ``InsufficientScope`` (HTTP 403).
   Then the handler. ``ToolError`` → result with ``isError: true`` and
   ``structuredContent`` = envelope. Any other exception → ``isError`` 500 envelope,
   logged with the request id, body says nothing more (§12.3).

Identity: ``event["requestContext"]["authorizer"]["sub"]`` and ``["scope"]`` (space
separated), placed there by the authorizer. Missing → 401 challenge (defensive; the
gateway should never route an unauthenticated POST here).

One structured log line per tool call (§12.7): request_id · subject · tool · path ·
decision · grants_used · duration_ms · bytes.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.mcp.protocol import ToolContext


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for API Gateway REST proxy integration."""
    raise NotImplementedError


def dispatch(
    method: str, params: dict[str, Any], headers: dict[str, str], ctx: ToolContext
) -> tuple[int, dict[str, Any]]:
    """Route one JSON-RPC method to its result. Returns ``(http_status, jsonrpc_result)``.

    Exposed for unit tests so the protocol can be exercised without an event envelope.
    """
    raise NotImplementedError


def build_context(event: dict[str, Any], settings: Settings) -> ToolContext:
    """Construct a ``ToolContext`` from the authorizer context and module-level
    singletons (GrantStore, CredentialMinter) that persist across warm invocations."""
    raise NotImplementedError


__all__ = ["build_context", "dispatch", "handle"]
