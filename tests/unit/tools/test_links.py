"""Links (s3condbrain S7): validation and the one-hop reference."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
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
from app.mcp.tools.archive_article import TOOL as ARCHIVE
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.list_folder import TOOL as LIST
from app.mcp.tools.move_article import TOOL as MOVE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.search import TOOL as SEARCH
from app.mcp.tools.unarchive_article import TOOL as UNARCHIVE
from app.mcp.tools.update_article import TOOL as UPDATE
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

LINK_PATH = "/me/racing.md"
FRIEND = "user_friend"


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


def test_read_raw_link_without_target_is_a_link_with_empty_target(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(LINK_PATH, {"type": "link"})
    out = call(READ, ctx, path=LINK_PATH)
    assert out == {"kind": "link", "path": LINK_PATH, "link_to": "", "note": out["note"]}


def test_list_folder_shows_link_resolved_for_owner(ctx: ToolContext) -> None:
    _create_link(ctx, "/racing")
    out = call(LIST, ctx, path="/me")
    [link] = out["articles"]
    assert link["type"] == "link"
    assert link["link_to"] == "/racing"
    assert link["resolved"] is True


def test_list_folder_marks_unreachable_target_unresolved(
    make_ctx: Any, seed_grant: Any, ctx: ToolContext
) -> None:
    seed_grant(FRIEND, "/friend", Permission.WRITE)
    friend = make_ctx(FRIEND)
    _create_link(friend, "/secret", path="/friend/secret.md")
    out = call(LIST, friend, path="/friend")
    [link] = out["articles"]
    assert link["link_to"] == "/secret"
    assert link["resolved"] is False


def test_foreign_link_has_no_resolved_field(ctx: ToolContext) -> None:
    _create_link(ctx, "https://wiki.acme.com/a/standards/torque.md")
    [link] = call(LIST, ctx, path="/me")["articles"]
    assert "resolved" not in link


def test_resolving_a_link_touches_nothing_at_the_target(
    make_ctx: Any, seed_grant: Any, minter: FakeMinter, ctx: ToolContext
) -> None:
    seed_grant(FRIEND, "/friend", Permission.WRITE)
    seed_grant(FRIEND, "/racing", Permission.READ)
    friend = make_ctx(FRIEND)
    _create_link(friend, "/racing", path="/friend/racing.md")
    minter.calls.clear()
    [link] = call(LIST, friend, path="/friend")["articles"]
    assert link["resolved"] is True
    # A grant lookup only: no credential for the target, no audited decision on it.
    assert [path for _subject, _shape, path in minter.calls] == ["/friend"]
    assert ("/racing", Permission.READ.value) not in friend.audit.grants_used


def test_malformed_stored_target_lists_as_unresolved(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(LINK_PATH, {"type": "link", "link_to": "/racing/../secret"})
    [link] = call(LIST, ctx, path="/me")["articles"]
    assert link["link_to"] == "/racing/../secret"
    assert link["resolved"] is False


def test_archive_removes_a_dead_link(ctx: ToolContext) -> None:
    version = _create_link(ctx)["version"]
    call(ARCHIVE, ctx, path=LINK_PATH, if_version=version)
    expect_error(READ, ctx, 404, "not_found", path=LINK_PATH)


def test_unarchive_restores_a_link(ctx: ToolContext) -> None:
    version = _create_link(ctx)["version"]
    archived = call(ARCHIVE, ctx, path=LINK_PATH, if_version=version)
    call(UNARCHIVE, ctx, path=LINK_PATH, if_version=archived["version"])
    assert call(READ, ctx, path=LINK_PATH)["kind"] == "link"


def test_move_keeps_a_link_a_link(ctx: ToolContext) -> None:
    version = _create_link(ctx)["version"]
    moved = "/me/shared/racing.md"
    call(MOVE, ctx, **{"from": LINK_PATH, "to": moved, "if_version": version})
    out = call(READ, ctx, path=moved)
    assert out["kind"] == "link"
    assert out["link_to"] == "/racing"


def test_a_link_grants_nothing(
    make_ctx: Any, seed_grant: Any, ctx: ToolContext, minter: FakeMinter
) -> None:
    # The owner has content at /secret; the friend links to it without a grant.
    call(
        CREATE,
        ctx,
        path="/secret/plan.md",
        frontmatter={"type": "doc", "title": "Plan"},
        content="classified\n",
    )
    seed_grant(FRIEND, "/friend", Permission.WRITE)
    friend = make_ctx(FRIEND)
    _create_link(friend, "/secret", path="/friend/secret.md")
    minter.calls.clear()

    assert call(READ, friend, path="/friend/secret.md")["link_to"] == "/secret"
    expect_error(READ, friend, 404, "not_found", path="/secret/plan.md")
    expect_error(LIST, friend, 404, "not_found", path="/secret")
    hits = call(SEARCH, friend, query="plan")["hits"]
    assert all(not h["path"].startswith("/secret/") for h in hits)
    # No credential was ever minted for the target.
    assert all(not p.startswith("/secret") for _, _, p in minter.calls)


def test_linking_needs_write_on_the_link_folder(
    make_ctx: Any, seed_grant: Any, ctx: ToolContext
) -> None:
    seed_grant(FRIEND, "/racing", Permission.READ)
    friend = make_ctx(FRIEND)
    expect_error(
        CREATE,
        friend,
        403,
        "forbidden",
        path="/racing/mine.md",
        frontmatter={"type": "link", "link_to": "/racing"},
        content="",
    )


def test_paths_under_a_link_do_not_resolve(ctx: ToolContext) -> None:
    call(CREATE, ctx, path="/racing/setup.md", frontmatter={"type": "doc"}, content="x\n")
    _create_link(ctx, "/racing")  # /me/racing.md
    expect_error(LIST, ctx, 404, "not_found", path="/me/racing")
