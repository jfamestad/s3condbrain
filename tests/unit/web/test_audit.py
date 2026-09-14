"""``AuditQuery`` over a stub CloudWatch Logs client: the query text, the poll loop,
the bounded wait with ``partial``, and row parsing."""

from __future__ import annotations

from typing import Any

import pytest

from app.web.audit import QUERY_LIMIT, AuditQuery, AuditRow, build_query, escape


class StubLogs:
    """Answers ``get_query_results`` from a script of ``(status, results)`` pages."""

    def __init__(self, pages: list[tuple[str, list[list[dict[str, str]]]]]) -> None:
        self.pages = list(pages)
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.polls = 0

    def start_query(self, **kwargs: Any) -> dict[str, str]:
        self.started.append(kwargs)
        return {"queryId": "q-1"}

    def get_query_results(self, queryId: str) -> dict[str, Any]:  # noqa: N803 — boto3 casing
        assert queryId == "q-1"
        self.polls += 1
        status, results = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return {"status": status, "results": results}

    def stop_query(self, queryId: str) -> dict[str, bool]:  # noqa: N803
        self.stopped.append(queryId)
        return {"success": True}


def _result(**fields: str) -> list[dict[str, str]]:
    return [{"field": k, "value": v} for k, v in fields.items()]


ROW = _result(
    **{
        "@timestamp": "2026-09-13 10:00:00.000",
        "subject": "user_dana",
        "tool": "update_article",
        "decision": "allow",
        "status": "200",
        "request_id": "r1",
    }
)


def test_query_text_selects_fields_filters_path_and_escapes() -> None:
    q = build_query("/racing/setup/x.md")
    assert q.startswith("fields @timestamp, subject, tool, decision, status, request_id")
    assert '| filter message = "tool_call" and path = "/racing/setup/x.md"' in q
    assert q.endswith(f"| sort @timestamp desc | limit {QUERY_LIMIT}")
    assert escape('a"b\\c') == 'a\\"b\\\\c'
    assert 'path = "a\\"b"' in build_query('a"b')


def test_complete_query_returns_rows_newest_first_as_given() -> None:
    logs = StubLogs([("Running", []), ("Complete", [ROW, _result(subject="user_pat")])])
    aq = AuditQuery("/aws/lambda/mcp", client=logs, sleep=lambda s: None)
    out = aq.query("/racing/setup/x.md", since_days=30)
    assert not out.partial and out.status == "Complete"
    assert out.rows[0] == AuditRow(
        "2026-09-13 10:00:00.000", "user_dana", "update_article", "allow", "200", "r1"
    )
    assert out.rows[1].subject == "user_pat" and out.rows[1].tool == ""
    assert logs.polls == 2 and logs.stopped == []
    started = logs.started[0]
    assert started["logGroupName"] == "/aws/lambda/mcp" and started["limit"] == QUERY_LIMIT
    assert started["endTime"] - started["startTime"] == 30 * 86400
    assert started["queryString"] == build_query("/racing/setup/x.md")


def test_deadline_returns_partial_rows_and_stops_the_query() -> None:
    logs = StubLogs([("Running", [ROW])])
    ticks = iter([0.0, 0.0, 5.0, 11.0])
    slept: list[float] = []
    aq = AuditQuery(
        "g",
        client=logs,
        timeout_seconds=10,
        poll_seconds=0.25,
        sleep=slept.append,
        clock=lambda: next(ticks),
    )
    out = aq.query("/x.md")
    assert out.partial and out.rows and out.rows[0].subject == "user_dana"
    assert logs.stopped == ["q-1"] and slept == [0.25, 0.25]


def test_failed_status_is_terminal_with_whatever_arrived() -> None:
    logs = StubLogs([("Failed", [])])
    out = AuditQuery("g", client=logs, sleep=lambda s: None).query("/x.md")
    assert out.rows == [] and not out.partial and out.status == "Failed"


def test_empty_log_group_is_refused() -> None:
    with pytest.raises(ValueError):
        AuditQuery("")


def test_stop_failure_is_swallowed() -> None:
    class Grumpy(StubLogs):
        def stop_query(self, queryId: str) -> dict[str, bool]:  # noqa: N803
            raise RuntimeError("no")

    logs = Grumpy([("Running", [])])
    aq = AuditQuery("g", client=logs, timeout_seconds=0, sleep=lambda s: None)
    assert aq.query("/x.md").partial
