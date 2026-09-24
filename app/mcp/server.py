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
6. For ``tools/call``: the handler runs directly — custom scopes are not an
   authorization input (ADR-0016; the grant layer alone decides, §3.3). ``ToolError``
   → result with ``isError: true`` and ``structuredContent`` = envelope. Any other
   exception → ``isError`` 500 envelope, logged with the request id, body says
   nothing more (§12.3).

Identity: ``event["requestContext"]["authorizer"]["sub"]`` and ``["scope"]`` (space
separated), placed there by the authorizer. Missing ``sub`` → 401 challenge
(defensive; the gateway should never route an unauthenticated POST here). A missing
or empty ``scope`` is not an error — it is an empty set (ADR-0016).

One structured log line per tool call (§12.7): request_id · subject · tool · path ·
decision · status · grants_used · duration_ms · bytes.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
import traceback
from typing import Any

from aws_lambda_powertools import Logger

from app.auth.credentials import CredentialMinter
from app.auth.grants import GrantStore
from app.auth.ratelimit import RateLimiter
from app.config import SCOPE_WRITE, Settings
from app.errors import HttpError, ToolError
from app.mcp import tools as tools_pkg
from app.mcp.protocol import (
    H_METHOD,
    H_NAME,
    H_PROTOCOL_VERSION,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    Tool,
    ToolContext,
)

logger = Logger(service="wiki-mcp")

SERVER_INFO = {"name": "wiki-substrate", "version": "0.1.0"}
CAPABILITIES = {"tools": {"listChanged": False}}
COMPAT_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
JSON_HEADERS = {"Content-Type": "application/json"}
_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="
_INTERNAL_ENVELOPE = {
    "status": 500,
    "code": "internal",
    "message": "The server could not complete the request.",
}

# Warm-invocation singletons. ``_reset()`` clears them for tests.
_settings: Settings | None = None
_registry: dict[str, Tool] | None = None
_grants: GrantStore | None = None
_minter: CredentialMinter | None = None
_limiter: RateLimiter | None = None


def _reset() -> None:
    """Drop cached singletons (tests only)."""
    global _settings, _registry, _grants, _minter, _limiter
    _settings = None
    _registry = None
    _grants = None
    _minter = None
    _limiter = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def _get_registry() -> dict[str, Tool]:
    global _registry
    if _registry is None:
        _registry = tools_pkg.registry()
    return _registry


def _get_grants(settings: Settings) -> GrantStore:
    global _grants
    if _grants is None:
        _grants = GrantStore(settings.grant_table)
    return _grants


def _get_limiter(settings: Settings) -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(
            settings.ratelimit_table,
            settings.calls_per_minute,
            settings.writes_per_hour,
        )
    return _limiter


def _get_minter(settings: Settings) -> CredentialMinter:
    global _minter
    if _minter is None:
        _minter = CredentialMinter(
            settings.storage_role_arn,
            settings.bucket,
            settings.kms_key_arn,
            settings.credential_cache_seconds,
        )
    return _minter


