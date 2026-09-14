"""``list_folder`` (HANDOFF §10.4) over the ``_listing.json`` projection (§8.6)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ, Settings
from app.mcp.protocol import ToolContext
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.list_folder import TOOL
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.listings import listing_key
from app.storage.markdown import serialize
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "list_folder"
    assert TOOL.scope == SCOPE_READ
    assert "$ref" not in str(TOOL.descriptor())
    assert TOOL.description.startswith("Immediate contents of one folder")


@pytest.fixture
def tree(bucket: Any, settings: Settings) -> None:
    """Objects written behind the tools' back: no listings exist yet."""
    for key, body in {
        "a/racing/notes.md": serialize(
            {
                "type": "doc",
                "title": "Notes",
                "description": "Season notes",
                "tags": ["racing", "log"],
                "status": "draft",
                "stale_after": "2000-01-01T00:00:00Z",
                "seq": 4,
                "verified": [{"by": "human:josh"}],
            },
            "notes",
        ),
        "a/racing/moved.md": serialize({"type": "pointer", "moved_to": "/x.md", "seq": 2}, ""),
        "a/racing/gone.md": serialize({"type": "archived", "seq": 2}, ""),
        "a/racing/setup/rear-bar.md": serialize({"type": "doc", "title": "Rear bar"}, "x"),
        "a/racing/history/2025.md": serialize({"type": "archived"}, ""),
        "a/racing/_scratch/hidden.md": b"x",
        "a/racing/photo.png": b"x",
        "a/other/x.md": serialize({"type": "doc"}, "x"),
        "a/top.md": serialize({"type": "doc", "title": "Top"}, "x"),
    }.items():
        bucket.put_object(Bucket=settings.bucket, Key=key, Body=body)


@pytest.fixture
def has_listing(bucket: Any, settings: Settings) -> Callable[[str], bool]:
    def _has(folder: str) -> bool:
        listed = bucket.list_objects_v2(Bucket=settings.bucket, Prefix=listing_key(folder))
        return any(o["Key"] == listing_key(folder) for o in listed.get("Contents", []))

    return _has


def test_lists_visible_folders_and_full_summaries(
    ctx: ToolContext, minter: FakeMinter, tree: None, head_raw: Callable[..., Any]
) -> None:
    result = call(TOOL, ctx, path="/racing")
    assert result["path"] == "/racing"
    # ``history`` holds only a tombstone: invisible. ``_scratch`` is a system prefix.
    assert result["folders"] == ["/racing/setup"]
    assert result["truncated"] is False
    assert [a["path"] for a in result["articles"]] == ["/racing/notes.md"]
    summary = result["articles"][0]
    assert summary == {
        "path": "/racing/notes.md",
        "type": "doc",
        "version": head_raw("/racing/notes.md")["ETag"].strip('"'),
        "trust": "human-reviewed",
        "title": "Notes",
        "description": "Season notes",
        "tags": ["racing", "log"],
        "status": "draft",
        "stale": True,
        "size_bytes": head_raw("/racing/notes.md")["ContentLength"],
        "seq": 4,
    }
    # The owner holds write on the folder, so the rebuild ran under MAINTAIN.
    assert minter.calls == [(OWNER, Shape.MAINTAIN, "/racing")]


def test_owner_read_persists_the_rebuilt_listing(
    ctx: ToolContext, tree: None, has_listing: Callable[[str], bool]
) -> None:
    assert not has_listing("/racing")
    call(TOOL, ctx, path="/racing")
    assert has_listing("/racing")
    assert not has_listing("/racing/setup")  # descendants are never written from a parent


def test_reader_gets_the_same_result_without_persisting(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    tree: None,
    has_listing: Callable[[str], bool],
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    result = call(TOOL, make_ctx("user_reader"), path="/racing")
    assert result["folders"] == ["/racing/setup"]
    assert [a["title"] for a in result["articles"]] == ["Notes"]
    assert minter.calls == [("user_reader", Shape.LIST, "/racing")]
    assert not has_listing("/racing")


def test_pointers_and_tombstones_never_appear(ctx: ToolContext, tree: None) -> None:
    paths = [a["path"] for a in call(TOOL, ctx, path="/racing")["articles"]]
    assert "/racing/moved.md" not in paths
    assert "/racing/gone.md" not in paths
    assert "/racing/_listing.json" not in paths
    assert "/racing/photo.png" not in paths


def test_root_lists_top_level(ctx: ToolContext, tree: None) -> None:
    result = call(TOOL, ctx, path="/")
    assert result["folders"] == ["/other", "/racing"]
    assert [a["path"] for a in result["articles"]] == ["/top.md"]


def test_empty_root_is_not_404_and_writes_nothing(
    ctx: ToolContext, has_listing: Callable[[str], bool]
) -> None:
    assert call(TOOL, ctx, path="/") == {
        "path": "/",
        "folders": [],
        "articles": [],
        "truncated": False,
    }
    assert not has_listing("/")


def test_folder_with_nothing_under_it_is_404_and_writes_nothing(
    ctx: ToolContext, tree: None, has_listing: Callable[[str], bool]
) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path="/racing/nothing")
    expect_error(TOOL, ctx, 404, "not_found", path="/nope")
    assert not has_listing("/racing/nothing")
    assert not has_listing("/nope")


def test_folder_holding_only_tombstones_is_404(ctx: ToolContext, tree: None) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path="/racing/history")


