"""Links (s3condbrain S7): validation and the one-hop reference."""

from __future__ import annotations

from typing import Any

import pytest

from app.auth.credentials import Shape
from app.errors import ToolError
from app.mcp.protocol import ToolContext
from app.mcp.tools._links import (
    LINK_TO,
    TYPE_LINK,
    check_link,
    is_local,
    link_reference,
    link_target,
)
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.update_article import TOOL as UPDATE
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

LINK_PATH = "/me/racing.md"


@pytest.mark.parametrize(
    "target",
    [
        "/racing",
        "/",
        "/racing/setup/rear-bar.md",
        "https://wiki.acme.com/a/standards/torque-spec.md",
    ],
)
def test_valid_targets(target: str) -> None:
    assert link_target(target) == target


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "racing",
        "/Racing",
        "/racing/",
        "/_sys/x.md",
        "/x.md/y.md",
        "http://wiki.acme.com/a/x.md",
        "https://wiki.acme.com/racing",  # folder URLs are not yet in the §7 grammar
        "https://user@wiki.acme.com/a/x.md",
    ],
)
def test_invalid_targets_are_400(target: object) -> None:
    with pytest.raises(ToolError) as info:
        link_target(target)
    assert info.value.status == 400


def test_check_link_requires_target_on_links() -> None:
    with pytest.raises(ToolError):
        check_link({"type": TYPE_LINK})
    check_link({"type": TYPE_LINK, LINK_TO: "/racing"})


def test_check_link_rejects_target_on_non_links() -> None:
    with pytest.raises(ToolError):
        check_link({"type": "doc", LINK_TO: "/racing"})
    check_link({"type": "doc"})


def test_is_local() -> None:
    assert is_local("/racing")
    assert not is_local("https://wiki.acme.com/a/x.md")


def test_link_reference_shape() -> None:
    ref = link_reference("/me/racing.md", {"type": TYPE_LINK, LINK_TO: "/racing"})
    assert ref["kind"] == "link"
    assert ref["path"] == "/me/racing.md"
    assert ref["link_to"] == "/racing"
    assert "list_folder" in ref["note"]


def _create_link(ctx: ToolContext, target: str = "/racing", path: str = LINK_PATH) -> Any:
    return call(
        CREATE,
        ctx,
        path=path,
        frontmatter={"type": "link", "link_to": target, "title": "Racing (shared)"},
        content="",
    )


def test_create_link(ctx: ToolContext) -> None:
    assert _create_link(ctx)["seq"] == 1


def test_create_link_without_target_is_400(ctx: ToolContext) -> None:
    expect_error(
        CREATE, ctx, 400, "bad_request", path=LINK_PATH, frontmatter={"type": "link"}, content=""
    )


def test_create_link_with_bad_target_is_400(ctx: ToolContext) -> None:
    expect_error(
        CREATE,
        ctx,
        400,
        "bad_request",
        path=LINK_PATH,
        frontmatter={"type": "link", "link_to": "racing"},
        content="",
    )


def test_link_to_on_a_doc_is_400(ctx: ToolContext) -> None:
    expect_error(
        CREATE,
        ctx,
        400,
        "bad_request",
        path=LINK_PATH,
        frontmatter={"type": "doc", "link_to": "/racing"},
        content="",
    )


def test_retarget_link_with_update(ctx: ToolContext) -> None:
    version = _create_link(ctx)["version"]
    out = call(
        UPDATE,
        ctx,
        path=LINK_PATH,
        if_version=version,
        frontmatter={"type": "link", "link_to": "/family"},
        content="",
    )
    assert out["seq"] == 2


def test_update_link_to_bad_target_is_400(ctx: ToolContext) -> None:
    version = _create_link(ctx)["version"]
    expect_error(
        UPDATE,
        ctx,
        400,
        "bad_request",
        path=LINK_PATH,
        if_version=version,
        frontmatter={"type": "link", "link_to": "nope"},
        content="",
    )


def test_read_link_returns_reference_and_reads_nothing_at_target(
    ctx: ToolContext, minter: FakeMinter
) -> None:
    _create_link(ctx, "/racing/setup/rear-bar.md")
    minter.calls.clear()
    out = call(READ, ctx, path=LINK_PATH)
    assert out == {
        "kind": "link",
        "path": LINK_PATH,
        "link_to": "/racing/setup/rear-bar.md",
        "note": out["note"],
    }
    assert minter.calls == [(OWNER, Shape.READ, LINK_PATH)]  # never the target


def test_read_descriptor_advertises_link_variant() -> None:
    kinds = [v["properties"]["kind"].get("const") for v in READ.output_schema["oneOf"][1:]]
    assert "link" in kinds
