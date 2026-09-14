"""``list_versions`` (HANDOFF §5.3, §8.3, §10.7)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_READ
from app.mcp.protocol import ToolContext
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.list_versions import ACTOR_UNKNOWN, PREFIX_BYTES, TOOL
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import META_ACTOR, META_KIND, META_MOVED_FROM, key_for
from app.storage.markdown import MAX_FRONTMATTER_BYTES, parse, serialize
from tests.unit.tools.conftest import OWNER, DenyingS3, FakeMinter, call, expect_error

PATH = "/racing/setup/rear-bar.md"
OLD = "/racing/old-name.md"
NEW = "/racing/new-name.md"


class DenyingVersions(DenyingS3):
    """``DenyingS3`` plus the one verb this tool adds."""

    def list_object_versions(self, **_: Any) -> Any:
        raise self._deny("ListObjectVersions")


class RecordingS3:
    """Wraps a client and records every ``(operation, kwargs)`` it is asked for."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(**kwargs: Any) -> Any:
            self.calls.append((name, dict(kwargs)))
            return attr(**kwargs)

        return wrapped

    def keys_touched(self) -> set[str]:
        touched: set[str] = set()
        for _, kwargs in self.calls:
            for field in ("Key", "Prefix"):
                if field in kwargs:
                    touched.add(kwargs[field])
        return touched


def _three_versions(put_raw: Callable[..., str], path: str = PATH) -> None:
    put_raw(path, {"type": "doc", "seq": 1}, "one", **{META_ACTOR: "human:a", META_KIND: "write"})
    put_raw(path, {"type": "doc", "seq": 2}, "two", **{META_ACTOR: "human:b", META_KIND: "write"})
    put_raw(path, {"type": "doc", "seq": 3}, "three", **{META_ACTOR: "human:c", META_KIND: "write"})


def _simulate_move(put_raw: Callable[..., str]) -> None:
    """What ``move_article`` leaves behind (§5.3, §8.3), written directly."""
    put_raw(OLD, {"type": "doc", "seq": 1}, "first", **{META_ACTOR: "human:a", META_KIND: "write"})
    put_raw(OLD, {"type": "doc", "seq": 2}, "second", **{META_ACTOR: "human:b", META_KIND: "write"})
    put_raw(
        OLD,
        {"type": "pointer", "moved_to": NEW, "moved_at": "2026-03-04T05:06:07Z", "seq": 3},
        "",
        **{META_ACTOR: "human:mover", META_KIND: "moved_out"},
    )
    put_raw(
        NEW,
        {"type": "doc", "seq": 3, "moved_from": OLD},
        "second",
        **{META_ACTOR: "human:mover", META_KIND: "moved_in", META_MOVED_FROM: OLD},
    )


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "list_versions"
    assert TOOL.scope == SCOPE_READ
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["path"]
    assert descriptor["inputSchema"]["properties"]["limit"]["default"] == 20
    assert descriptor["outputSchema"]["required"] == ["path", "versions", "truncated"]
    entry = descriptor["outputSchema"]["properties"]["versions"]["items"]
    assert entry["required"] == ["version_id", "seq", "kind", "actor", "at"]
    assert TOOL.description.startswith("The version chain of one path, newest first")


# --- the chain ----------------------------------------------------------------------


def test_three_updates_newest_first_with_seq_and_actor_from_metadata(
    ctx: ToolContext, put_raw: Callable[..., str], minter: FakeMinter
) -> None:
    _three_versions(put_raw)
    result = call(TOOL, ctx, path=PATH)
    assert result["path"] == PATH
    assert result["truncated"] is False
    assert "cursor" not in result
    assert "continues_at" not in result
    versions = result["versions"]
    assert [v["seq"] for v in versions] == [3, 2, 1]
    assert [v["actor"] for v in versions] == ["human:c", "human:b", "human:a"]
    assert [v["kind"] for v in versions] == ["write", "write", "write"]
    assert len({v["version_id"] for v in versions}) == 3
    for version in versions:
        assert version["at"].startswith("20")
        assert version["size_bytes"] > 0
        assert "moved_to" not in version
        assert "moved_from" not in version
    assert minter.calls == [(OWNER, Shape.READ, PATH)]


def test_versions_written_through_the_write_tools(ctx: ToolContext) -> None:
    created = call(CREATE, ctx, path=PATH, content="v1", frontmatter={"type": "doc"})
    second = call(UPDATE, ctx, path=PATH, content="v2", if_version=created["version"])
    call(UPDATE, ctx, path=PATH, content="v3", if_version=second["version"])
    versions = call(TOOL, ctx, path=PATH)["versions"]
    assert [v["seq"] for v in versions] == [3, 2, 1]
    assert {v["actor"] for v in versions} == {f"human:{OWNER}"}
    assert {v["kind"] for v in versions} == {"write"}


