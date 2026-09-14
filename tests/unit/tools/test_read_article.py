"""``read_article`` (HANDOFF §10.5)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.mcp.protocol import ToolContext
from app.mcp.tools.read_article import TOOL, extract_section
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
BODY = (
    "# Rear bar\n"
    "\n"
    "Intro paragraph.\n"
    "\n"
    "## Setup\n"
    "\n"
    "Setup text.\n"
    "\n"
    "### Detail\n"
    "\n"
    "```\n"
    "# not a heading\n"
    "## also not\n"
    "```\n"
    "\n"
    "## Notes\n"
    "\n"
    "Notes text.\n"
)


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "read_article"
    assert TOOL.scope == SCOPE_READ
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert "oneOf" in descriptor["outputSchema"]
    assert TOOL.description.startswith("Current live version of one article")


def test_read_returns_frontmatter_body_version_and_sizes(
    ctx: ToolContext, put_raw: Callable[..., str], minter: FakeMinter
) -> None:
    fm = {"type": "doc", "title": "Rear bar", "unknown_key": "kept", "seq": 2}
    version = put_raw(PATH, fm, BODY)
    result = call(TOOL, ctx, path=PATH)
    assert result == {
        "path": PATH,
        "frontmatter": fm,
        "content": BODY,
        "version": version,
        "trust": "unverified",
        "total_bytes": len(BODY.encode()),
        "returned_bytes": len(BODY.encode()),
        "truncated": False,
    }
    assert minter.calls == [(OWNER, Shape.READ, PATH)]


def test_missing_is_404(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


def test_pointer_body_is_forward_reference(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(
        PATH,
        {
            "type": "pointer",
            "moved_to": "/racing/rear-bar.md",
            "moved_at": "2026-02-03T04:05:06Z",
            "seq": 5,
        },
    )
    result = call(TOOL, ctx, path=PATH)
    assert result["kind"] == "forward_reference"
    assert result["moved_to"] == "/racing/rear-bar.md"
    assert result["moved_at"] == "2026-02-03T04:05:06Z"
    assert "moved" in result["note"]
    assert "content" not in result


def test_pointer_without_moved_at(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "pointer", "moved_to": "https://other.example/wiki/a/x.md"})
    result = call(TOOL, ctx, path=PATH)
    assert result["moved_to"] == "https://other.example/wiki/a/x.md"
    assert "moved_at" not in result


def test_archived_body_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "archived", "seq": 3}, "old body")
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


@pytest.mark.parametrize(
    ("verified", "trust"),
    [
        (None, "unverified"),
        ([], "unverified"),
        ([{"by": "claude/4.5", "at": "2026-01-01T00:00:00Z"}], "machine-confirmed"),
        ([{"by": "process:nightly"}], "machine-confirmed"),
        ([{"by": "claude/4.5"}, {"by": "human:alice"}], "human-reviewed"),
        ("garbage", "unverified"),
    ],
)
def test_trust_is_derived(
    ctx: ToolContext, put_raw: Callable[..., str], verified: Any, trust: str
) -> None:
    fm: dict[str, Any] = {"type": "doc"}
    if verified is not None:
        fm["verified"] = verified
    put_raw(PATH, fm, "x")
    assert call(TOOL, ctx, path=PATH)["trust"] == trust


# --- section -----------------------------------------------------------------------


def test_section_extracts_through_next_same_level_heading(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    result = call(TOOL, ctx, path=PATH, section="setup")
    expected = "## Setup\n\nSetup text.\n\n### Detail\n\n```\n# not a heading\n## also not\n```\n\n"
    assert result["content"] == expected
    assert result["returned_bytes"] == len(expected.encode())
    assert result["total_bytes"] == len(BODY.encode())
    assert result["truncated"] is True


def test_section_at_end_runs_to_eof(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    assert call(TOOL, ctx, path=PATH, section="NOTES")["content"] == "## Notes\n\nNotes text.\n"


def test_section_top_level_heading_returns_everything(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    result = call(TOOL, ctx, path=PATH, section="Rear Bar")
    assert result["content"] == BODY
    assert result["truncated"] is False


def test_section_ignores_headings_inside_fences(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    expect_error(TOOL, ctx, 404, "not_found", path=PATH, section="not a heading")


def test_section_missing_is_404(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    error = expect_error(TOOL, ctx, 404, "not_found", path=PATH, section="Nope")
    assert "Nope" in error.message


def test_section_takes_precedence_over_byte_range(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, BODY)
    result = call(TOOL, ctx, path=PATH, section="Notes", byte_range=[0, 3])
    assert result["content"] == "## Notes\n\nNotes text.\n"


@pytest.mark.parametrize("section", ["", "   ", 5, "x" * 201])
def test_bad_section_is_400(ctx: ToolContext, section: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, section=section)


def test_extract_section_closing_hashes_and_deeper_heading_included() -> None:
    body = "## A ##\n\ntext\n\n### A.1\n\nmore\n\n# Top\n\nend\n"
    assert extract_section(body, "a") == "## A ##\n\ntext\n\n### A.1\n\nmore\n\n"
    assert extract_section(body, "A.1") == "### A.1\n\nmore\n\n"
    assert extract_section(body, "top") == "# Top\n\nend\n"
    assert extract_section(body, "missing") is None


# --- byte_range ---------------------------------------------------------------------


def test_byte_range_is_inclusive_and_truncated(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "0123456789")
    result = call(TOOL, ctx, path=PATH, byte_range=[2, 5])
    assert result["content"] == "2345"
    assert result["returned_bytes"] == 4
    assert result["total_bytes"] == 10
    assert result["truncated"] is True


def test_byte_range_clamps_to_body(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "doc"}, "0123456789")
    result = call(TOOL, ctx, path=PATH, byte_range=[7, 500])
    assert result["content"] == "789"
    assert result["returned_bytes"] == 3
    assert result["truncated"] is True


def test_byte_range_covering_whole_body_is_not_truncated(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "0123456789")
    result = call(TOOL, ctx, path=PATH, byte_range=[0, 9])
    assert result["content"] == "0123456789"
    assert result["truncated"] is False


def test_byte_range_past_end_is_empty(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "doc"}, "0123456789")
    result = call(TOOL, ctx, path=PATH, byte_range=[50, 60])
    assert result["content"] == ""
    assert result["returned_bytes"] == 0
    assert result["truncated"] is True


def test_byte_range_splitting_a_multibyte_char_decodes_with_replacement(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "a☕b")  # ☕ is 3 bytes
    result = call(TOOL, ctx, path=PATH, byte_range=[0, 1])
    assert result["returned_bytes"] == 2
    assert result["content"].startswith("a")
    assert "�" in result["content"]


@pytest.mark.parametrize(
    "byte_range", [[5, 2], [-1, 3], [0], [0, 1, 2], ["0", "5"], [True, 5], "0-5", [0.5, 2]]
)
def test_bad_byte_range_is_400(ctx: ToolContext, byte_range: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, byte_range=byte_range)


# --- authorization --------------------------------------------------------------------


def test_zero_grant_subject_is_404_and_nothing_minted(
    nobody: ToolContext, minter: FakeMinter, put_raw: Callable[..., str]
) -> None:
    # A path you may not see is indistinguishable from none (§10.1): the answer is
    # the same whether or not the article exists, and no credential is minted.
    put_raw(PATH, {"type": "doc"}, "secret")
    expect_error(TOOL, nobody, 404, "not_found", path=PATH)
    expect_error(TOOL, nobody, 404, "not_found", path="/racing/does-not-exist.md")
    assert minter.mint_count == 0


def test_grant_elsewhere_is_404_and_nothing_minted(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    put_raw: Callable[..., str],
) -> None:
    put_raw(PATH, {"type": "doc"}, "secret")
    seed_grant("user_other", "/other", Permission.OWN)
    expect_error(TOOL, make_ctx("user_other"), 404, "not_found", path=PATH)
    assert minter.mint_count == 0


def test_article_grant_reads_that_article(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
) -> None:
    put_raw(PATH, {"type": "doc"}, "just this one")
    seed_grant("user_one", PATH, Permission.READ)
    assert call(TOOL, make_ctx("user_one"), path=PATH)["content"] == "just this one"


def test_s3_access_denied_is_404(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 404, "not_found", path=PATH)


@pytest.mark.parametrize("path", ["/Racing/x.md", "/x", "/_x.md", None])
def test_bad_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path)
    assert minter.mint_count == 0


# --- audit (AS-10) -----------------------------------------------------------------------


def test_grants_used_are_audited(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    put_raw(PATH, {"type": "doc"}, "body")
    call(TOOL, ctx, path=PATH)
    assert ("/", "own") in ctx.audit.grants_used


def test_article_grant_is_audited(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
) -> None:
    put_raw(PATH, {"type": "doc"}, "just this one")
    seed_grant("user_one", PATH, Permission.READ)
    reader = make_ctx("user_one")
    call(TOOL, reader, path=PATH)
    assert reader.audit.grants_used == [(PATH, "read")]


def test_denied_read_with_no_contributing_grant_audits_nothing(
    nobody: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "secret")
    expect_error(TOOL, nobody, 404, "not_found", path=PATH)
    assert nobody.audit.grants_used == []
