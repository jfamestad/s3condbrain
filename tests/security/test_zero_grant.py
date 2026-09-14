"""§12.8 item 5, the observable half — a user with zero grants sees nothing.

Listing and direct reads answer 404 (never 403 — §10.4 indistinguishability), a
write is a plain grant denial that names no scope (§6.5 row 3), and nothing was
written. The other half of item 5 — that no ``AssumeRole`` was made for any of
these requests — is asserted offline in ``tests/unit/tools`` (``mint_count == 0``);
CloudTrail is too slow to assert on here. See README.md.

The zero-grant token must carry both scopes, exactly like the owner's: this file
tests the grant layer, and a token short of ``wiki.write`` would produce an HTTP 403
``insufficient_scope`` instead, which is the *other* row of §6.5.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.security

Call = Callable[..., httpx.Response]
Parse = Callable[[httpx.Response], dict[str, Any]]

PROBE_PATH = "/probe.md"
SCOPE_WORDS = ("scope", "wiki.read", "wiki.write", "insufficient")


def _tool(
    http: httpx.Client, call: Call, token: str, name: str, **arguments: Any
) -> httpx.Response:
    return call(http, token, "tools/call", {"name": name, "arguments": arguments})


def _assert_not_http_denial(response: httpx.Response) -> None:
    """Grant denials are tool errors inside a 200; an HTTP 401/403 here means the
    request never reached the grant layer, which is a different finding."""
    assert response.status_code == 200, f"{response.status_code}: {response.text[:300]}"
    assert "www-authenticate" not in response.headers, dict(response.headers)


# ------------------------------------------------------------ reads see nothing


def test_list_root_is_404(
    http: httpx.Client, call: Call, token_nogrants: str, tool_error: Parse
) -> None:
    response = _tool(http, call, token_nogrants, "list_folder", path="/")
    _assert_not_http_denial(response)
    envelope = tool_error(response)
    assert envelope["status"] == 404, envelope
    assert envelope["code"] == "not_found", envelope


def test_read_article_is_404_not_403(
    http: httpx.Client, call: Call, token_nogrants: str, tool_error: Parse
) -> None:
    response = _tool(http, call, token_nogrants, "read_article", path="/anything.md")
    _assert_not_http_denial(response)
    envelope = tool_error(response)
    assert envelope["status"] == 404, "a path you may not see is indistinguishable from none"
    assert envelope["code"] == "not_found", envelope


def test_search_is_empty(
    http: httpx.Client, call: Call, token_nogrants: str, rpc_result: Parse
) -> None:
    """``search`` arrives with increment A; until then this skips rather than passes."""
    names = {t["name"] for t in rpc_result(call(http, token_nogrants, "tools/list", {}))["tools"]}
    if "search" not in names:
        pytest.skip("search tool not deployed (increment A)")
    result = rpc_result(_tool(http, call, token_nogrants, "search", query="probe"))
    assert result.get("isError") is not True, result
    assert result["structuredContent"]["hits"] == [], result


# ------------------------------------------------------------ writes are refused, quietly


def test_create_is_forbidden_without_naming_a_scope(
    http: httpx.Client,
    call: Call,
    token: str,
    token_nogrants: str,
    tool_error: Parse,
) -> None:
    # Precondition: nothing at the probe path. A leftover here means an earlier run's
    # create *succeeded* — investigate and remove it before trusting this test again.
    before = tool_error(_tool(http, call, token, "read_article", path=PROBE_PATH))
    assert before["status"] == 404, f"stale {PROBE_PATH} exists from an earlier run: {before}"

    response = _tool(
        http,
        call,
        token_nogrants,
        "create_article",
        path=PROBE_PATH,
        content="probe",
        frontmatter={"type": "doc", "title": "probe"},
    )
    _assert_not_http_denial(response)
    envelope = tool_error(response)
    assert envelope["status"] == 403, envelope
    assert envelope["code"] == "forbidden", envelope
    message = envelope["message"].lower()
    for word in SCOPE_WORDS:
        assert word not in message, (
            f"§6.5: a grant denial must not read as a scope error: {message!r}"
        )


def test_probe_was_not_created(
    http: httpx.Client, call: Call, token: str, tool_error: Parse
) -> None:
    """Runs after the create attempt (file order): the owner, who can read everything,
    still finds nothing at the probe path."""
    envelope = tool_error(_tool(http, call, token, "read_article", path=PROBE_PATH))
    assert envelope["status"] == 404, f"{PROBE_PATH} exists: the zero-grant create went through"
