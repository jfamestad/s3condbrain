"""``search_and_read`` — ``search`` then ``read_article`` on the top hits, one call."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.mcp.protocol import ToolContext
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.search_and_read import TOOL
from tests.unit.tools.conftest import FakeMinter, call, expect_error


def _doc(ctx: ToolContext, path: str, title: str, body: str) -> None:
    call(CREATE, ctx, path=path, content=body, frontmatter={"type": "doc", "title": title})


@pytest.fixture
def three(ctx: ToolContext) -> None:
    # Scores: title "gearbox" (3) + name "gearbox" (2) beats title only.
    _doc(ctx, "/a/gearbox.md", "Gearbox notes", "# Gearbox\n\n## Oil\n\nuse 75w90\n")
    _doc(ctx, "/a/gears.md", "Gearbox ratios", "## Ratios\n\nshort\n")
    _doc(ctx, "/b/zz.md", "Gearbox rebuild", "## Oil\n\nflush it\n\n## Parts\n\nlist\n")


# --- 7. descriptor ---------------------------------------------------------------


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "search_and_read"
    assert TOOL.scope == SCOPE_READ
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["query"]
    assert set(descriptor["inputSchema"]["properties"]) == {
        "query",
        "prefix",
        "k",
        "section",
        "max_bytes",
    }
    assert TOOL.description.startswith("Search, then return the top hits' content")


def test_registered_after_search() -> None:
    from app.mcp.tools import registry

    names = list(registry())
    assert names.index("search_and_read") == names.index("search") + 1


# --- 1. top k with content, in search order ---------------------------------------


def test_k_hits_with_content_in_search_order(ctx: ToolContext, three: None) -> None:
    result = call(TOOL, ctx, query="gearbox", k=2)
    assert result["search_truncated"] is True
    paths = [item["path"] for item in result["results"]]
    assert paths == ["/a/gearbox.md", "/a/gears.md"]
    first = result["results"][0]
    assert first["content"] == "# Gearbox\n\n## Oil\n\nuse 75w90\n"
    assert first["title"] == "Gearbox notes"
    assert first["score"] == 5
    assert first["truncated"] is False
    assert first["total_bytes"] == first["returned_bytes"] == len(first["content"].encode())
    assert "version" in first


def test_default_k_is_three(ctx: ToolContext, three: None) -> None:
    result = call(TOOL, ctx, query="gearbox")
    assert len(result["results"]) == 3
    assert result["search_truncated"] is False


# --- 2. section ----------------------------------------------------------------------


def test_section_per_item_and_missing_section_is_a_per_item_404(
    ctx: ToolContext, three: None
) -> None:
    result = call(TOOL, ctx, query="gearbox", section="oil")
    by_path = {item["path"]: item for item in result["results"]}
    assert by_path["/a/gearbox.md"]["content"] == "## Oil\n\nuse 75w90\n"
    assert by_path["/b/zz.md"]["content"] == "## Oil\n\nflush it\n\n"
    assert by_path["/a/gears.md"]["error"] == {"status": 404, "code": "not_found"}
    assert "content" not in by_path["/a/gears.md"]


# --- 3. the budget -------------------------------------------------------------------


def test_budget_truncates_then_skips(ctx: ToolContext, three: None) -> None:
    result = call(TOOL, ctx, query="gearbox", max_bytes=10)
    first, *rest = result["results"]
    assert first["content"] == "# Gearbox\n"
    assert first["returned_bytes"] == 10
    assert first["truncated"] is True
    assert rest
    for item in rest:
        assert item["skipped"] == "budget"
        assert "content" not in item
        assert item["title"]


def test_budget_cuts_on_a_character_boundary(ctx: ToolContext) -> None:
    _doc(ctx, "/a/utf.md", "Utf gearbox", "ééé")  # 6 bytes
    result = call(TOOL, ctx, query="utf", max_bytes=3)
    item = result["results"][0]
    assert item["content"] == "é"
    assert item["returned_bytes"] == 2
    assert item["truncated"] is True


def test_budget_exactly_spent_skips_the_rest(ctx: ToolContext, three: None) -> None:
    size = len(b"# Gearbox\n\n## Oil\n\nuse 75w90\n")
    result = call(TOOL, ctx, query="gearbox", max_bytes=size)
    first, second, _ = result["results"]
    assert first["truncated"] is False
    assert second["skipped"] == "budget"


# --- 4. links are references, never followed ------------------------------------------


def test_link_hit_is_a_reference_and_the_target_is_not_read(
    ctx: ToolContext, three: None, minter: FakeMinter
) -> None:
    call(
        CREATE,
        ctx,
        path="/c/linky.md",
        content="",
        frontmatter={"type": "link", "link_to": "/b/zz.md", "title": "Linky"},
    )
    before = len(minter.calls)
    result = call(TOOL, ctx, query="linky")
    (item,) = result["results"]
    assert item["reference"]["kind"] == "link"
    assert item["reference"]["link_to"] == "/b/zz.md"
    assert "content" not in item
    assert all(path != "/b/zz.md" for _, _, path in minter.calls[before:])


# --- 5. never more than individual reads ---------------------------------------------


def test_results_only_from_granted_paths(
    ctx: ToolContext,
    three: None,
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_a", "/a", Permission.READ)
    before = len(minter.calls)
    result = call(TOOL, make_ctx("user_a"), query="gearbox", k=10)
    assert [item["path"] for item in result["results"]] == ["/a/gearbox.md", "/a/gears.md"]
    assert all("content" in item for item in result["results"])
    calls = minter.calls[before:]
    assert calls
    assert all(not path.startswith("/b") for _, _, path in calls)
    assert all(shape in (Shape.LIST, Shape.READ) for _, shape, _ in calls)


# --- 6. zero grants ------------------------------------------------------------------


def test_zero_grants_is_empty_and_mints_nothing(
    ctx: ToolContext, three: None, nobody: ToolContext, minter: FakeMinter
) -> None:
    before = minter.mint_count
    assert call(TOOL, nobody, query="gearbox") == {"results": [], "search_truncated": False}
    assert minter.mint_count == before


# --- arguments -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"k": 0},
        {"k": 11},
        {"k": True},
        {"max_bytes": 0},
        {"max_bytes": 100_001},
        {"section": ""},
        {"section": 3},
        {"prefix": "a"},
    ],
)
def test_bad_arguments_are_400_and_nothing_minted(
    ctx: ToolContext, minter: FakeMinter, extra: dict[str, Any]
) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", query="gearbox", **extra)
    assert minter.mint_count == 0


def test_missing_query_is_400(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 400, "bad_request")
