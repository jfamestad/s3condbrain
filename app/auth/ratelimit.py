"""Per-subject rate limits — HANDOFF §12.6, increment D.

Counters live in their own small DynamoDB table so the MCP role can ``UpdateItem``
on counters without gaining any write on the grant table (§4.7, §8.8).

Table layout (the infra worker builds this; keys are strings, TTL on ``ttl``):

    pk  = "RL#<subject>"                    S, hash key
    sk  = "<window-kind>#<window-start>"    S, range key — ``calls#1757721600``
    count                                   N, incremented with ``ADD``
    ttl                                     N, epoch seconds = window end + 60

Two bounds, both fixed windows: ``calls`` per 60 s and ``writes`` per 3600 s. A call
increments the ``calls`` window first and only touches the ``writes`` window once
that check has passed, so a rejected call never consumes write budget. A rejection
is a ``ToolError(429, "rate_limited")`` carrying ``retry_after`` (integer seconds
to the end of the window). The transport logs every rejection with the subject and
the tool.

**Fails open.** The limiter protects the bill, not the data: authorization has
already happened by the time it runs and nothing it decides changes what a subject
can reach. So a DynamoDB error (throttle, outage, misconfigured table) is logged and
the call is *allowed* — a limiter outage must not become a wiki outage.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError

from app.errors import ToolError

logger = Logger(service="wiki-ratelimit")

_PK_PREFIX = "RL#"
_CALLS_KIND = "calls"
_WRITES_KIND = "writes"
CALLS_WINDOW_SECONDS = 60
WRITES_WINDOW_SECONDS = 3600
_TTL_GRACE_SECONDS = 60

_MESSAGES = {
    _CALLS_KIND: "Too many calls; slow down.",
    _WRITES_KIND: "Too many writes this hour; slow down.",
}


class RateLimiter:
    """Fixed-window counters in DynamoDB.

    Args:
        table_name: The rate-limit table. Empty string → every check is a no-op
            (skeleton and unit tests) and DynamoDB is never touched.
        calls_per_minute: Per-subject ceiling on tool calls.
        writes_per_hour: Per-subject ceiling on mutating tool calls.
        dynamodb_resource: Injected for tests.
        clock: Injected wall clock (``time.time``) for window tests.
    """

    def __init__(
        self,
        table_name: str,
        calls_per_minute: int = 60,
        writes_per_hour: int = 200,
        dynamodb_resource: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.table_name = table_name
        self.calls_per_minute = calls_per_minute
        self.writes_per_hour = writes_per_hour
        self._ddb = dynamodb_resource
        self._clock = clock
        self._table: Any | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.table_name)

    @property
    def table(self) -> Any:
        """Lazy so a disabled limiter never constructs a client."""
        if self._table is None:
            if self._ddb is None:
                self._ddb = boto3.resource("dynamodb")
            self._table = self._ddb.Table(self.table_name)
        return self._table

    def check(self, subject: str, *, is_write: bool) -> None:
        """Increment the window counters for ``subject`` and raise on overflow.
        A no-op when no table is configured.

        Raises:
            ToolError: 429 ``rate_limited`` with ``retry_after``.
        """
        if not self.enabled:
            return
        now = self._clock()
        self._bump(subject, _CALLS_KIND, CALLS_WINDOW_SECONDS, self.calls_per_minute, now)
        if is_write:
            self._bump(subject, _WRITES_KIND, WRITES_WINDOW_SECONDS, self.writes_per_hour, now)

    def _bump(self, subject: str, kind: str, window: int, ceiling: int, now: float) -> None:
        """Atomically increment one window counter; raise 429 when it exceeds ``ceiling``.

        The post-increment count is what DynamoDB returns, so the decision needs no
        second read and no condition expression: the 61st call sees 61 and is
        refused, the 60th sees 60 and passes.
        """
        start = int(now) // window * window
        end = start + window
        try:
            response = self.table.update_item(
                Key={"pk": f"{_PK_PREFIX}{subject}", "sk": f"{kind}#{start}"},
                UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :ttl)",
                ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
                ExpressionAttributeValues={":one": 1, ":ttl": end + _TTL_GRACE_SECONDS},
                ReturnValues="UPDATED_NEW",
            )
            count = int(response["Attributes"]["count"])
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "ratelimit_unavailable",
                subject=subject,
                window=kind,
                exc_class=type(exc).__name__,
                decision="allow",
            )
            return
        if count > ceiling:
            retry_after = max(1, math.ceil(end - now))
            raise ToolError(429, "rate_limited", _MESSAGES[kind], retry_after=retry_after)


__all__ = ["CALLS_WINDOW_SECONDS", "WRITES_WINDOW_SECONDS", "RateLimiter", "ToolError"]
