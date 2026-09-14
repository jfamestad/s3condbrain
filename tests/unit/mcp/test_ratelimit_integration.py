"""Increment D — the rate limiter at the transport boundary (HANDOFF §12.6, §10.14).

Two layers. With a stubbed limiter: the 429 envelope, the audit line, and the
ordering (scope check → limiter → handler). With the real ``RateLimiter`` over a
moto table: ``_get_limiter`` wires the settings through and the third call of a
two-per-minute subject comes back 429.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
import pytest

from app.errors import ToolError
from app.mcp import server
from tests.unit.mcp.conftest import REQUEST_ID, SUB, call, make_event, rpc

RETRY_AFTER = 17


class StubLimiter:
    """Records every check; raises 429 when told to."""

    instances: list[StubLimiter] = []

    def __init__(self, table_name: str, calls_per_minute: int, writes_per_hour: int) -> None:
        self.table_name = table_name
        self.calls_per_minute = calls_per_minute
        self.writes_per_hour = writes_per_hour
        self.checks: list[tuple[str, bool]] = []
        self.reject = False
        StubLimiter.instances.append(self)

    def check(self, subject: str, *, is_write: bool) -> None:
        self.checks.append((subject, is_write))
        if self.reject:
            raise ToolError(
                429, "rate_limited", "Too many calls; slow down.", retry_after=RETRY_AFTER
            )


@pytest.fixture
def limiter(monkeypatch: pytest.MonkeyPatch) -> StubLimiter:
    StubLimiter.instances.clear()
    monkeypatch.setattr(server, "RateLimiter", StubLimiter)
    # force construction now so the test can flip ``reject`` before the call
    settings = server._get_settings()
    return server._get_limiter(settings)  # type: ignore[return-value]


def _tools_call(name: str, arguments: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    params = {"name": name, "arguments": arguments if arguments is not None else {}}
    return make_event(rpc("tools/call", params), **kw)


# --- stubbed limiter ------------------------------------------------------------------


def test_rejection_is_a_429_tool_error_envelope(limiter: StubLimiter) -> None:
    limiter.reject = True
    status, headers, body = call(_tools_call("echo", {"path": "/racing/x.md"}))
    assert status == 200  # JSON-RPC succeeded; the *tool* result is the error
    assert headers["Content-Type"] == "application/json"
    result = body["result"]
    assert result["isError"] is True
    assert result["structuredContent"] == {
        "status": 429,
        "code": "rate_limited",
        "message": "Too many calls; slow down.",
        "retry_after": RETRY_AFTER,
    }
    assert result["content"] == [{"type": "text", "text": "Too many calls; slow down."}]


def test_rejection_logs_the_audit_line(limiter: StubLimiter, log_lines) -> None:
    limiter.reject = True
    call(_tools_call("echo", {"path": "/racing/x.md"}))
    lines = [ln for ln in log_lines() if ln.get("tool")]
    assert len(lines) == 1
    line = lines[0]
    assert line["request_id"] == REQUEST_ID
    assert line["subject"] == SUB
    assert line["tool"] == "echo"
    assert line["path"] == "/racing/x.md"
    assert line["status"] == 429
    assert line["decision"] == "error"  # §12.7: not a grant denial, not an allow
    assert line["grants_used"] == []  # the handler never ran


def test_limiter_runs_before_the_handler(limiter: StubLimiter) -> None:
    limiter.reject = True
    _, _, body = call(_tools_call("echo", {"path": "/p", "marker": "handler-ran"}))
    assert "marker" not in body["result"]["structuredContent"]
    assert body["result"]["isError"] is True


def test_limiter_sees_subject_and_write_flag(limiter: StubLimiter) -> None:
    call(_tools_call("echo", {"path": "/p"}))  # wiki.read
    call(_tools_call("boom_grant", {"path": "/p"}))  # wiki.write
    assert limiter.checks == [(SUB, False), (SUB, True)]


def test_scope_check_precedes_the_limiter(limiter: StubLimiter) -> None:
    status, _, _ = call(
        _tools_call("boom_grant", {"path": "/p"}, authorizer={"sub": SUB, "scope": "wiki.read"})
    )
    assert status == 403
    assert limiter.checks == []


def test_unknown_tool_never_reaches_the_limiter(limiter: StubLimiter) -> None:
    call(_tools_call("no_such_tool"))
    assert limiter.checks == []


def test_non_call_methods_never_reach_the_limiter(limiter: StubLimiter) -> None:
    call(make_event(rpc("tools/list")))
    call(make_event(rpc("ping")))
    call(make_event(rpc("initialize", {"protocolVersion": "2026-07-28"})))
    assert limiter.checks == []


def test_allowed_call_proceeds_normally(limiter: StubLimiter) -> None:
    _, _, body = call(_tools_call("echo", {"path": "/p"}))
    assert body["result"]["isError"] is False
    assert body["result"]["structuredContent"] == {"path": "/p"}


def test_limiter_is_a_warm_singleton_built_from_settings(limiter: StubLimiter) -> None:
    settings = server._get_settings()
    assert limiter.table_name == settings.ratelimit_table
    assert limiter.calls_per_minute == settings.calls_per_minute
    assert limiter.writes_per_hour == settings.writes_per_hour
    ctx = server.build_context(make_event(rpc("ping")), settings)
    assert ctx.limiter is limiter
    assert len(StubLimiter.instances) == 1


# --- real limiter over moto ------------------------------------------------------------


@pytest.fixture
def real_table(aws: None, monkeypatch: pytest.MonkeyPatch) -> Any:
    name = "wiki-ratelimit-integration"
    monkeypatch.setenv("RATELIMIT_TABLE", name)
    monkeypatch.setenv("CALLS_PER_MINUTE", "2")
    monkeypatch.setenv("WRITES_PER_HOUR", "1")
    server._reset()  # settings were possibly cached before the env changed
    ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    table = ddb.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
    table.wait_until_exists()
    return table


def test_real_limiter_end_to_end(real_table: Any, log_lines) -> None:
    ok1 = call(_tools_call("echo", {"path": "/p"}))[2]["result"]
    ok2 = call(_tools_call("echo", {"path": "/p"}))[2]["result"]
    rejected = call(_tools_call("echo", {"path": "/p"}))[2]["result"]
    assert ok1["isError"] is False and ok2["isError"] is False
    assert rejected["isError"] is True
    envelope = rejected["structuredContent"]
    assert envelope["status"] == 429
    assert envelope["code"] == "rate_limited"
    assert 0 < envelope["retry_after"] <= 60

    rows = real_table.scan()["Items"]
    assert len(rows) == 1
    assert rows[0]["pk"] == f"RL#{SUB}"
    assert rows[0]["sk"].startswith("calls#")
    assert int(rows[0]["count"]) == 3

    statuses = [ln["status"] for ln in log_lines() if ln.get("tool")]
    assert statuses == [200, 200, 429]


def test_real_limiter_write_budget(real_table: Any) -> None:
    # writes_per_hour=1, calls_per_minute=2: first write ok, second write is
    # refused by the write window (calls window would still allow it).
    first = call(_tools_call("boom_grant", {"path": "/p"}))[2]["result"]
    second = call(_tools_call("boom_grant", {"path": "/p"}))[2]["result"]
    assert first["structuredContent"]["status"] == 403  # the handler ran and denied
    assert second["structuredContent"]["status"] == 429
    assert second["structuredContent"]["message"] == "Too many writes this hour; slow down."
    kinds = sorted(r["sk"].split("#")[0] for r in real_table.scan()["Items"])
    assert kinds == ["calls", "writes"]
