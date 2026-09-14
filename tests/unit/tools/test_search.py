"""``search`` (HANDOFF §10.3, §8.7): metadata matching filtered by path against the
caller's grants, with every read under a credential minted for a granted path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Grant, Permission
from app.config import SCOPE_READ, Settings
from app.mcp.protocol import ToolContext
from app.mcp.tools.search import DEFAULT_LIMIT, TOOL, score, searchable_area, tokens
from app.storage.listings import ListingChild
from app.storage.markdown import serialize
from tests.unit.tools.conftest import FakeMinter, call, expect_error


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "search"
    assert TOOL.scope == SCOPE_READ
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["query"]
    assert set(descriptor["inputSchema"]["properties"]) == {"query", "prefix", "limit"}
    assert descriptor["outputSchema"]["required"] == ["hits", "truncated"]
    assert TOOL.description.startswith("Find articles by words")


# --- fixtures ----------------------------------------------------------------------------


ARTICLES: dict[str, dict[str, Any]] = {
    "/racing/setup/rear-bar.md": {
        "type": "doc",
        "title": "Rear bar settings",
        "description": "Sway bar stiffness by track",
        "tags": ["setup", "handling"],
    },
    "/racing/setup/tyres.md": {
        "type": "doc",
        "title": "Tyre pressures",
        "description": "Cold pressures for the rear",
        "tags": ["setup"],
    },
    "/racing/history/2025.md": {"type": "doc", "title": "2025 season", "tags": ["log"]},
    "/racing/moved.md": {"type": "pointer", "moved_to": "/x.md"},
    "/racing/gone.md": {"type": "archived", "title": "Rear bar (old)"},
    "/kitchen/bread.md": {
        "type": "doc",
        "title": "Sourdough",
        "description": "Rear of the oven runs hot",
        "tags": ["baking"],
    },
    "/kitchen/private/rear-window.md": {"type": "doc", "title": "Rear window plan"},
}


@pytest.fixture
def tree(bucket: Any, settings: Settings) -> dict[str, str]:
    """Articles written directly, no listings: search rebuilds them in memory."""
    versions: dict[str, str] = {}
    for path, fm in ARTICLES.items():
        response = bucket.put_object(
            Bucket=settings.bucket, Key="a" + path, Body=serialize(fm, "body")
        )
        versions[path] = response["ETag"].strip('"')
    return versions


@pytest.fixture
def as_user(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None]
) -> Callable[..., ToolContext]:
    def _as(subject: str, *grants: tuple[str, Permission]) -> ToolContext:
        for node, permission in grants:
            seed_grant(subject, node, permission)
        return make_ctx(subject)

    return _as


def paths(result: dict[str, Any]) -> list[str]:
    return [h["path"] for h in result["hits"]]


# --- pure pieces -------------------------------------------------------------------------


class TestTokens:
    def test_lowercases_splits_and_dedupes(self) -> None:
        assert tokens("Rear-Bar rear, BAR!") == ["rear", "bar"]

    def test_punctuation_only_is_empty(self) -> None:
        assert tokens("--- ///") == []


class TestScore:
    def test_weights(self) -> None:
        child = ListingChild.article(
            "rear-bar.md",
            "e",
            1,
            {"title": "Rear bar", "description": "about rear", "tags": ["rear"]},
        )
        assert score(child, ["rear"]) == 3 + 2 + 2 + 1
        assert score(child, ["bar"]) == 3 + 2
        assert score(child, ["nothing"]) == 0

    def test_case_insensitive(self) -> None:
        child = ListingChild.article("x.md", "e", 1, {"title": "SOURDOUGH"})
        assert score(child, tokens("Sourdough")) == 3


class TestSearchableArea:
    def _grant(self, node: str) -> Grant:
        return Grant(subject="s", node=node, permission=Permission.READ)

    def test_nested_folder_grants_collapse(self) -> None:
        walk, articles = searchable_area(
            [self._grant("/racing/setup"), self._grant("/racing"), self._grant("/kitchen")], None
        )
        assert walk == ["/kitchen", "/racing"]
        assert articles == []

    def test_root_grant_covers_everything(self) -> None:
        walk, articles = searchable_area(
            [self._grant("/"), self._grant("/racing"), self._grant("/k/x.md")], None
        )
        assert (walk, articles) == (["/"], [])

    def test_article_grant_under_a_folder_grant_is_absorbed(self) -> None:
        walk, articles = searchable_area(
            [self._grant("/racing"), self._grant("/racing/a.md"), self._grant("/k/b.md")], None
        )
        assert (walk, articles) == (["/racing"], ["/k/b.md"])

    def test_prefix_inside_a_grant_narrows_the_walk(self) -> None:
        walk, _ = searchable_area([self._grant("/racing")], "/racing/setup")
        assert walk == ["/racing/setup"]

    def test_prefix_above_a_grant_keeps_the_grant(self) -> None:
        walk, _ = searchable_area([self._grant("/racing/setup")], "/racing")
        assert walk == ["/racing/setup"]

    def test_prefix_disjoint_from_every_grant_is_empty(self) -> None:
        walk, articles = searchable_area(
            [self._grant("/racing"), self._grant("/k/b.md")], "/kitchen"
        )
        assert (walk, articles) == ([], [])

    def test_prefix_filters_article_grants(self) -> None:
        _, articles = searchable_area([self._grant("/k/b.md"), self._grant("/r/c.md")], "/k")
        assert articles == ["/k/b.md"]


# --- the tool ----------------------------------------------------------------------------


def test_folder_grant_finds_nested_articles_under_one_list_credential(
    as_user: Callable[..., ToolContext], minter: FakeMinter, tree: dict[str, str]
) -> None:
    user = as_user("user_r", ("/racing", Permission.READ))
    result = call(TOOL, user, query="rear")
    assert paths(result) == ["/racing/setup/rear-bar.md", "/racing/setup/tyres.md"]
    assert result["truncated"] is False
    assert minter.calls == [("user_r", Shape.LIST, "/racing")]

    hit = result["hits"][0]
    assert hit["version"] == tree["/racing/setup/rear-bar.md"]
    assert hit["title"] == "Rear bar settings"
    assert hit["snippet"] == "Sway bar stiffness by track"
    assert hit["score"] == 3 + 2  # title and name; the description says "sway bar"
    assert hit["trust"] == "unverified"
    assert hit["tags"] == ["setup", "handling"]
    assert result["hits"][1]["score"] == 1  # description only


def test_pointers_and_tombstones_never_match(
    as_user: Callable[..., ToolContext], tree: dict[str, str]
) -> None:
    user = as_user("user_r", ("/racing", Permission.READ))
    assert "/racing/gone.md" not in paths(call(TOOL, user, query="old"))
    assert "/racing/moved.md" not in paths(call(TOOL, user, query="moved"))


def test_article_grant_finds_only_that_article_under_a_read_credential(
    as_user: Callable[..., ToolContext], minter: FakeMinter, tree: dict[str, str]
) -> None:
    user = as_user("user_a", ("/kitchen/bread.md", Permission.READ))
    result = call(TOOL, user, query="rear")
    assert paths(result) == ["/kitchen/bread.md"]
    assert result["hits"][0]["snippet"] == "Rear of the oven runs hot"
    assert minter.calls == [("user_a", Shape.READ, "/kitchen/bread.md")]
    assert paths(call(TOOL, user, query="sourdough")) == ["/kitchen/bread.md"]
    assert paths(call(TOOL, user, query="tyre")) == []


def test_article_grant_on_a_tombstone_finds_nothing(
    as_user: Callable[..., ToolContext], tree: dict[str, str]
) -> None:
    user = as_user("user_a", ("/racing/gone.md", Permission.READ))
    assert paths(call(TOOL, user, query="rear")) == []


def test_zero_grants_is_empty_and_mints_nothing(
    nobody: ToolContext, minter: FakeMinter, tree: dict[str, str]
) -> None:
    assert call(TOOL, nobody, query="rear") == {"hits": [], "truncated": False}
    assert minter.mint_count == 0


def test_nothing_is_read_outside_the_grant_set(
    as_user: Callable[..., ToolContext], minter: FakeMinter, tree: dict[str, str]
) -> None:
    user = as_user(
        "user_m",
        ("/racing/setup", Permission.READ),
        ("/kitchen/bread.md", Permission.READ),
    )
    result = call(TOOL, user, query="rear")
    assert paths(result) == [
        "/racing/setup/rear-bar.md",
        "/kitchen/bread.md",
        "/racing/setup/tyres.md",
    ]
    assert "/kitchen/private/rear-window.md" not in paths(result)
    assert "/racing/history/2025.md" not in paths(result)
    assert sorted(minter.calls) == [
        ("user_m", Shape.LIST, "/racing/setup"),
        ("user_m", Shape.READ, "/kitchen/bread.md"),
    ]


def test_prefix_narrows(
    as_user: Callable[..., ToolContext], minter: FakeMinter, tree: dict[str, str]
) -> None:
    user = as_user("user_r", ("/", Permission.READ))
    everything = paths(call(TOOL, user, query="rear"))
    assert everything == [
        "/kitchen/private/rear-window.md",
        "/racing/setup/rear-bar.md",
        "/kitchen/bread.md",
        "/racing/setup/tyres.md",
    ]
    minter.calls.clear()
    narrowed = call(TOOL, user, query="rear", prefix="/racing")
    assert paths(narrowed) == ["/racing/setup/rear-bar.md", "/racing/setup/tyres.md"]
    assert minter.calls == [("user_r", Shape.LIST, "/racing")]


def test_prefix_never_widens(
    as_user: Callable[..., ToolContext], minter: FakeMinter, tree: dict[str, str]
) -> None:
    user = as_user("user_r", ("/racing/setup", Permission.READ))
    assert paths(call(TOOL, user, query="rear", prefix="/racing")) == [
        "/racing/setup/rear-bar.md",
        "/racing/setup/tyres.md",
    ]
    assert paths(call(TOOL, user, query="rear", prefix="/kitchen")) == []
    assert paths(call(TOOL, user, query="rear", prefix="/racing/history")) == []
    assert all(path == "/racing/setup" for _s, _shape, path in minter.calls)


def test_scoring_order_then_path(as_user: Callable[..., ToolContext], tree: dict[str, str]) -> None:
    user = as_user("user_r", ("/", Permission.READ))
    result = call(TOOL, user, query="rear bar setup")
    hits = result["hits"]
    assert hits[0]["path"] == "/racing/setup/rear-bar.md"
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    # Ties break on path.
    tied = [h["path"] for h in hits if h["score"] == hits[-1]["score"]]
    assert tied == sorted(tied)


def test_limit_and_truncated(as_user: Callable[..., ToolContext], tree: dict[str, str]) -> None:
    user = as_user("user_r", ("/", Permission.READ))
    result = call(TOOL, user, query="rear", limit=2)
    assert len(result["hits"]) == 2
    assert result["truncated"] is True
    result = call(TOOL, user, query="rear", limit=50)
    assert len(result["hits"]) == 4
    assert result["truncated"] is False
    assert DEFAULT_LIMIT == 10


def test_no_match_is_empty_not_error(
    as_user: Callable[..., ToolContext], tree: dict[str, str]
) -> None:
    user = as_user("user_r", ("/", Permission.READ))
    assert call(TOOL, user, query="zzzz-no-such-word") == {"hits": [], "truncated": False}
    assert call(TOOL, user, query="---") == {"hits": [], "truncated": False}


def test_searches_persisted_listings_too(
    ctx: ToolContext, as_user: Callable[..., ToolContext], tree: dict[str, str]
) -> None:
    """The owner's list_folder persists listings; a later search reads them."""
    from app.mcp.tools.list_folder import TOOL as LIST

    call(LIST, ctx, path="/")
    call(LIST, ctx, path="/racing")
    call(LIST, ctx, path="/racing/setup")
    user = as_user("user_r", ("/racing", Permission.READ))
    assert paths(call(TOOL, user, query="rear")) == [
        "/racing/setup/rear-bar.md",
        "/racing/setup/tyres.md",
    ]


def test_search_never_persists_a_listing(
    as_user: Callable[..., ToolContext], tree: dict[str, str], bucket: Any, settings: Settings
) -> None:
    user = as_user("user_w", ("/", Permission.OWN))
    call(TOOL, user, query="rear")
    listed = bucket.list_objects_v2(Bucket=settings.bucket, Prefix="a/")
    assert not any(o["Key"].endswith("_listing.json") for o in listed.get("Contents", []))


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 7},
        {"query": "x" * 401},
        {"query": "x", "prefix": "/racing/"},
        {"query": "x", "prefix": "racing"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 51},
        {"query": "x", "limit": "5"},
        {"query": "x", "limit": True},
    ],
)
def test_bad_arguments_are_400_and_mint_nothing(
    ctx: ToolContext, minter: FakeMinter, args: dict[str, Any]
) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", **args)
    assert minter.mint_count == 0


def test_s3_access_denied_is_empty(denied_ctx: ToolContext) -> None:
    assert call(TOOL, denied_ctx, query="rear") == {"hits": [], "truncated": False}
