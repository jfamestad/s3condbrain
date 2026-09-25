"""``edit_article`` — patch edits through ``update_article``'s write path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_WRITE
from app.mcp.protocol import ToolContext
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.edit_article import TOOL
from app.mcp.tools.list_folder import TOOL as LIST
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import ArticleStore
from app.storage.markdown import parse
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "status": "draft"}
BODY = (
    "# Rear bar\n"
    "\n"
    "Intro text.\n"
    "\n"
    "## Settings\n"
    "\n"
    "- stiffness: 3\n"
    "\n"
    "```\n"
    "## Not a heading\n"
    "```\n"
    "\n"
    "### Detail\n"
    "\n"
    "detail text\n"
    "\n"
    "## History\n"
    "\n"
    "- v1\n"
)


@pytest.fixture
def existing(ctx: ToolContext) -> dict[str, Any]:
    return call(CREATE, ctx, path=PATH, content=BODY, frontmatter=FM)


def _body(get_raw: Callable[..., Any]) -> str:
    return parse(get_raw(PATH)).body


# --- 12. descriptor --------------------------------------------------------------


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "edit_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path", "if_version"]
    assert TOOL.description.startswith("Edit part of an article")
    assert "single call" in TOOL.description


def test_registered_after_update_article() -> None:
    from app.mcp.tools import registry

    names = list(registry())
    assert names.index("edit_article") == names.index("update_article") + 1


# --- 1. a single replace ---------------------------------------------------------


def test_single_replace(
    ctx: ToolContext,
    existing: dict[str, Any],
    minter: FakeMinter,
    get_raw: Callable[..., Any],
) -> None:
    result = call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "stiffness: 3", "new": "stiffness: 4"}],
    )
    expected = BODY.replace("stiffness: 3", "stiffness: 4")
    assert set(result) == {"path", "version", "seq", "total_bytes"}
    assert result["path"] == PATH
    assert result["seq"] == 2
    assert result["total_bytes"] == len(expected.encode("utf-8"))
    assert result["version"] != existing["version"]
    stored = parse(get_raw(PATH))
    assert stored.body == expected
    assert stored.frontmatter == {**FM, "seq": 2}
    # Same credential sequence as update_article: the write, then the listing refresh.
    assert minter.calls[-2:] == [
        (OWNER, Shape.WRITE, PATH),
        (OWNER, Shape.MAINTAIN, "/racing/setup"),
    ]
    listed = call(LIST, ctx, path="/racing/setup")
    entry = next(c for c in listed["articles"] if c["path"] == PATH)
    assert entry["version"] == result["version"]


# --- 2. edits apply in order -----------------------------------------------------


def test_edits_apply_in_order(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        edits=[
            {"old": "Intro text.", "new": "Intro text, revised."},
            {"old": "text, revised.", "new": "text, revised twice."},
        ],
    )
    assert "Intro text, revised twice.\n" in _body(get_raw)


# --- 3. not found writes nothing -------------------------------------------------


def test_old_not_found_is_400_naming_index_and_writes_nothing(
    ctx: ToolContext, existing: dict[str, Any], head_raw: Callable[..., Any]
) -> None:
    before = head_raw(PATH)
    error = expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "Intro", "new": "Outro"}, {"old": "absent", "new": "x"}],
    )
    assert "edits[1].old not found" in error.message
    assert head_raw(PATH)["VersionId"] == before["VersionId"]


# --- 4. ambiguity and replace_all ------------------------------------------------


def test_ambiguous_old_is_400(ctx: ToolContext, existing: dict[str, Any]) -> None:
    error = expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "text", "new": "words"}],
    )
    assert f"edits[0].old matches {BODY.count('text')} times" in error.message
    assert "replace_all" in error.message


def test_replace_all(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "text", "new": "words", "replace_all": True}],
    )
    assert _body(get_raw) == BODY.replace("text", "words")


# --- 5. append at the end ---------------------------------------------------------


def test_append_at_end(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(TOOL, ctx, path=PATH, if_version=existing["version"], edits=[{"append": "- v2\n"}])
    assert _body(get_raw) == BODY + "- v2\n"


def test_append_at_end_without_trailing_newline(
    ctx: ToolContext, get_raw: Callable[..., Any]
) -> None:
    created = call(CREATE, ctx, path=PATH, content="line one", frontmatter=FM)
    call(TOOL, ctx, path=PATH, if_version=created["version"], edits=[{"append": "line two\n"}])
    assert _body(get_raw) == "line one\nline two\n"


# --- 6. append to a section -------------------------------------------------------


def test_append_to_section_lands_before_next_same_level_heading(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "more detail\n", "section": "settings"}],
    )
    # The fenced "## Not a heading" does not end the section, and neither does the
    # deeper "### Detail": the text lands at the end of "Detail", before "## History".
    assert _body(get_raw) == BODY.replace(
        "detail text\n\n## History", "detail text\nmore detail\n\n## History"
    )


def test_append_to_last_section(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "- v2", "section": "History"}],
    )
    assert _body(get_raw) == BODY + "- v2"


def test_append_to_unknown_section_is_400(
    ctx: ToolContext, existing: dict[str, Any], head_raw: Callable[..., Any]
) -> None:
    before = head_raw(PATH)
    error = expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "x\n", "section": "Not a heading"}],
    )
    assert "edits[0]" in error.message
    assert "Not a heading" in error.message
    assert head_raw(PATH)["VersionId"] == before["VersionId"]


# --- 7. frontmatter merge ---------------------------------------------------------


def test_frontmatter_merge_sets_and_removes(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    result = call(
        TOOL,
        ctx,
        path=PATH,
        if_version=existing["version"],
        frontmatter={"status": "stable", "tags": None, "seq": 999},
    )
    assert result["seq"] == 2
    stored = parse(get_raw(PATH))
    assert stored.frontmatter == {"type": "doc", "title": "Rear bar", "status": "stable", "seq": 2}
    assert stored.body == BODY


@pytest.mark.parametrize("article_type", ["pointer", "archived"])
def test_frontmatter_type_cannot_become_reserved(
    ctx: ToolContext, existing: dict[str, Any], article_type: str
) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        frontmatter={"type": article_type},
    )


def test_frontmatter_link_to_on_doc_is_400(
    ctx: ToolContext, existing: dict[str, Any], head_raw: Callable[..., Any]
) -> None:
    before = head_raw(PATH)
    error = expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        frontmatter={"link_to": "/racing"},
    )
    assert "link_to" in error.message
    assert head_raw(PATH)["VersionId"] == before["VersionId"]


def test_frontmatter_removing_type_is_400(ctx: ToolContext, existing: dict[str, Any]) -> None:
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        frontmatter={"type": None},
    )


# --- 8, 9. stale if_version is lean ---------------------------------------------


def test_stale_version_edits_still_apply(
    ctx: ToolContext, existing: dict[str, Any], get_raw: Callable[..., Any]
) -> None:
    v2 = call(UPDATE, ctx, path=PATH, content=BODY + "- v2\n", if_version=existing["version"])
    edits = [{"old": "stiffness: 3", "new": "stiffness: 5"}]
    error = expect_error(
        TOOL, ctx, 409, "conflict", path=PATH, if_version=existing["version"], edits=edits
    )
    assert error.extra == {"current_version": v2["version"], "edits_apply": True}
    assert "current_body" not in error.structured()
    assert "edits_apply" in error.message
    assert parse(get_raw(PATH)).body == BODY + "- v2\n"

    retried = call(TOOL, ctx, path=PATH, if_version=error.extra["current_version"], edits=edits)
    assert retried["seq"] == 3
    assert _body(get_raw) == BODY.replace("stiffness: 3", "stiffness: 5") + "- v2\n"


def test_stale_version_edits_no_longer_apply(ctx: ToolContext, existing: dict[str, Any]) -> None:
    v2 = call(
        UPDATE,
        ctx,
        path=PATH,
        content=BODY.replace("stiffness: 3", "stiffness: 9"),
        if_version=existing["version"],
    )
    error = expect_error(
        TOOL,
        ctx,
        409,
        "conflict",
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "stiffness: 3", "new": "stiffness: 5"}],
    )
    assert error.extra == {"current_version": v2["version"], "edits_apply": False}


def test_lost_race_at_s3_is_lean_409(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_put = ArticleStore.put_if_match
    winner: dict[str, str] = {}

    def racing_put(
        self: ArticleStore, s3: Any, path: str, body: bytes, etag: str, meta: Any
    ) -> Any:
        if not winner:
            won = real_put(self, s3, path, b"---\ntype: doc\nseq: 2\n---\nwinner", etag, meta)
            winner["v"] = won.version
        return real_put(self, s3, path, body, etag, meta)

    monkeypatch.setattr(ArticleStore, "put_if_match", racing_put)
    error = expect_error(
        TOOL,
        ctx,
        409,
        "conflict",
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "Intro", "new": "Outro"}],
    )
    assert error.extra == {"current_version": winner["v"], "edits_apply": False}


# --- 10. permissions, exactly as update_article ---------------------------------


def test_zero_grant_subject_is_403_and_nothing_minted(
    ctx: ToolContext, existing: dict[str, Any], nobody: ToolContext, minter: FakeMinter
) -> None:
    minted_before = minter.mint_count
    expect_error(
        TOOL,
        nobody,
        403,
        "forbidden",
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "x"}],
    )
    assert minter.mint_count == minted_before


def test_read_grant_is_403_and_nothing_minted(
    ctx: ToolContext,
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_reader", "/racing", Permission.READ)
    minted_before = minter.mint_count
    reader = make_ctx("user_reader")
    expect_error(
        TOOL,
        reader,
        403,
        "forbidden",
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "x"}],
    )
    assert minter.mint_count == minted_before
    assert ("/racing", "read") in reader.audit.grants_used


def test_article_write_grant_suffices(
    ctx: ToolContext,
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
) -> None:
    seed_grant("user_editor", PATH, Permission.WRITE)
    result = call(
        TOOL,
        make_ctx("user_editor"),
        path=PATH,
        if_version=existing["version"],
        edits=[{"append": "x\n"}],
    )
    assert result["seq"] == 2


def test_missing_article_is_404(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, if_version="abc", edits=[{"append": "x"}])


@pytest.mark.parametrize(
    "frontmatter",
    [
        {"type": "pointer", "moved_to": "/racing/rear-bar.md", "seq": 2},
        {"type": "archived", "seq": 2},
    ],
)
def test_pointer_or_tombstone_is_404(
    ctx: ToolContext, put_raw: Callable[..., str], frontmatter: dict[str, Any]
) -> None:
    version = put_raw(PATH, frontmatter, "")
    expect_error(
        TOOL, ctx, 404, "not_found", path=PATH, if_version=version, edits=[{"append": "x"}]
    )


# --- 11. links ----------------------------------------------------------------------


def test_link_retarget_to_invalid_target_is_400(ctx: ToolContext) -> None:
    link = "/me/racing.md"
    created = call(
        CREATE, ctx, path=link, content="", frontmatter={"type": "link", "link_to": "/racing"}
    )
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=link,
        if_version=created["version"],
        frontmatter={"link_to": "racing"},
    )
    ok = call(TOOL, ctx, path=link, if_version=created["version"], frontmatter={"link_to": "/f"})
    assert ok["seq"] == 2


# --- argument validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"edits": []},
        {"edits": [{}]},
        {"edits": [{"old": "", "new": "x"}]},
        {"edits": [{"old": "a"}]},
        {"edits": [{"old": "a", "new": "b", "append": "c"}]},
        {"edits": [{"append": "a", "replace_all": True}]},
        {"edits": [{"old": "a", "new": "b", "section": "s"}]},
        {"edits": [{"append": ""}]},
        {"edits": [{"append": "a"}] * 51},
        {"edits": "nope"},
        {"frontmatter": "nope"},
    ],
)
def test_bad_arguments_are_400_and_nothing_minted(
    ctx: ToolContext, minter: FakeMinter, extra: dict[str, Any]
) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, if_version="v", **extra)
    assert minter.mint_count == 0


def test_object_over_limit_is_400(
    ctx: ToolContext, existing: dict[str, Any], head_raw: Callable[..., Any]
) -> None:
    before = head_raw(PATH)
    expect_error(
        TOOL,
        ctx,
        400,
        "bad_request",
        path=PATH,
        if_version=existing["version"],
        edits=[{"old": "Intro", "new": "x" * 600_000}, {"append": "y" * 600_000}],
    )
    assert head_raw(PATH)["VersionId"] == before["VersionId"]
