"""Per-subject rate limits — HANDOFF §12.6, increment D.

Counters live in their own small DynamoDB table (``pk = "RL#<subject>"``,
``sk = "<window>"``, ``count``, ``ttl``) so the MCP role can ``UpdateItem`` on
counters without gaining any write on the grant table (§4.7, §8.8).

Two bounds, both fixed windows: calls per minute and writes per hour. A rejection is
a ``ToolError(429, "rate_limited")`` carrying ``retry_after`` seconds. Every
rejection is logged by the transport with the subject and the tool.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.errors import ToolError


class RateLimiter:
    """Fixed-window counters in DynamoDB.

    Args:
        table_name: The rate-limit table. Empty string → every check is a no-op
            (skeleton and unit tests).
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

    @property
    def enabled(self) -> bool:
        return bool(self.table_name)

    def check(self, subject: str, *, is_write: bool) -> None:
        """Increment the window counters for ``subject`` and raise on overflow.
        A no-op when no table is configured.

        Raises:
            ToolError: 429 ``rate_limited`` with ``retry_after``.
        """
        if not self.enabled:
            return
        raise NotImplementedError  # increment D


__all__ = ["RateLimiter", "ToolError"]
