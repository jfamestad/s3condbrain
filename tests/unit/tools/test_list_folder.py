"""``list_folder`` (HANDOFF §10.4, skeleton scope §11.4)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ, Settings
from app.mcp.protocol import ToolContext
from app.mcp.tools.list_folder import TOOL
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "list_folder"
    assert TOOL.scope == SCOPE_READ
    assert "$ref" not in str(TOOL.descriptor())
    assert TOOL.description.startswith("Immediate contents of one folder")


@pytest.fixture
def tree(bucket: Any, settings: Settings) -> None:
    for key, body in {
        "a/racing/notes.md": b"---\ntype: doc\n---\nnotes",
        "a/racing/setup/rear-bar.md": b"x",
        "a/racing/history/2025.md": b"x",
        "a/racing/_listing.json": b"{}",
        "a/racing/_scratch/hidden.md": b"x",
        "a/racing/photo.png": b"x",
        "a/other/x.md": b"x",
        "a/top.md": b"x",
    }.items():
        bucket.put_object(Bucket=settings.bucket, Key=key, Body=body)


def test_lists_folders_and_article_summaries(
    ctx: ToolContext, minter: FakeMinter, tree: None, head_raw: Callable[..., Any]
) -> None:
    result = call(TOOL, ctx, path="/racing")
    assert result["path"] == "/racing"
    assert result["folders"] == ["/racing/history", "/racing/setup"]
    assert result["truncated"] is False
    assert len(result["articles"]) == 1
    summary = result["articles"][0]
    assert summary["path"] == "/racing/notes.md"
    assert summary["type"] == "doc"
    assert summary["trust"] == "unverified"
    assert summary["size_bytes"] == len(b"---\ntype: doc\n---\nnotes")
    assert summary["version"] == head_raw("/racing/notes.md")["ETag"].strip('"')
    assert "title" not in summary
    assert minter.calls == [(OWNER, Shape.LIST, "/racing")]


def test_skips_system_keys_and_non_markdown(ctx: ToolContext, tree: None) -> None:
    result = call(TOOL, ctx, path="/racing")
    paths = [a["path"] for a in result["articles"]]
    assert "/racing/_listing.json" not in paths
    assert "/racing/photo.png" not in paths
    assert "/racing/_scratch" not in result["folders"]


def test_root_lists_top_level(ctx: ToolContext, tree: None) -> None:
    result = call(TOOL, ctx, path="/")
    assert result["folders"] == ["/other", "/racing"]
    assert [a["path"] for a in result["articles"]] == ["/top.md"]


def test_empty_root_is_not_404(ctx: ToolContext) -> None:
    result = call(TOOL, ctx, path="/")
    assert result == {"path": "/", "folders": [], "articles": [], "truncated": False}


def test_folder_with_nothing_under_it_is_404(ctx: ToolContext, tree: None) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path="/racing/nothing")
    expect_error(TOOL, ctx, 404, "not_found", path="/nope")


def test_prefix_match_is_not_a_folder(ctx: ToolContext, tree: None) -> None:
    # "/rac" is a string prefix of "/racing" but not a folder.
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
    assert call(TOOL, reader, path="/racing")["folders"] == ["/racing/history", "/racing/setup"]
    assert [a["path"] for a in call(TOOL, reader, path="/racing/setup")["articles"]] == [
        "/racing/setup/rear-bar.md"
    ]
    expect_error(TOOL, reader, 404, "not_found", path="/")
    expect_error(TOOL, reader, 404, "not_found", path="/other")


def test_s3_access_denied_is_404(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 404, "not_found", path="/racing")
