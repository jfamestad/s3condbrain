"""``resolve_reference`` (HANDOFF §7, §10.6, §10.15).

The tool parses a string. It must never consult the grant store, mint a credential
or touch S3 — the context handed to it here explodes on any such attempt.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.mcp.protocol import ToolContext
from app.mcp.tools.resolve_reference import (
    FOREIGN_NOTE,
    LOCAL_NOTE,
    TOOL,
    local_root,
    parse_reference,
)
from tests.unit.tools.conftest import FakeMinter, call, expect_error

LOCAL_ROOT = "https://wiki-dev.example.com"  # CANONICAL_MCP_URL minus /mcp (tests/conftest.py)
LOCAL_URL = f"{LOCAL_ROOT}/a/racing/setup/rear-bar.md"
FOREIGN_URL = "https://wiki.acme.com/a/standards/torque-spec.md"


class _Exploding:
    """Any attribute access is a test failure: the tool must not look here."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"resolve_reference touched grants.{name}")


@pytest.fixture
def minter() -> FakeMinter:
    return FakeMinter(_Exploding())


@pytest.fixture
def ctx(settings: Settings, minter: FakeMinter) -> ToolContext:
    return ToolContext(
        subject="user_anyone",
        scopes=frozenset(),  # no scope needed (§10.6)
        grants=_Exploding(),  # type: ignore[arg-type]
        minter=minter,  # type: ignore[arg-type]
        settings=settings,
        request_id="req-test",
    )


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "resolve_reference"
    assert TOOL.scope is None
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["url"]
    assert descriptor["inputSchema"]["properties"]["url"]["format"] == "uri"
    assert descriptor["outputSchema"]["required"] == ["url", "root", "path", "local", "resolvable"]
    assert TOOL.description.startswith("Parse a wiki URL and say whether it can be read from here.")


def test_local_reference(ctx: ToolContext, minter: FakeMinter) -> None:
    result = call(TOOL, ctx, url=LOCAL_URL)
    assert result == {
        "url": LOCAL_URL,
        "root": LOCAL_ROOT,
        "path": "/racing/setup/rear-bar.md",
        "local": True,
        "resolvable": True,
        "mcp_endpoint": f"{LOCAL_ROOT}/mcp",
        "note": LOCAL_NOTE,
    }
    assert minter.mint_count == 0


def test_foreign_reference(ctx: ToolContext, minter: FakeMinter) -> None:
    result = call(TOOL, ctx, url=FOREIGN_URL)
    assert result == {
        "url": FOREIGN_URL,
        "root": "https://wiki.acme.com",
        "path": "/standards/torque-spec.md",
        "local": False,
        "resolvable": False,
        "mcp_endpoint": "https://wiki.acme.com/mcp",
        "note": FOREIGN_NOTE.format(root="https://wiki.acme.com"),
    }
    assert "https://wiki.acme.com" in result["note"]
    assert "unresolved citation" in result["note"]
    assert minter.mint_count == 0


def test_same_answer_for_every_caller(settings: Settings, minter: FakeMinter) -> None:
    """§10.15: it reveals nothing about the tree, so identity does not enter into it."""
    answers = {
        call(
            TOOL,
            ToolContext(
                subject=subject,
                scopes=frozenset(),
                grants=_Exploding(),  # type: ignore[arg-type]
                minter=minter,  # type: ignore[arg-type]
                settings=settings,
            ),
            url=FOREIGN_URL,
        )["resolvable"]
        for subject in ("user_owner", "user_nobody", "")
    }
    assert answers == {False}


def test_host_is_case_insensitive(ctx: ToolContext) -> None:
    result = call(TOOL, ctx, url="https://WIKI-DEV.Example.com/a/x.md")
    assert result["local"] is True
    assert result["root"] == LOCAL_ROOT


def test_path_case_is_not_normalised(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", url=f"{LOCAL_ROOT}/a/Racing/x.md")


def test_port_distinguishes_instances(ctx: ToolContext) -> None:
    result = call(TOOL, ctx, url=f"{LOCAL_ROOT}:8443/a/x.md")
    assert result["local"] is False
    assert result["root"] == f"{LOCAL_ROOT}:8443"
    assert result["mcp_endpoint"] == f"{LOCAL_ROOT}:8443/mcp"


def test_query_and_fragment_are_ignored(ctx: ToolContext) -> None:
    result = call(TOOL, ctx, url=f"{LOCAL_URL}?v=3#setup")
    assert result["path"] == "/racing/setup/rear-bar.md"
    assert result["local"] is True


def test_root_article(ctx: ToolContext) -> None:
    assert call(TOOL, ctx, url=f"{LOCAL_ROOT}/a/readme.md")["path"] == "/readme.md"


@pytest.mark.parametrize(
    "url",
    [
        "http://wiki.acme.com/a/x.md",  # not https
        "https:///a/x.md",  # no host
        "https://wiki.acme.com/x.md",  # outside the article namespace
        "https://wiki.acme.com/a",  # namespace without a path
        "https://wiki.acme.com/a/",  # empty path
        "https://wiki.acme.com/a/racing",  # not an article
        "https://wiki.acme.com/a/racing/",  # a folder
        "https://wiki.acme.com/a/_sys/x.md",  # reserved segment
        "https://wiki.acme.com/mcp",  # the endpoint itself
        "https://wiki.acme.com/.well-known/oauth-protected-resource/mcp",
        "https://user:pw@wiki.acme.com/a/x.md",  # userinfo
        "wiki.acme.com/a/x.md",  # no scheme
        "",
        "not a url",
    ],
)
def test_not_a_wiki_reference_is_400(ctx: ToolContext, minter: FakeMinter, url: str) -> None:
    error = expect_error(TOOL, ctx, 400, "bad_request", url=url)
    assert "wiki reference" in error.message or "'url'" in error.message
    assert minter.mint_count == 0


@pytest.mark.parametrize("url", [None, 7, ["https://wiki.acme.com/a/x.md"], "x" * 2049])
def test_non_string_or_oversized_url_is_400(ctx: ToolContext, url: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", url=url)


def test_local_root_strips_mcp_suffix(settings: Settings) -> None:
    assert local_root(settings) == LOCAL_ROOT
    bare = Settings(
        **{
            **settings.__dict__,
            "canonical_mcp_url": "https://wiki.example.org/",
        }
    )
    assert local_root(bare) == "https://wiki.example.org"


def test_parse_reference_returns_root_and_path() -> None:
    assert parse_reference("https://Wiki.Acme.com/a/x.md?q#f") == (
        "https://wiki.acme.com",
        "/x.md",
    )