class _RpcError(Exception):
    """A JSON-RPC error response with its HTTP status. Internal to this module."""

    def __init__(
        self, status: int, code: int, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data

    def partial(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return {"error": err}


# --- responses ----------------------------------------------------------------


def _response(status: int, body: Any, headers: dict[str, str] | None = None) -> dict[str, Any]:
    text = body if isinstance(body, str) else json.dumps(body)
    return {"statusCode": status, "headers": {**JSON_HEADERS, **(headers or {})}, "body": text}


def _rpc_response(status: int, rpc_id: Any, partial: dict[str, Any]) -> dict[str, Any]:
    return _response(status, {"jsonrpc": "2.0", "id": rpc_id, **partial})


# --- request preprocessing ----------------------------------------------------


def _normalise_headers(event: dict[str, Any]) -> dict[str, str]:
    """Lowercase keys; merge ``headers`` and ``multiValueHeaders``; first value wins."""
    out: dict[str, str] = {}
    for key, value in (event.get("headers") or {}).items():
        if value is not None:
            out.setdefault(key.lower(), str(value))
    for key, values in (event.get("multiValueHeaders") or {}).items():
        if values:
            out.setdefault(key.lower(), str(values[0]))
    return out


def _check_origin(headers: dict[str, str], settings: Settings) -> None:
    origin = headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        raise HttpError(403, {"error": "origin not allowed"})


def _parse_body(event: dict[str, Any]) -> Any:
    raw = event.get("body")
    if raw is None:
        raw = ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise _RpcError(400, PARSE_ERROR, "Parse error") from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _RpcError(400, PARSE_ERROR, "Parse error") from exc


def _validate_envelope(payload: Any) -> tuple[str, dict[str, Any], bool, Any]:
    """Return ``(method, params, is_notification, id)`` or raise ``-32600``."""
    if isinstance(payload, list):
        raise _RpcError(400, INVALID_REQUEST, "Batch requests are not supported")
    if not isinstance(payload, dict):
        raise _RpcError(400, INVALID_REQUEST, "Invalid Request")
    if payload.get("jsonrpc") != "2.0":
        raise _RpcError(400, INVALID_REQUEST, "Invalid Request")
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise _RpcError(400, INVALID_REQUEST, "Invalid Request")
    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _RpcError(400, INVALID_REQUEST, "Invalid Request")
    return method, params, "id" not in payload, payload.get("id")


def _decode_sentinel(value: str) -> str:
    """Decode ``=?base64?<b64>?=``; return other values unchanged."""
    if not (value.startswith(_SENTINEL_PREFIX) and value.endswith(_SENTINEL_SUFFIX)):
        return value
    inner = value[len(_SENTINEL_PREFIX) : -len(_SENTINEL_SUFFIX)]
    try:
        return base64.b64decode(inner, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _mismatch("undecodable") from exc


def _mismatch(header: str) -> _RpcError:
    return _RpcError(400, HEADER_MISMATCH, "HeaderMismatch", {"header": header})


def _supported_versions(settings: Settings) -> list[str]:
    versions = [settings.protocol_version, *COMPAT_PROTOCOL_VERSIONS]
    return list(dict.fromkeys(versions))


def _check_headers(
    method: str, params: dict[str, Any], headers: dict[str, str], settings: Settings
) -> None:
    """§6.2 header/body agreement. Present-and-wrong always fails; absent fails
    only under ``strict_mcp_headers``."""
    strict = settings.strict_mcp_headers
    is_call = method == "tools/call"

    version = headers.get(H_PROTOCOL_VERSION)
    if version is None:
        if strict:
            raise _mismatch(H_PROTOCOL_VERSION)
    elif _decode_sentinel(version) not in _supported_versions(settings):
        raise _mismatch(H_PROTOCOL_VERSION)

    hdr_method = headers.get(H_METHOD)
    if hdr_method is None:
        if strict:
            raise _mismatch(H_METHOD)
    elif _decode_sentinel(hdr_method) != method:
        raise _mismatch(H_METHOD)

    if not is_call:
        return
    hdr_name = headers.get(H_NAME)
    if hdr_name is None:
        if strict:
            raise _mismatch(H_NAME)
    elif _decode_sentinel(hdr_name) != params.get("name"):
        raise _mismatch(H_NAME)


# --- dispatch -----------------------------------------------------------------


def _discover(settings: Settings) -> dict[str, Any]:
    return {
        "protocolVersions": _supported_versions(settings),
        "capabilities": CAPABILITIES,
        "serverInfo": SERVER_INFO,
    }


def _initialize(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    version = (
        requested
        if isinstance(requested, str) and requested in _supported_versions(settings)
        else settings.protocol_version
    )
    return {"protocolVersion": version, "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO}


def _tools_list() -> dict[str, Any]:
    return {"tools": [t.descriptor() for t in _get_registry().values()]}


def _tools_call(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise _RpcError(200, INVALID_PARAMS, "Invalid params: name must be a string")
    arguments = params.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise _RpcError(200, INVALID_PARAMS, "Invalid params: arguments must be an object")
    tool = _get_registry().get(name)
    if tool is None:
        raise _RpcError(200, INVALID_PARAMS, "Invalid params: unknown tool", {"name": name})

    # No scope gate here (ADR-0016): the grant layer is the only authority (§3.3).
    # ``tool.scope`` still classifies read vs write for the rate limiter below.
    try:
        if ctx.limiter is not None:
            ctx.limiter.check(ctx.subject, is_write=tool.scope == SCOPE_WRITE)
        result = tool.handler(ctx, arguments)
    except ToolError as err:
        return {
            "content": [{"type": "text", "text": err.message}],
            "structuredContent": err.structured(),
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 — the boundary; body says nothing (§12.3)
        logger.error(
            "tool_crash",
            request_id=ctx.request_id,
            tool=name,
            exc_class=type(exc).__name__,
            stack="".join(traceback.format_tb(exc.__traceback__)),
        )
        return {
            "content": [{"type": "text", "text": _INTERNAL_ENVELOPE["message"]}],
            "structuredContent": dict(_INTERNAL_ENVELOPE),
            "isError": True,
        }
    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "structuredContent": result,
        "isError": False,
    }


def dispatch(
    method: str, params: dict[str, Any], headers: dict[str, str], ctx: ToolContext
) -> tuple[int, dict[str, Any]]:
    """Route one JSON-RPC method to its result.

    Returns ``(http_status, partial)`` where ``partial`` is ``{"result": ...}`` or
    ``{"error": {...}}`` — the envelope minus ``jsonrpc`` and ``id``. Header/body
    agreement (§6.2) is checked here first, against the normalised ``headers``.
    ``HttpError`` (scope refusal) propagates; the caller renders it.

    Exposed for unit tests so the protocol can be exercised without an event envelope.
    """
    settings = ctx.settings
    try:
        _check_headers(method, params, headers, settings)
        if method == "server/discover":
            return 200, {"result": _discover(settings)}
        if method == "initialize":
            return 200, {"result": _initialize(params, settings)}
        if method == "ping":
            return 200, {"result": {}}
        if method == "tools/list":
            return 200, {"result": _tools_list()}
        if method == "tools/call":
            return 200, {"result": _tools_call(params, ctx)}
        raise _RpcError(404, METHOD_NOT_FOUND, "Method not found")
    except _RpcError as err:
        return err.status, err.partial()


# --- context ------------------------------------------------------------------


def _request_id(event: dict[str, Any], context: Any = None) -> str:
    rid = (event.get("requestContext") or {}).get("requestId")
    if rid:
        return str(rid)
    return str(getattr(context, "aws_request_id", "") or "")


def build_context(event: dict[str, Any], settings: Settings) -> ToolContext:
    """Construct a ``ToolContext`` from the authorizer context and module-level
    singletons (GrantStore, CredentialMinter) that persist across warm invocations.

    A missing or non-string ``scope`` is not a rejection — it is an empty set of
    scopes (ADR-0016: custom scopes are no longer an authorization input, so there
    is nothing to require). ``sub`` is unaffected and stays strict.

    Raises:
        HttpError: 401 with the discovery challenge when ``sub`` is absent or empty.
    """
    auth = (event.get("requestContext") or {}).get("authorizer") or {}
    sub = auth.get("sub")
    scope = auth.get("scope")
    if not isinstance(sub, str) or not sub:
        # No `scope=`: advertising a scope the authorization server cannot grant sends
        # the client back for `invalid_scope` (ADR-0016). Matches the gateway challenge.
        challenge = f'Bearer resource_metadata="{settings.resource_metadata_url}"'
        raise HttpError(401, {"error": "unauthorized"}, {"WWW-Authenticate": challenge})
    if not isinstance(scope, str):
        scope = ""
    return ToolContext(
        subject=sub,
        scopes=frozenset(scope.split()),
        grants=_get_grants(settings),
        minter=_get_minter(settings),
        settings=settings,
        request_id=_request_id(event),
        log=logger,
        limiter=_get_limiter(settings),
    )


# --- audit log (§12.7) --------------------------------------------------------


def _outcome(partial: dict[str, Any] | None, http_error: HttpError | None) -> tuple[str, int]:
    """Derive ``(decision, status)`` for the audit line from a tools/call outcome."""
    if http_error is not None:
        return ("deny" if http_error.status == 403 else "error", http_error.status)
    if partial is None or "error" in partial:
        return "error", 400
    result = partial["result"]
    if not result.get("isError"):
        return "allow", 200
    status = int((result.get("structuredContent") or {}).get("status", 500))
    return ("deny" if status == 403 else "error", status)


def _log_call(
    ctx: ToolContext,
    params: dict[str, Any],
    started: float,
    body_len: int,
    partial: dict[str, Any] | None,
    http_error: HttpError | None,
) -> None:
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    path = arguments.get("path") or arguments.get("from") or ""
    decision, status = _outcome(partial, http_error)
    logger.info(
        "tool_call",
        request_id=ctx.request_id,
        subject=ctx.subject,
        tool=params.get("name") if isinstance(params.get("name"), str) else "",
        path=path if isinstance(path, str) else "",
        decision=decision,
        status=status,
        grants_used=[f"{node}:{perm}" for node, perm in ctx.audit.grants_used],
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        bytes=body_len,
    )


# --- entry point --------------------------------------------------------------


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for API Gateway REST proxy integration."""
    settings = _get_settings()
    try:
        return _handle(event, context, settings)
    except HttpError as err:
        return _response(err.status, err.body, err.headers)


def _handle(event: dict[str, Any], context: Any, settings: Settings) -> dict[str, Any]:
    if event.get("httpMethod") != "POST":
        raise HttpError(405, {"error": "method not allowed"}, {"Allow": "POST, OPTIONS"})
    headers = _normalise_headers(event)
    _check_origin(headers, settings)
    ctx = build_context(event, settings)

    try:
        method, params, is_notification, rpc_id = _validate_envelope(_parse_body(event))
    except _RpcError as err:
        return _rpc_response(err.status, None, err.partial())

    if is_notification:
        try:
            _check_headers(method, params, headers, settings)
        except _RpcError as err:
            return _rpc_response(err.status, None, err.partial())
        return {"statusCode": 202, "headers": {}, "body": ""}

    is_call = method == "tools/call"
    started = time.perf_counter()
    try:
        status, partial = dispatch(method, params, headers, ctx)
    except HttpError as err:
        if is_call:
            body_len = len(json.dumps(err.body)) if not isinstance(err.body, str) else len(err.body)
            _log_call(ctx, params, started, body_len, None, err)
        raise
    response = _rpc_response(status, rpc_id, partial)
    if is_call:
        _log_call(ctx, params, started, len(response["body"]), partial, None)
    return response


__all__ = ["build_context", "dispatch", "handle"]
