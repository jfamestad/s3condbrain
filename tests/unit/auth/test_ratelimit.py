"""Increment D — per-subject rate limits (HANDOFF §12.6).

The rate-limit table is separate from the grant table (§4.7: the MCP role may
write counters but not grants), so it gets its own fixture here — pk S / sk S,
TTL on ``ttl``. This is the layout the infra stack must build.
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError, EndpointConnectionError

from app.auth import ratelimit as rl
from app.auth.ratelimit import RateLimiter
from app.errors import ToolError

TABLE = "wiki-ratelimit-test"
SUB = "user_01ABC"
T0 = 1_757_721_600.0  # a minute and an hour boundary (divisible by 3600)


@pytest.fixture
def ratelimit_table(aws: None) -> Any:
    ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    table = ddb.create_table(
        TableName=TABLE,
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
    ddb.meta.client.update_time_to_live(
        TableName=TABLE, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}
    )
    return table


@pytest.fixture
def ddb(ratelimit_table: Any) -> Any:
    return boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(ddb: Any, clock: Clock) -> RateLimiter:
    return RateLimiter(TABLE, 60, 200, dynamodb_resource=ddb, clock=clock)


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    buf = io.StringIO()
    logger = Logger(
        service=f"wiki-ratelimit-test-{uuid.uuid4().hex}",
        logger_handler=logging.StreamHandler(buf),
    )
    monkeypatch.setattr(rl, "logger", logger)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    yield read


def _count(table: Any, subject: str, kind: str, start: int) -> int | None:
    item = table.get_item(Key={"pk": f"RL#{subject}", "sk": f"{kind}#{start}"}).get("Item")
    return int(item["count"]) if item else None


def _raises_429(fn: Callable[[], None]) -> ToolError:
    with pytest.raises(ToolError) as exc:
        fn()
    assert exc.value.status == 429
    assert exc.value.code == "rate_limited"
    return exc.value


# --- calls window --------------------------------------------------------------------


def test_sixty_calls_pass_and_the_sixty_first_is_429(
    limiter: RateLimiter, ratelimit_table: Any
) -> None:
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    err = _raises_429(lambda: limiter.check(SUB, is_write=False))
    assert 0 < err.extra["retry_after"] <= 60
    assert isinstance(err.extra["retry_after"], int)
    assert err.structured() == {
        "status": 429,
        "code": "rate_limited",
        "message": "Too many calls; slow down.",
        "retry_after": err.extra["retry_after"],
    }
    assert _count(ratelimit_table, SUB, "calls", int(T0)) == 61


def test_retry_after_is_seconds_to_window_end(limiter: RateLimiter, clock: Clock) -> None:
    clock.now = T0 + 12.4
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    err = _raises_429(lambda: limiter.check(SUB, is_write=False))
    assert err.extra["retry_after"] == 48  # ceil(60 - 12.4)


def test_retry_after_never_zero(limiter: RateLimiter, clock: Clock) -> None:
    clock.now = T0 + 59.999
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    err = _raises_429(lambda: limiter.check(SUB, is_write=False))
    assert err.extra["retry_after"] == 1


def test_window_rolls_over(limiter: RateLimiter, clock: Clock, ratelimit_table: Any) -> None:
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    _raises_429(lambda: limiter.check(SUB, is_write=False))
    clock.now = T0 + 60
    limiter.check(SUB, is_write=False)  # fresh window
    assert _count(ratelimit_table, SUB, "calls", int(T0) + 60) == 1
    assert _count(ratelimit_table, SUB, "calls", int(T0)) == 61


def test_windows_are_aligned_to_the_minute(limiter: RateLimiter, clock: Clock) -> None:
    clock.now = T0 + 30
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    clock.now = T0 + 59
    _raises_429(lambda: limiter.check(SUB, is_write=False))
    clock.now = T0 + 60
    limiter.check(SUB, is_write=False)


def test_subjects_are_independent(limiter: RateLimiter) -> None:
    for _ in range(60):
        limiter.check(SUB, is_write=False)
    _raises_429(lambda: limiter.check(SUB, is_write=False))
    limiter.check("user_other", is_write=False)


def test_ceilings_are_configurable(ddb: Any, clock: Clock) -> None:
    lim = RateLimiter(
        TABLE, calls_per_minute=2, writes_per_hour=1, dynamodb_resource=ddb, clock=clock
    )
    lim.check(SUB, is_write=False)
    lim.check(SUB, is_write=False)
    _raises_429(lambda: lim.check(SUB, is_write=False))


# --- writes window ----------------------------------------------------------------------


def test_write_window_is_independent(ddb: Any, clock: Clock, ratelimit_table: Any) -> None:
    lim = RateLimiter(
        TABLE, calls_per_minute=1000, writes_per_hour=2, dynamodb_resource=ddb, clock=clock
    )
    lim.check(SUB, is_write=True)
    lim.check(SUB, is_write=True)
    err = _raises_429(lambda: lim.check(SUB, is_write=True))
    assert err.message == "Too many writes this hour; slow down."
    assert 0 < err.extra["retry_after"] <= 3600
    # reads keep flowing after the write budget is spent
    lim.check(SUB, is_write=False)
    assert _count(ratelimit_table, SUB, "writes", int(T0)) == 3
    assert _count(ratelimit_table, SUB, "calls", int(T0)) == 4


def test_write_window_is_an_hour(ddb: Any, clock: Clock) -> None:
    lim = RateLimiter(
        TABLE, calls_per_minute=1000, writes_per_hour=1, dynamodb_resource=ddb, clock=clock
    )
    lim.check(SUB, is_write=True)
    clock.now = T0 + 1800
    err = _raises_429(lambda: lim.check(SUB, is_write=True))
    assert err.extra["retry_after"] == 1800
    clock.now = T0 + 3600
    lim.check(SUB, is_write=True)


def test_reads_do_not_touch_the_write_window(limiter: RateLimiter, ratelimit_table: Any) -> None:
    limiter.check(SUB, is_write=False)
    assert _count(ratelimit_table, SUB, "writes", int(T0)) is None


def test_rejected_call_does_not_consume_write_budget(
    ddb: Any, clock: Clock, ratelimit_table: Any
) -> None:
    lim = RateLimiter(
        TABLE, calls_per_minute=1, writes_per_hour=5, dynamodb_resource=ddb, clock=clock
    )
    lim.check(SUB, is_write=True)
    _raises_429(lambda: lim.check(SUB, is_write=True))
    assert _count(ratelimit_table, SUB, "calls", int(T0)) == 2
    assert _count(ratelimit_table, SUB, "writes", int(T0)) == 1


# --- row layout ---------------------------------------------------------------------------


def test_row_layout_and_ttl(limiter: RateLimiter, ratelimit_table: Any, clock: Clock) -> None:
    clock.now = T0 + 7
    limiter.check(SUB, is_write=True)
    calls = ratelimit_table.get_item(Key={"pk": f"RL#{SUB}", "sk": f"calls#{int(T0)}"})["Item"]
    writes = ratelimit_table.get_item(Key={"pk": f"RL#{SUB}", "sk": f"writes#{int(T0)}"})["Item"]
    assert int(calls["count"]) == 1
    assert int(calls["ttl"]) == int(T0) + 60 + 60
    assert int(writes["count"]) == 1
    assert int(writes["ttl"]) == int(T0) + 3600 + 60
    # ttl is set once and never moved by later increments
    clock.now = T0 + 30
    limiter.check(SUB, is_write=False)
    again = ratelimit_table.get_item(Key={"pk": f"RL#{SUB}", "sk": f"calls#{int(T0)}"})["Item"]
    assert int(again["count"]) == 2
    assert int(again["ttl"]) == int(T0) + 120


# --- disabled / failure modes ---------------------------------------------------------------


class _Exploding:
    """A DynamoDB resource stand-in that fails on first contact."""

    def Table(self, name: str) -> Any:  # noqa: N802 — boto3's spelling
        raise AssertionError("disabled limiter touched DynamoDB")


def test_disabled_limiter_never_touches_dynamodb() -> None:
    lim = RateLimiter("", dynamodb_resource=_Exploding())
    assert lim.enabled is False
    for _ in range(500):
        lim.check(SUB, is_write=True)


class _FailingTable:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def update_item(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.exc


class _FailingResource:
    def __init__(self, exc: Exception) -> None:
        self.table = _FailingTable(exc)

    def Table(self, name: str) -> Any:  # noqa: N802
        return self.table


@pytest.mark.parametrize(
    "exc",
    [
        ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
            "UpdateItem",
        ),
        ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "UpdateItem"
        ),
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "UpdateItem"),
        EndpointConnectionError(endpoint_url="https://dynamodb.invalid"),
    ],
)
def test_dynamodb_error_fails_open_and_logs(exc: Exception, log_lines, clock: Clock) -> None:
    resource = _FailingResource(exc)
    lim = RateLimiter(TABLE, calls_per_minute=1, dynamodb_resource=resource, clock=clock)
    lim.check(SUB, is_write=True)
    lim.check(SUB, is_write=True)  # would be the 2nd call against a ceiling of 1 — still allowed
    assert resource.table.calls == 4  # calls + writes, twice: the failure did not short-circuit
    lines = [ln for ln in log_lines() if ln["message"] == "ratelimit_unavailable"]
    assert len(lines) == 4
    assert lines[0]["level"] == "WARNING"
    assert lines[0]["subject"] == SUB
    assert lines[0]["exc_class"] == type(exc).__name__
    assert {ln["window"] for ln in lines} == {"calls", "writes"}


def test_non_dynamodb_errors_are_not_swallowed(clock: Clock) -> None:
    lim = RateLimiter(TABLE, dynamodb_resource=_FailingResource(RuntimeError("bug")), clock=clock)
    with pytest.raises(RuntimeError):
        lim.check(SUB, is_write=False)


def test_default_clock_is_wall_time(ddb: Any) -> None:
    lim = RateLimiter(TABLE, dynamodb_resource=ddb)
    lim.check(SUB, is_write=False)  # simply must not raise