def test_object_without_metadata_gets_defaults(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc"}, "no metadata, no seq")
    [version] = call(TOOL, ctx, path=PATH)["versions"]
    assert version["kind"] == "write"
    assert version["actor"] == ACTOR_UNKNOWN
    assert version["seq"] == 0


def test_archive_tombstone_is_part_of_the_chain(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(PATH, {"type": "doc", "seq": 1}, "live", **{META_ACTOR: "human:a", META_KIND: "write"})
    put_raw(
        PATH, {"type": "archived", "seq": 2}, "", **{META_ACTOR: "human:b", META_KIND: "archive"}
    )
    versions = call(TOOL, ctx, path=PATH)["versions"]
    assert [(v["kind"], v["seq"], v["actor"]) for v in versions] == [
        ("archive", 2, "human:b"),
        ("write", 1, "human:a"),
    ]


def test_frontmatter_larger_than_the_prefix_still_yields_seq(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    # The largest block ``parse`` accepts: with its fences the header overruns the
    # peek, so the tool has to read the whole version to find ``seq``.
    probe = serialize({"type": "doc", "notes": "n", "seq": 7}, "")
    notes = "n" * (MAX_FRONTMATTER_BYTES - (len(probe) - len(b"---\n---\n")) + 1)
    frontmatter = {"type": "doc", "notes": notes, "seq": 7}
    stored = serialize(frontmatter, "body")
    assert len(stored) - len(b"body") > PREFIX_BYTES
    assert parse(stored).frontmatter == frontmatter
    put_raw(PATH, frontmatter, "body", **{META_ACTOR: "human:a", META_KIND: "write"})
    [version] = call(TOOL, ctx, path=PATH)["versions"]
    assert version["seq"] == 7
    assert version["actor"] == "human:a"


def test_frontmatter_over_the_parse_cap_yields_seq_zero(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    # Storage treats a block over ``MAX_FRONTMATTER_BYTES`` as malformed; the version
    # is still listed, with the metadata it does have.
    put_raw(
        PATH,
        {"type": "doc", "notes": "n" * (PREFIX_BYTES * 2), "seq": 7},
        "body",
        **{META_ACTOR: "human:a", META_KIND: "write"},
    )
    [version] = call(TOOL, ctx, path=PATH)["versions"]
    assert version["seq"] == 0
    assert version["actor"] == "human:a"


def test_missing_path_is_404(ctx: ToolContext) -> None:
    expect_error(TOOL, ctx, 404, "not_found", path=PATH)


# --- pagination ---------------------------------------------------------------------


def test_limit_one_pages_through_the_chain(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    _three_versions(put_raw)
    first = call(TOOL, ctx, path=PATH, limit=1)
    assert [v["seq"] for v in first["versions"]] == [3]
    assert first["truncated"] is True
    assert isinstance(first["cursor"], str)

    second = call(TOOL, ctx, path=PATH, limit=1, cursor=first["cursor"])
    assert [v["seq"] for v in second["versions"]] == [2]
    assert second["truncated"] is True

    third = call(TOOL, ctx, path=PATH, limit=1, cursor=second["cursor"])
    assert [v["seq"] for v in third["versions"]] == [1]
    assert third["truncated"] is False
    assert "cursor" not in third


def test_cursor_is_opaque_base64_of_markers(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    _three_versions(put_raw)
    first = call(TOOL, ctx, path=PATH, limit=2)
    cursor = first["cursor"]
    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert decoded == {"k": key_for(PATH), "v": first["versions"][-1]["version_id"]}


def test_cursor_from_another_path_is_400(ctx: ToolContext, put_raw: Callable[..., str]) -> None:
    _three_versions(put_raw)
    _three_versions(put_raw, "/racing/other.md")
    cursor = call(TOOL, ctx, path=PATH, limit=1)["cursor"]
    expect_error(TOOL, ctx, 400, "bad_request", path="/racing/other.md", cursor=cursor)


@pytest.mark.parametrize("cursor", ["", "garbage!", "e30", 5, ["x"]])
def test_bad_cursor_is_400(ctx: ToolContext, put_raw: Callable[..., str], cursor: Any) -> None:
    _three_versions(put_raw)
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, cursor=cursor)


@pytest.mark.parametrize("limit", [0, 101, -1, "5", True, 2.5])
def test_bad_limit_is_400(ctx: ToolContext, minter: FakeMinter, limit: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=PATH, limit=limit)
    assert minter.mint_count == 0


# --- moves: history is per path (§5.3) ------------------------------------------------


def test_destination_chain_names_continues_at_and_never_follows_it(
    ctx: ToolContext,
    make_ctx: Callable[..., ToolContext],
    bucket: Any,
    put_raw: Callable[..., str],
) -> None:
    _simulate_move(put_raw)
    recording = RecordingS3(bucket)
    result = call(TOOL, make_ctx(OWNER, minter_override=FakeMinter(recording)), path=NEW)

    [entry] = result["versions"]
    assert entry["kind"] == "moved_in"
    assert entry["moved_from"] == OLD
    assert entry["seq"] == 3
    assert entry["actor"] == "human:mover"
    assert "moved_to" not in entry
    assert result["continues_at"] == OLD
    assert result["truncated"] is False

    # The old path's key was never touched: not listed, not read, not headed.
    assert recording.keys_touched() == {key_for(NEW)}
    assert all(kwargs.get("Prefix", key_for(NEW)) == key_for(NEW) for _, kwargs in recording.calls)


def test_continues_at_only_on_the_page_holding_the_oldest_version(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    _simulate_move(put_raw)
    put_raw(NEW, {"type": "doc", "seq": 4}, "edited", **{META_ACTOR: "human:e", META_KIND: "write"})

    first = call(TOOL, ctx, path=NEW, limit=1)
    assert [v["kind"] for v in first["versions"]] == ["write"]
    assert first["truncated"] is True
    assert "continues_at" not in first

    last = call(TOOL, ctx, path=NEW, limit=1, cursor=first["cursor"])
    assert [v["kind"] for v in last["versions"]] == ["moved_in"]
    assert last["truncated"] is False
    assert last["continues_at"] == OLD


def test_vacated_path_keeps_its_history_under_the_pointer(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    _simulate_move(put_raw)
    result = call(TOOL, ctx, path=OLD)
    versions = result["versions"]
    assert [v["kind"] for v in versions] == ["moved_out", "write", "write"]
    assert [v["seq"] for v in versions] == [3, 2, 1]
    assert versions[0]["moved_to"] == NEW
    assert versions[0]["actor"] == "human:mover"
    assert "moved_from" not in versions[0]
    assert "continues_at" not in result


def test_pointer_without_kind_metadata_still_reports_moved_to(
    ctx: ToolContext, put_raw: Callable[..., str]
) -> None:
    put_raw(OLD, {"type": "pointer", "moved_to": NEW, "seq": 2})
    [entry] = call(TOOL, ctx, path=OLD)["versions"]
    assert entry["moved_to"] == NEW
    assert entry["kind"] == "write"  # metadata is authoritative for kind; absent means default


def test_reader_of_destination_only_cannot_reach_the_old_chain(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
) -> None:
    # Whoever has access at the new location gets the present and none of the past.
    _simulate_move(put_raw)
    seed_grant("user_new", NEW, Permission.READ)
    reader = make_ctx("user_new")
    result = call(TOOL, reader, path=NEW)
    assert result["continues_at"] == OLD
    expect_error(TOOL, reader, 404, "not_found", path=OLD)


# --- authorization --------------------------------------------------------------------


def test_zero_grant_subject_is_404_and_nothing_minted(
    nobody: ToolContext, minter: FakeMinter, put_raw: Callable[..., str]
) -> None:
    _three_versions(put_raw)
    expect_error(TOOL, nobody, 404, "not_found", path=PATH)
    expect_error(TOOL, nobody, 404, "not_found", path="/racing/does-not-exist.md")
    assert minter.mint_count == 0


def test_grant_elsewhere_is_404_and_nothing_minted(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    put_raw: Callable[..., str],
) -> None:
    _three_versions(put_raw)
    seed_grant("user_other", "/other", Permission.OWN)
    expect_error(TOOL, make_ctx("user_other"), 404, "not_found", path=PATH)
    assert minter.mint_count == 0


def test_article_read_grant_lists_that_article(
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
) -> None:
    _three_versions(put_raw)
    seed_grant("user_one", PATH, Permission.READ)
    assert len(call(TOOL, make_ctx("user_one"), path=PATH)["versions"]) == 3


def test_s3_access_denied_is_404(ctx: ToolContext, make_ctx: Callable[..., ToolContext]) -> None:
    # ``ctx`` seeds the grant; the client then refuses, as a credential minted for
    # another prefix would.
    denied = make_ctx(OWNER, minter_override=FakeMinter(DenyingVersions()))
    expect_error(TOOL, denied, 404, "not_found", path=PATH)


@pytest.mark.parametrize("path", ["/Racing/x.md", "/x", "/_x.md", None])
def test_bad_path_is_400(ctx: ToolContext, minter: FakeMinter, path: Any) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", path=path)
    assert minter.mint_count == 0
