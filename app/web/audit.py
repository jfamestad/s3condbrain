"""The per-path audit view over the tool-call log (HANDOFF §8.9, §12.7, AS-10).

The MCP function writes one structured line per tool call — ``tool_call`` with
``subject``, ``tool``, ``path``, ``decision``, ``status`` — reads and denials
included. "What happened to my article" is therefore a CloudWatch Logs Insights
query on that log group filtered by ``path``; nothing is duplicated into a table.

Insights is asynchronous: ``start_query`` then poll ``get_query_results`` until the
status is terminal. A Lambda page cannot wait forever, so the poll is bounded (about
ten seconds by default); on the deadline the query is stopped and whatever rows
have arrived are returned with ``partial=True`` so the page can say so.

The web role holds ``logs:StartQuery``/``GetQueryResults``/``StopQuery`` on exactly
the MCP log group (infra/stacks/compute.py). Only the fields the page shows are
selected — never the whole line.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3

QUERY_LIMIT = 200
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 0.5
_TERMINAL = frozenset({"Complete", "Failed", "Cancelled", "Timeout", "Unknown"})
_FIELDS = ("@timestamp", "subject", "tool", "decision", "status", "request_id")


@dataclass(frozen=True)
class AuditRow:
    """One tool call against the path, as the page shows it."""

    at: str
    subject: str
    tool: str
    decision: str
    status: str
    request_id: str = ""


@dataclass
class AuditResult:
    rows: list[AuditRow] = field(default_factory=list)
    partial: bool = False  # the poll deadline passed before the query completed
    status: str = ""  # Insights' terminal status, for the log line


def escape(value: str) -> str:
    """Escape a string literal for an Insights ``filter`` clause. The path grammar
    admits neither character, but the query is built from user input regardless."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_query(path: str, limit: int = QUERY_LIMIT) -> str:
    """The Insights query for one path, newest first."""
    return (
        f"fields {', '.join(_FIELDS)}"
        f' | filter message = "tool_call" and path = "{escape(path)}"'
        f" | sort @timestamp desc | limit {int(limit)}"
    )


def _row(result: list[dict[str, Any]]) -> AuditRow:
    values = {str(f.get("field", "")): str(f.get("value", "")) for f in result}
    return AuditRow(
        at=values.get("@timestamp", ""),
        subject=values.get("subject", ""),
        tool=values.get("tool", ""),
        decision=values.get("decision", ""),
        status=values.get("status", ""),
        request_id=values.get("request_id", ""),
    )


class AuditQuery:
    """Runs the per-path query against one log group.

    Args:
        log_group: The MCP function's log group name (``MCP_LOG_GROUP``).
        client: Injected CloudWatch Logs client for tests; defaults to boto3's.
        timeout_seconds: How long to wait for the query before returning partial rows.
        poll_seconds: Interval between ``get_query_results`` calls.
        sleep: Injected for tests so the poll loop does not actually wait.
        clock: Monotonic clock, injected for tests.
    """

    def __init__(
        self,
        log_group: str,
        client: Any | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not log_group:
            raise ValueError("audit log group is not configured")
        self.log_group = log_group
        self._client = client
        self._timeout = timeout_seconds
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("logs")
        return self._client

    def query(self, path: str, since_days: int = 30) -> AuditResult:
        """Every logged tool call on ``path`` in the last ``since_days`` days,
        newest first, at most ``QUERY_LIMIT`` rows.

        Raises:
            botocore.exceptions.ClientError: when Logs refuses the query (the page
                reports it; nothing here retries).
        """
        end = int(time.time())
        start = end - max(1, int(since_days)) * 86400
        started = self.client.start_query(
            logGroupName=self.log_group,
            startTime=start,
            endTime=end,
            queryString=build_query(path),
            limit=QUERY_LIMIT,
        )
        query_id = str(started["queryId"])
        deadline = self._clock() + self._timeout
        result = AuditResult()
        while True:
            response = self.client.get_query_results(queryId=query_id)
            status = str(response.get("status", "Unknown"))
            result.rows = [_row(r) for r in response.get("results") or []]
            result.status = status
            if status in _TERMINAL:
                return result
            if self._clock() >= deadline:
                result.partial = True
                self._stop(query_id)
                return result
            self._sleep(self._poll)

    def _stop(self, query_id: str) -> None:
        try:
            self.client.stop_query(queryId=query_id)
        except Exception:  # noqa: BLE001 — best effort; the rows in hand are the answer
            pass


__all__ = ["QUERY_LIMIT", "AuditQuery", "AuditResult", "AuditRow", "build_query", "escape"]