def test_prefix_match_is_not_a_folder(ctx: ToolContext, tree: None) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path="/rac")


@pytest.mark.parametrize("path", ["/racing/", "racing", "/Racing", "/_sys", "", None, "/a//b"])
def test_bad_folder_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path)
    assert minter.mint_count == 0


def test_zero_grant_subject_is_404_and_nothing_minted(
    nobody: ToolContext, minter: FakeMinter, tree: None
) -> None:
    expect_error(TOOL, nobody, 404, "not_found", path="/racing")
    expect_error(TOOL, nobody, 404, "not_found", path="/")
    assert minter.mint_count == 0


def test_grant_elsewhere_is_404_not_403(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    tree: None,
) -> None:
    seed_grant("user_other", "/other", Permission.OWN)
    expect_error(TOOL, make_ctx("user_other"), 404, "not_found", path="/racing")
    assert minter.mint_count == 0


def test_article_grant_does_not_list_parent(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    tree: None,
) -> None:
    # §4.6: searchable, not listable — the siblings are not theirs to see.
    seed_grant("user_one", "/racing/notes.md", Permission.READ)
    expect_error(TOOL, make_ctx("user_one"), 404, "not_found", path="/racing")
    assert minter.mint_count == 0


def test_folder_read_grant_lists_that_folder_and_below(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None], tree: None
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    reader = make_ctx("user_reader")
    assert call(TOOL, reader, path="/racing")["folders"] == ["/racing/setup"]
    assert [a["path"] for a in call(TOOL, reader, path="/racing/setup")["articles"]] == [
        "/racing/setup/rear-bar.md"
    ]
    expect_error(TOOL, reader, 404, "not_found", path="/")
    expect_error(TOOL, reader, 404, "not_found", path="/other")


def test_s3_access_denied_is_404(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 404, "not_found", path="/racing")


# --- the projection is what the write path maintains -------------------------------------


def test_create_then_list_shows_the_new_article(ctx: ToolContext) -> None:
    fm = {"type": "doc", "title": "Rear bar", "description": "Sway bar", "tags": ["setup"]}
    created = call(CREATE, ctx, path="/racing/setup/rear-bar.md", content="b", frontmatter=fm)
    result = call(TOOL, ctx, path="/racing/setup")
    assert result["articles"] == [
        {
            "path": "/racing/setup/rear-bar.md",
            "type": "doc",
            "version": created["version"],
            "trust": "unverified",
            "title": "Rear bar",
            "description": "Sway bar",
            "tags": ["setup"],
            "size_bytes": len(serialize({**fm, "seq": 1}, "b")),
            "seq": 1,
        }
    ]
    # And the folder chain became visible from the root.
    assert call(TOOL, ctx, path="/")["folders"] == ["/racing"]
    assert call(TOOL, ctx, path="/racing")["folders"] == ["/racing/setup"]


def test_update_refreshes_the_projection(ctx: ToolContext) -> None:
    created = call(
        CREATE, ctx, path="/r/a.md", content="b", frontmatter={"type": "doc", "title": "One"}
    )
    updated = call(
        UPDATE,
        ctx,
        path="/r/a.md",
        content="c",
        if_version=created["version"],
        frontmatter={"type": "doc", "title": "Two", "status": "stable"},
    )
    [summary] = call(TOOL, ctx, path="/r")["articles"]
    assert (summary["title"], summary["status"], summary["version"], summary["seq"]) == (
        "Two",
        "stable",
        updated["version"],
        2,
    )


def test_article_only_writer_skips_refresh_and_folder_read_repairs(
    ctx: ToolContext,
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    bucket: Any,
    settings: Settings,
) -> None:
    created = call(
        CREATE, ctx, path="/r/a.md", content="b", frontmatter={"type": "doc", "title": "One"}
    )
    call(TOOL, ctx, path="/r")  # listing exists and is current

    seed_grant("user_editor", "/r/a.md", Permission.WRITE)
    editor = make_ctx("user_editor")
    minted_before = minter.mint_count
    updated = call(
        UPDATE,
        editor,
        path="/r/a.md",
        content="c",
        if_version=created["version"],
        frontmatter={"type": "doc", "title": "Edited"},
    )
    # Only the WRITE credential for the key: no folder-level write, no MAINTAIN mint.
    assert minter.calls[minted_before:] == [("user_editor", Shape.WRITE, "/r/a.md")]

    stored = json.loads(
        bucket.get_object(Bucket=settings.bucket, Key=listing_key("/r"))["Body"].read()
    )
    assert stored["children"][0]["title"] == "One"  # stale, by design (§4.6)

    [summary] = call(TOOL, ctx, path="/r")["articles"]  # owner's read self-heals
    assert (summary["title"], summary["version"]) == ("Edited", updated["version"])
    stored = json.loads(
        bucket.get_object(Bucket=settings.bucket, Key=listing_key("/r"))["Body"].read()
    )
    assert stored["children"][0]["title"] == "Edited"


def test_stale_listing_self_heals_on_read(
    ctx: ToolContext, bucket: Any, settings: Settings, put_raw: Callable[..., str]
) -> None:
    call(CREATE, ctx, path="/r/a.md", content="b", frontmatter={"type": "doc", "title": "A"})
    put_raw("/r/b.md", {"type": "doc", "title": "B"})  # behind the listing's back
    assert [a["title"] for a in call(TOOL, ctx, path="/r")["articles"]] == ["A", "B"]
