"""Transport conformance — HANDOFF §6, build step 7.

Every case goes through ``handle`` with a synthetic API Gateway REST proxy event
unless it is explicitly about ``dispatch``.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.mcp import server
from app.mcp.protocol import (
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ToolContext,
)
from tests.unit.mcp.conftest import REQUEST_ID, SUB, call, make_event, rpc, sentinel

TOOL_CALL_HEADERS = {
    "MCP-Protocol-Version": "2026-07-28",
    "Mcp-Method": "tools/call",
    "Mcp-Name": "echo",
}


def _tools_call(name: str, arguments: dict | None = None, **kw):
    params = {"name": name, "arguments": arguments if arguments is not None else {}}
    return make_event(rpc("tools/call", params), **kw)


# --- method / origin / auth guards --------------------------------------------


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS"])
def test_non_post_is_405(method: str) -> None:
    status, headers, body = call(make_event(rpc("ping"), method=method))
    assert status == 405
    assert headers["Allow"] == "POST, OPTIONS"
    assert body == {"error": "method not allowed"}


def test_bad_origin_is_403() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Origin": "https://evil.example"}))
    assert status == 403
    assert body == {"error": "origin not allowed"}


def test_origin_match_is_exact() -> None:
    status, _, _ = call(make_event(rpc("ping"), headers={"Origin": "https://claude.ai.evil"}))
    assert status == 403


def test_good_origin_passes() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Origin": "https://claude.ai"}))
    assert status == 200
    assert body["result"] == {}


def test_origin_via_multi_value_headers_is_checked() -> None:
    ev = make_event(rpc("ping"))
    ev["headers"] = {"Content-Type": "application/json"}
    ev["multiValueHeaders"] = {"Origin": ["https://evil.example"]}
    status, _, _ = call(ev)
    assert status == 403


def test_no_origin_passes() -> None:
    status, _, _ = call(make_event(rpc("ping")))
    assert status == 200


def test_missing_authorizer_context_is_401_with_challenge(settings: Settings) -> None:
    status, headers, body = call(make_event(rpc("ping"), authorizer=None))
    assert status == 401
    challenge = headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{settings.resource_metadata_url}"' in challenge
    assert 'scope="wiki.read"' in challenge
    assert body == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "authorizer",
    [{"scope": "wiki.read"}, {"sub": "", "scope": "wiki.read"}, {"sub": "u"}],
)
def test_incomplete_authorizer_context_is_401(authorizer: dict) -> None:
    status, headers, _ = call(make_event(rpc("ping"), authorizer=authorizer))
    assert status == 401
    assert "resource_metadata=" in headers["WWW-Authenticate"]


def test_empty_scope_is_accepted_as_identity() -> None:
    status, _, body = call(make_event(rpc("ping"), authorizer={"sub": SUB, "scope": ""}))
    assert status == 200
    assert body["result"] == {}


# --- body parsing --------------------------------------------------------------


def test_invalid_json_is_parse_error() -> None:
    status, _, body = call(make_event(raw_body="{not json"))
    assert status == 400
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == PARSE_ERROR


def test_empty_body_is_parse_error() -> None:
    status, _, body = call(make_event(raw_body=""))
    assert status == 400
    assert body["error"]["code"] == PARSE_ERROR


def test_base64_encoded_body_is_decoded() -> None:
    status, _, body = call(make_event(rpc("ping"), base64_body=True))
    assert status == 200
    assert body["result"] == {}


def test_batch_is_invalid_request() -> None:
    status, _, body = call(make_event([rpc("ping"), rpc("ping", id=2)]))
    assert status == 400
    assert body["id"] is None
    assert body["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "ping", "id": 1},
        {"jsonrpc": "1.0", "method": "ping", "id": 1},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "method": 5, "id": 1},
        {"jsonrpc": "2.0", "method": "ping", "params": [1, 2], "id": 1},
        "just a string",
        42,
    ],
)
def test_malformed_envelope_is_invalid_request(payload) -> None:
    status, _, body = call(make_event(payload))
    assert status == 400
    assert body["error"]["code"] == INVALID_REQUEST


# --- header / body agreement (§6.2) -------------------------------------------


def test_mcp_method_header_mismatch_is_400_32020() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Mcp-Method": "tools/list"}))
    assert status == 400
    assert body["id"] == 1
    assert body["error"]["code"] == HEADER_MISMATCH
    assert body["error"]["message"] == "HeaderMismatch"


def test_mcp_name_header_mismatch_on_tools_call() -> None:
    status, _, body = call(_tools_call("echo", headers={"Mcp-Name": "crash"}))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_mcp_name_header_ignored_for_other_methods() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Mcp-Name": "anything"}))
    assert status == 200
    assert body["result"] == {}


def test_unsupported_protocol_version_header_is_mismatch() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"MCP-Protocol-Version": "1999-01-01"}))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


@pytest.mark.parametrize("version", ["2026-07-28", "2025-06-18", "2025-03-26"])
def test_supported_protocol_versions_pass(version: str) -> None:
    status, _, _ = call(make_event(rpc("ping"), headers={"MCP-Protocol-Version": version}))
    assert status == 200


def test_matching_headers_pass() -> None:
    status, _, body = call(_tools_call("echo", {"path": "/x"}, headers=TOOL_CALL_HEADERS))
    assert status == 200
    assert body["result"]["isError"] is False


def test_base64_sentinel_header_decodes_before_comparison() -> None:
    headers = {
        "Mcp-Method": sentinel("tools/call"),
        "Mcp-Name": sentinel("echo"),
        "MCP-Protocol-Version": sentinel("2026-07-28"),
    }
    status, _, body = call(_tools_call("echo", headers=headers))
    assert status == 200
    assert body["result"]["isError"] is False


def test_base64_sentinel_header_that_decodes_wrong_is_mismatch() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Mcp-Method": sentinel("tools/list")}))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_undecodable_sentinel_header_is_mismatch() -> None:
    status, _, body = call(make_event(rpc("ping"), headers={"Mcp-Method": "=?base64?!!!?="}))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_header_mismatch_on_notification_is_still_400() -> None:
    ev = make_event(rpc("notifications/initialized", id=None), headers={"Mcp-Method": "ping"})
    status, _, body = call(ev)
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_strict_mode_missing_headers_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_STRICT_HEADERS", "true")
    server._reset()
    status, _, body = call(make_event(rpc("ping")))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_strict_mode_missing_name_on_tools_call_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_STRICT_HEADERS", "true")
    server._reset()
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call"}
    status, _, body = call(_tools_call("echo", headers=headers))
    assert status == 400
    assert body["error"]["code"] == HEADER_MISMATCH


def test_strict_mode_with_all_headers_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_STRICT_HEADERS", "true")
    server._reset()
    status, _, _ = call(_tools_call("echo", headers=TOOL_CALL_HEADERS))
    assert status == 200
    # mcp-name not required on non-call methods even in strict mode
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "ping"}
    status, _, _ = call(make_event(rpc("ping"), headers=headers))
    assert status == 200


def test_non_strict_missing_headers_pass() -> None:
    status, _, body = call(_tools_call("echo"))
    assert status == 200
    assert body["result"]["isError"] is False


# --- routing -------------------------------------------------------------------


def test_unknown_method_is_404_32601() -> None:
    status, _, body = call(make_event(rpc("resources/list")))
    assert status == 404
    assert body["id"] == 1
    assert body["error"]["code"] == METHOD_NOT_FOUND


def test_notification_is_202_empty() -> None:
    resp = server.handle(make_event(rpc("notifications/initialized", id=None)), None)
    assert resp["statusCode"] == 202
    assert resp["body"] == ""


def test_unknown_notification_is_accepted_and_ignored() -> None:
    resp = server.handle(make_event(rpc("notifications/whatever", id=None)), None)
    assert resp["statusCode"] == 202
    assert resp["body"] == ""


def test_server_discover_shape(settings: Settings) -> None:
    status, headers, body = call(make_event(rpc("server/discover", id="d1")))
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "d1"
    result = body["result"]
    assert result["protocolVersions"][0] == settings.protocol_version
    assert set(result["protocolVersions"]) >= {"2025-06-18", "2025-03-26"}
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "wiki-substrate", "version": "0.1.0"}


def test_initialize_echoes_supported_version() -> None:
    ev = make_event(rpc("initialize", {"protocolVersion": "2025-06-18"}))
    status, _, body = call(ev)
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert body["result"]["serverInfo"]["name"] == "wiki-substrate"


def test_initialize_falls_back_to_our_version(settings: Settings) -> None:
    ev = make_event(rpc("initialize", {"protocolVersion": "2024-11-05"}))
    status, _, body = call(ev)
    assert status == 200
    assert body["result"]["protocolVersion"] == settings.protocol_version
    # and with no params at all
    status, _, body = call(make_event(rpc("initialize")))
    assert body["result"]["protocolVersion"] == settings.protocol_version


def test_ping() -> None:
    status, _, body = call(make_event(rpc("ping", id=7)))
    assert status == 200
    assert body == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_tools_list_descriptors() -> None:
    status, _, body = call(make_event(rpc("tools/list")))
    assert status == 200
    tools = body["result"]["tools"]
    assert [t["name"] for t in tools] == ["echo", "boom_grant", "crash"]
    echo = tools[0]
    assert set(echo) == {"name", "description", "inputSchema", "outputSchema"}
    assert "outputSchema" not in tools[1]


# --- tools/call ----------------------------------------------------------------


def test_tools_call_success_shape() -> None:
    status, _, body = call(_tools_call("echo", {"path": "/racing/x.md", "n": 1}))
    assert status == 200
    result = body["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"path": "/racing/x.md", "n": 1}
    assert result["content"] == [
        {"type": "text", "text": json.dumps({"path": "/racing/x.md", "n": 1})}
    ]


def test_tools_call_arguments_default_to_empty() -> None:
    status, _, body = call(make_event(rpc("tools/call", {"name": "echo"})))
    assert status == 200
    assert body["result"]["structuredContent"] == {}


def test_tool_error_is_is_error_result_with_envelope() -> None:
    status, _, body = call(_tools_call("boom_grant", {"path": "/p"}))
    assert status == 200
    result = body["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["status"] == 403
    assert result["structuredContent"]["code"] == "forbidden"
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == result["structuredContent"]["message"]
    # grant denial must never name a scope (§6.5)
    assert "wiki." not in json.dumps(result)


def test_unknown_tool_is_invalid_params() -> None:
    status, _, body = call(_tools_call("nope"))
    assert status == 200
    assert body["error"]["code"] == INVALID_PARAMS
    assert "result" not in body


@pytest.mark.parametrize(
    "params",
    [{}, {"name": 5}, {"name": "echo", "arguments": [1]}, {"name": "echo", "arguments": "x"}],
)
def test_bad_tools_call_params_is_invalid_params(params: dict) -> None:
    status, _, body = call(make_event(rpc("tools/call", params)))
    assert status == 200
    assert body["error"]["code"] == INVALID_PARAMS


def test_missing_scope_is_http_403_with_challenge(settings: Settings) -> None:
    ev = _tools_call("boom_grant", authorizer={"sub": SUB, "scope": "wiki.read"})
    status, headers, body = call(ev)
    assert status == 403
    challenge = headers["WWW-Authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="wiki.write"' in challenge
    assert settings.resource_metadata_url in challenge
    assert body["error"] == "insufficient_scope"
    assert "jsonrpc" not in body


def test_scope_check_precedes_handler() -> None:
    # crash requires wiki.read; without it the handler must not run at all
    ev = _tools_call("crash", authorizer={"sub": SUB, "scope": ""})
    status, _, _ = call(ev)
    assert status == 403


def test_unexpected_exception_is_500_envelope_and_says_nothing() -> None:
    status, _, body = call(_tools_call("crash", {"path": "/p", "body": "sky is falling"}))
    assert status == 200
    result = body["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {
        "status": 500,
        "code": "internal",
        "message": "The server could not complete the request.",
    }
    raw = json.dumps(body)
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw
    assert "sky is falling" not in raw


# --- dispatch as the testable core --------------------------------------------


def _ctx(settings: Settings, scopes: str = "wiki.read wiki.write") -> ToolContext:
    return ToolContext(
        subject=SUB,
        scopes=frozenset(scopes.split()),
        grants=object(),  # type: ignore[arg-type]
        minter=object(),  # type: ignore[arg-type]
        settings=settings,
        request_id=REQUEST_ID,
    )


def test_dispatch_returns_partial_envelope(settings: Settings) -> None:
    status, out = server.dispatch("ping", {}, {}, _ctx(settings))
    assert (status, out) == (200, {"result": {}})
    status, out = server.dispatch("nope", {}, {}, _ctx(settings))
    assert status == 404
    assert out["error"]["code"] == METHOD_NOT_FOUND


def test_dispatch_tools_call_without_event(settings: Settings) -> None:
    params = {"name": "echo", "arguments": {"path": "/a"}}
    status, out = server.dispatch("tools/call", params, {}, _ctx(settings))
    assert status == 200
    assert out["result"]["structuredContent"] == {"path": "/a"}


def test_build_context_wires_singletons(settings: Settings) -> None:
    ev = make_event(rpc("ping"))
    ctx = server.build_context(ev, settings)
    assert ctx.subject == SUB
    assert ctx.scopes == frozenset({"wiki.read", "wiki.write"})
    assert ctx.request_id == REQUEST_ID
    assert ctx.settings is settings
    assert ctx.grants.table_name == settings.grant_table
    assert ctx.minter.role_arn == settings.storage_role_arn
    assert ctx.minter.cache_seconds == settings.credential_cache_seconds
    # singletons persist across calls (warm invocations)
    ctx2 = server.build_context(ev, settings)
    assert ctx2.grants is ctx.grants
    assert ctx2.minter is ctx.minter


# --- audit log (§12.7) ---------------------------------------------------------


def test_log_line_on_allow(log_lines) -> None:
    ev = _tools_call("echo", {"path": "/racing/x.md", "body": "SECRET BODY"})
    ev["headers"]["Authorization"] = "Bearer SECRET-TOKEN"
    status, _, _ = call(ev)
    assert status == 200
    lines = [ln for ln in log_lines() if ln.get("tool")]
    assert len(lines) == 1
    line = lines[0]
    assert line["request_id"] == REQUEST_ID
    assert line["subject"] == SUB
    assert line["tool"] == "echo"
    assert line["path"] == "/racing/x.md"
    assert line["decision"] == "allow"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int | float)
    assert line["bytes"] > 0
    raw = json.dumps(line)
    assert "SECRET-TOKEN" not in raw
    assert "SECRET BODY" not in raw
    assert "Authorization" not in raw


def test_log_line_on_grant_denial(log_lines) -> None:
    call(_tools_call("boom_grant", {"path": "/p"}))
    line = [ln for ln in log_lines() if ln.get("tool")][0]
    assert line["decision"] == "deny"
    assert line["status"] == 403


def test_log_line_on_scope_denial(log_lines) -> None:
    call(_tools_call("boom_grant", {"from": "/old"}, authorizer={"sub": SUB, "scope": "wiki.read"}))
    line = [ln for ln in log_lines() if ln.get("tool")][0]
    assert line["decision"] == "deny"
    assert line["status"] == 403
    assert line["path"] == "/old"


def test_log_line_on_crash(log_lines) -> None:
    call(_tools_call("crash", {"path": "/p", "body": "sky is falling"}))
    lines = log_lines()
    audit = [ln for ln in lines if ln.get("tool") and "decision" in ln][0]
    assert audit["decision"] == "error"
    assert audit["status"] == 500
    errors = [ln for ln in lines if ln["level"] == "ERROR"]
    assert errors and errors[0]["exc_class"] == "RuntimeError"
    assert "sky is falling" not in json.dumps(lines)


def test_no_log_line_for_non_call_methods(log_lines) -> None:
    call(make_event(rpc("tools/list")))
    call(make_event(rpc("ping")))
    assert [ln for ln in log_lines() if ln.get("tool")] == []
