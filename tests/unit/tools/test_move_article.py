"""``move_article`` (HANDOFF §4.6, §5.3, §8.3, §10.11).

Pointer first, so every refusal leaves nothing written; the impact report is computed
against grants seeded through ``put_grant``; the half-complete recovery path is
exercised by crashing the destination write and retrying the identical call.
``ListingIndex.refresh_child`` is replaced with a recorder (see test_archive_article).

The boundary decision (§4.6, decided 14 Sep 2026): a move through which anyone gains
access is ``403 boundary_change`` on the tool surface, with the report in the envelope
and nothing written. Narrowing moves and same-folder renames proceed. ``perform`` with
``allow_widening=True`` is the web application's path and is exercised directly here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.config import SCOPE_WRITE
from app.errors import ToolError
from app.mcp.protocol import ToolContext
from app.mcp.tools.archive_article import TOOL as ARCHIVE
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.move_article import (
    HISTORY_NOTE,
    POINTER_BODY,
    REFUSAL,
    TOOL,
    access_changes,
    perform,
)
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import (
    META_ACTOR,
    META_KIND,
    META_MOVED_FROM,
    AccessDenied,
    ArticleStore,
    key_for,
)
from app.storage.listings import ListingChild, ListingIndex
from app.storage.markdown import parse
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

FROM = "/private/notes/rear-bar.md"
TO = "/public/racing/rear-bar.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"], "custom": "kept"}
BODY = "the body\n"

ListingCall = tuple[str, ListingChild | None, str]


@pytest.fixture(autouse=True)
def listing_calls(monkeypatch: pytest.MonkeyPatch) -> list[ListingCall]:
    calls: list[ListingCall] = []

    def record(self: ListingIndex, s3: Any, folder: str, child: Any, name: str) -> None:
        calls.append((folder, child, name))

    monkeypatch.setattr(ListingIndex, "refresh_child", record)
    return calls


@pytest.fixture
def existing(ctx: ToolContext, listing_calls: list[ListingCall]) -> dict[str, Any]:
    created = call(CREATE, ctx, path=FROM, content=BODY, frontmatter=FM)
    listing_calls.clear()
    return created


@pytest.fixture
def exists_raw(bucket: Any, settings: Any) -> Callable[[str], bool]:
    def _exists(path: str) -> bool:
        response = bucket.list_object_versions(Bucket=settings.bucket, Prefix=key_for(path))
        return any(v["Key"] == key_for(path) for v in response.get("Versions", []))

    return _exists


def test_descriptor_is_self_contained() -> None:
    assert TOOL.name == "move_article"
    assert TOOL.scope == SCOPE_WRITE
    descriptor = TOOL.descriptor()
    assert "$ref" not in str(descriptor)
    assert descriptor["inputSchema"]["required"] == ["from", "to", "if_version"]
    out = descriptor["outputSchema"]
    assert out["required"] == ["from", "to", "version", "seq", "access_changes"]
    assert out["properties"]["access_changes"]["items"]["required"] == [
        "subject",
        "direction",
        "permission",
    ]
    assert TOOL.description.startswith("Relocate an article, leaving a permanent forward pointer")
    assert "boundary_change" in TOOL.description and "web application" in TOOL.description
    assert "boundary_change" in descriptor["inputSchema"]["description"]


# --- the happy path, step by step ------------------------------------------------------


def test_happy_path_pointer_then_destination(
    ctx: ToolContext,
    existing: dict[str, Any],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})

    # 4. The pointer at the vacated path.
    pointer_head = head_raw(FROM)
    assert pointer_head["Metadata"] == {META_ACTOR: f"human:{OWNER}", META_KIND: "moved_out"}
    pointer = parse(get_raw(FROM))
    assert pointer.body == POINTER_BODY.format(to=TO)
    assert pointer.frontmatter["type"] == "pointer"
    assert pointer.frontmatter["moved_to"] == TO
    assert pointer.frontmatter["seq"] == 2
    assert isinstance(pointer.frontmatter["moved_at"], str)
    assert pointer.frontmatter["moved_at"].endswith("Z")

    # 5. The content at the destination: same body, seq + 1, moved_from, moved_in.
    dest_head = head_raw(TO)
    assert dest_head["ETag"].strip('"') == result["version"]
    assert dest_head["Metadata"] == {
        META_ACTOR: f"human:{OWNER}",
        META_KIND: "moved_in",
        META_MOVED_FROM: FROM,
    }
    dest = parse(get_raw(TO))
    assert dest.body == BODY
    assert dest.frontmatter == {**FM, "moved_from": FROM, "seq": 2}

    # 7. The result.
    assert result == {
        "from": FROM,
        "to": TO,
        "version": result["version"],
        "seq": 2,
        "access_changes": [],
        "history_note": HISTORY_NOTE.format(from_path=FROM),
    }
    assert result["version"] != existing["version"]

    # Credentials: a WRITE shape for each end (§8.5 "move mints one for each end").
    assert (OWNER, Shape.WRITE, TO) in minter.calls
    assert (OWNER, Shape.WRITE, FROM) in minter.calls

    # 6. Listings: the child leaves one folder and joins the other.
    assert len(listing_calls) == 2
    assert listing_calls[0] == ("/private/notes", None, "rear-bar.md")
    folder, child, name = listing_calls[1]
    assert (folder, name) == ("/public/racing", "rear-bar.md")
    assert isinstance(child, ListingChild)
    assert (child.name, child.etag, child.title, child.seq) == (
        "rear-bar.md",
        result["version"],
        "Rear bar",
        2,
    )


def test_reads_after_move(ctx: ToolContext, existing: dict[str, Any]) -> None:
    """§5.3: one hop; the old path answers with a forward reference, the new with content."""
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    forward = call(READ, ctx, path=FROM)
    assert forward["kind"] == "forward_reference"
    assert forward["moved_to"] == TO
    assert "moved_at" in forward
    moved = call(READ, ctx, path=TO)
    assert moved["content"] == BODY
    assert moved["version"] == result["version"]
    assert moved["frontmatter"]["moved_from"] == FROM


def test_destination_starts_a_fresh_chain(ctx: ToolContext, existing: dict[str, Any]) -> None:
    v2 = call(UPDATE, ctx, path=FROM, content="v2", if_version=existing["version"])
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": v2["version"]})
    assert result["seq"] == 3
    v4 = call(UPDATE, ctx, path=TO, content="v4", if_version=result["version"])
    assert v4["seq"] == 4


def test_vacated_path_is_retired(ctx: ToolContext, existing: dict[str, Any]) -> None:
    call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    expect_error(CREATE, ctx, 409, "retired_pointer", path=FROM, content="x", frontmatter=FM)


def test_quoted_if_version_is_accepted(ctx: ToolContext, existing: dict[str, Any]) -> None:
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": f'"{existing["version"]}"'})
    assert result["seq"] == 2


# --- refusals write nothing --------------------------------------------------------------


def test_stale_if_version_is_409_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    head_raw: Callable[..., Any],
    exists_raw: Callable[[str], bool],
    listing_calls: list[ListingCall],
) -> None:
    v2 = call(UPDATE, ctx, path=FROM, content="v2 body\n", if_version=existing["version"])
    listing_calls.clear()
    before = head_raw(FROM)
    error = expect_error(
        TOOL, ctx, 409, "conflict", **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    assert error.extra["current_version"] == v2["version"]
    assert error.extra["current_body"] == "v2 body\n"
    assert head_raw(FROM)["VersionId"] == before["VersionId"]
    assert not exists_raw(TO)
    assert listing_calls == []


@pytest.mark.parametrize(
    "occupant",
    [
        {"type": "doc", "title": "Already here", "seq": 1},
        {"type": "pointer", "moved_to": "/elsewhere.md", "seq": 2},
        {"type": "archived", "seq": 2},
    ],
    ids=["article", "pointer", "tombstone"],
)
def test_occupied_destination_is_409_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    put_raw: Callable[..., str],
    head_raw: Callable[..., Any],
    occupant: dict[str, Any],
    listing_calls: list[ListingCall],
) -> None:
    occupant_version = put_raw(TO, occupant, "occupant")
    before = head_raw(FROM)
    error = expect_error(
        TOOL, ctx, 409, "conflict", **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    assert "occupies the destination" in error.message
    assert head_raw(FROM)["VersionId"] == before["VersionId"]
    assert head_raw(TO)["ETag"].strip('"') == occupant_version
    assert listing_calls == []


def test_missing_source_is_404(ctx: ToolContext, exists_raw: Callable[[str], bool]) -> None:
    expect_error(TOOL, ctx, 404, "not_found", **{"from": FROM, "to": TO, "if_version": "v"})
    assert not exists_raw(TO)


def test_archived_source_is_404(ctx: ToolContext, existing: dict[str, Any]) -> None:
    tombstone = call(ARCHIVE, ctx, path=FROM, if_version=existing["version"])
    expect_error(
        TOOL, ctx, 404, "not_found", **{"from": FROM, "to": TO, "if_version": tombstone["version"]}
    )


def test_pointer_to_another_destination_is_404(
    ctx: ToolContext, put_raw: Callable[..., str], exists_raw: Callable[[str], bool]
) -> None:
    """A vacated path is retired; only the pointer's own destination may complete it."""
    version = put_raw(FROM, {"type": "pointer", "moved_to": "/somewhere/else.md", "seq": 2})
    expect_error(TOOL, ctx, 404, "not_found", **{"from": FROM, "to": TO, "if_version": version})
    assert not exists_raw(TO)


def test_same_path_is_400(ctx: ToolContext, minter: FakeMinter) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", **{"from": FROM, "to": FROM, "if_version": "v"})
    assert minter.mint_count == 0


@pytest.mark.parametrize("to", ["/public/index.md", "/public/log.md"])
def test_reserved_destination_name_is_400(ctx: ToolContext, minter: FakeMinter, to: str) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", **{"from": FROM, "to": to, "if_version": "v"})
    assert minter.mint_count == 0


@pytest.mark.parametrize(
    "args",
    [
        {"to": TO, "if_version": "v"},
        {"from": FROM, "if_version": "v"},
        {"from": FROM, "to": TO},
        {"from": "/Private/x.md", "to": TO, "if_version": "v"},
        {"from": FROM, "to": "/public/_x.md", "if_version": "v"},
        {"from": FROM, "to": "/public/x", "if_version": "v"},
        {"from": FROM, "to": TO, "if_version": ""},
        {"from": FROM, "to": TO, "if_version": 7},
    ],
)
def test_bad_arguments_are_400(ctx: ToolContext, minter: FakeMinter, args: dict[str, Any]) -> None:
    expect_error(TOOL, ctx, 400, "bad_request", **args)
    assert minter.mint_count == 0


# --- permissions: write on both ends -----------------------------------------------------


def test_zero_grant_subject_is_403_and_nothing_minted(
    existing: dict[str, Any], nobody: ToolContext, minter: FakeMinter
) -> None:
    minted_before = minter.mint_count
    expect_error(
        TOOL,
        nobody,
        403,
        "forbidden",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert minter.mint_count == minted_before


def test_write_on_source_only_is_403(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    exists_raw: Callable[[str], bool],
) -> None:
    seed_grant("user_half", "/private", Permission.WRITE)
    minted_before = minter.mint_count
    before = head_raw(FROM)
    expect_error(
        TOOL,
        make_ctx("user_half"),
        403,
        "forbidden",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert minter.mint_count == minted_before
    assert head_raw(FROM)["VersionId"] == before["VersionId"]
    assert not exists_raw(TO)


def test_write_on_destination_only_is_403(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
) -> None:
    seed_grant("user_half", "/public", Permission.WRITE)
    seed_grant("user_half", "/private", Permission.READ)
    minted_before = minter.mint_count
    expect_error(
        TOOL,
        make_ctx("user_half"),
        403,
        "forbidden",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert minter.mint_count == minted_before


def test_write_on_both_folders_suffices(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
) -> None:
    seed_grant("user_both", "/private", Permission.WRITE)
    seed_grant("user_both", "/public/racing", Permission.WRITE)
    result = call(
        TOOL, make_ctx("user_both"), **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    assert result["seq"] == 2


def test_grants_used_on_both_ends_are_audited(
    existing: dict[str, Any],
    make_ctx: Callable[..., ToolContext],
    seed_grant: Callable[..., None],
) -> None:
    # own → write for the mover is a narrowing move, so the tool surface performs it.
    seed_grant("user_both", "/private", Permission.OWN)
    seed_grant("user_both", "/public", Permission.WRITE)
    mover = make_ctx("user_both")
    call(TOOL, mover, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert ("/private", "own") in mover.audit.grants_used
    assert ("/public", "write") in mover.audit.grants_used


def test_s3_access_denied_is_403(denied_ctx: ToolContext) -> None:
    expect_error(TOOL, denied_ctx, 403, "forbidden", **{"from": FROM, "to": TO, "if_version": "v"})


def test_s3_access_denied_on_pointer_write_is_403_with_nothing_written(
    ctx: ToolContext,
    existing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    exists_raw: Callable[[str], bool],
) -> None:
    def deny(*_: Any, **__: Any) -> Any:
        raise AccessDenied(FROM)

    monkeypatch.setattr(ArticleStore, "put_if_match", deny)
    expect_error(
        TOOL, ctx, 403, "forbidden", **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    assert not exists_raw(TO)


# --- the half-complete move (§8.3) ------------------------------------------------------


def test_crash_after_pointer_then_identical_retry_completes(
    ctx: ToolContext,
    existing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    get_raw: Callable[..., Any],
    head_raw: Callable[..., Any],
    exists_raw: Callable[[str], bool],
) -> None:
    """The destination write dies after the pointer landed. The article is readable at
    neither path (the correct side to fail on); the identical call finishes the move
    from the version beneath the pointer, with the same result the first call would
    have returned."""
    real_put_new = ArticleStore.put_new

    def crash(*_: Any, **__: Any) -> Any:
        raise ConnectionError("transport failure")

    monkeypatch.setattr(ArticleStore, "put_new", crash)
    with pytest.raises(ConnectionError):
        call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})

    # Half-complete: pointer at from, nothing at to.
    assert parse(get_raw(FROM)).type == "pointer"
    assert not exists_raw(TO)
    expect_error(READ, ctx, 404, "not_found", path=TO)
    assert call(READ, ctx, path=FROM)["kind"] == "forward_reference"

    monkeypatch.setattr(ArticleStore, "put_new", real_put_new)
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert result["seq"] == 2
    assert result["access_changes"] == []

    dest = parse(get_raw(TO))
    assert dest.body == BODY
    assert dest.frontmatter == {**FM, "moved_from": FROM, "seq": 2}
    assert head_raw(TO)["Metadata"][META_KIND] == "moved_in"
    # The pointer was not rewritten: one version on top of the original content.
    versions = ctx.minter.s3(ctx.subject, Shape.READ, FROM).list_object_versions(
        Bucket=ctx.settings.bucket, Prefix=key_for(FROM)
    )["Versions"]
    assert len([v for v in versions if v["Key"] == key_for(FROM)]) == 2


def test_resume_takes_the_newest_content_beneath_the_pointer(
    ctx: ToolContext,
    existing: dict[str, Any],
    put_raw: Callable[..., str],
    get_raw: Callable[..., Any],
) -> None:
    v2 = call(UPDATE, ctx, path=FROM, content="v2 body\n", if_version=existing["version"])
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "moved_at": "2026-01-01T00:00:00Z", "seq": 3})
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": v2["version"]})
    assert result["seq"] == 3
    assert parse(get_raw(TO)).body == "v2 body\n"


def test_resume_does_not_check_if_version(
    ctx: ToolContext, existing: dict[str, Any], put_raw: Callable[..., str]
) -> None:
    """Chosen behaviour: once the pointer has committed the source to this exact move,
    nothing at ``from`` can change again, so the token has nothing left to guard.
    Any writer with write on both ends may finish it — §10.11 says the retry is safe
    and completes a half-finished move, and a second agent who finds the pointer has
    no way to learn the original token."""
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "seq": 2})
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": "not-the-original"})
    assert result["seq"] == 2


def test_resume_with_nothing_beneath_the_pointer_is_500(
    ctx: ToolContext, put_raw: Callable[..., str], exists_raw: Callable[[str], bool]
) -> None:
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "seq": 1})
    expect_error(TOOL, ctx, 500, "internal", **{"from": FROM, "to": TO, "if_version": "v"})
    assert not exists_raw(TO)


def test_destination_filled_after_pointer_is_409_and_pointer_stands(
    ctx: ToolContext,
    existing: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    put_raw: Callable[..., str],
    get_raw: Callable[..., Any],
    listing_calls: list[ListingCall],
) -> None:
    """Someone creates at ``to`` between step 1 and step 5. ``If-None-Match: *`` refuses
    our write; the pointer already names ``to``, and the response says so."""
    real_put_new = ArticleStore.put_new

    def racing_put_new(self: ArticleStore, s3: Any, path: str, body: bytes, meta: Any) -> Any:
        put_raw(TO, {"type": "doc", "title": "Sniped", "seq": 1}, "sniped")
        return real_put_new(self, s3, path, body, meta)

    monkeypatch.setattr(ArticleStore, "put_new", racing_put_new)
    error = expect_error(
        TOOL, ctx, 409, "conflict", **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    assert "half-complete" in error.message
    assert parse(get_raw(FROM)).frontmatter["moved_to"] == TO
    assert parse(get_raw(TO)).body == "sniped"
    assert listing_calls == []


def test_half_complete_is_logged(
    ctx: ToolContext, existing: dict[str, Any], monkeypatch: pytest.MonkeyPatch, put_raw: Any
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class Log:
        def info(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def warning(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

        def error(self, event: str, **fields: Any) -> None:
            events.append((event, fields))

    logged = ToolContext(
        subject=ctx.subject,
        scopes=ctx.scopes,
        grants=ctx.grants,
        minter=ctx.minter,
        settings=ctx.settings,
        request_id="req-log",
        log=Log(),
    )
    real_put_new = ArticleStore.put_new

    def racing_put_new(self: ArticleStore, s3: Any, path: str, body: bytes, meta: Any) -> Any:
        put_raw(TO, {"type": "doc", "seq": 1}, "sniped")
        return real_put_new(self, s3, path, body, meta)

    monkeypatch.setattr(ArticleStore, "put_new", racing_put_new)
    expect_error(
        TOOL, logged, 409, "conflict", **{"from": FROM, "to": TO, "if_version": existing["version"]}
    )
    names = [name for name, _ in events]
    assert "move_half_complete" in names
    fields = dict(events)["move_half_complete"]
    assert (fields["from_path"], fields["to_path"], fields["request_id"]) == (FROM, TO, "req-log")


# --- access_changes (§4.6 "a move is a boundary decision") -------------------------------


READER = "user_reader"
EDITOR = "user_editor"


def test_move_that_gives_access_is_403_boundary_change_and_writes_nothing(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    minter: FakeMinter,
    head_raw: Callable[..., Any],
    get_raw: Callable[..., Any],
    exists_raw: Callable[[str], bool],
    listing_calls: list[ListingCall],
) -> None:
    """§4.6: widening is refused on the tool surface, with the report, and nothing
    written — not the pointer, not the destination, not a listing."""
    seed_grant(READER, "/public", Permission.READ)
    before = head_raw(FROM)
    minted_before = minter.mint_count
    error = expect_error(
        TOOL,
        ctx,
        403,
        "boundary_change",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert error.message == REFUSAL.format(who="1 person")
    assert "web application" in error.message
    assert error.structured()["access_changes"] == [
        {"subject": READER, "direction": "gains", "permission": "read", "via": "/public"}
    ]
    assert head_raw(FROM)["VersionId"] == before["VersionId"]
    assert parse(get_raw(FROM)).type == "doc"
    assert not exists_raw(TO)
    assert listing_calls == []
    # The destination head and the source read each minted a credential; the
    # refusal came after the read and minted nothing more.
    assert minter.mint_count == minted_before + 2
    assert minter.calls[-2:] == [(OWNER, Shape.WRITE, TO), (OWNER, Shape.WRITE, FROM)]


def test_refusal_counts_the_people_who_would_gain(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
) -> None:
    seed_grant(READER, "/public", Permission.READ)
    seed_grant(EDITOR, "/public/racing", Permission.WRITE)
    error = expect_error(
        TOOL,
        ctx,
        403,
        "boundary_change",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert error.message.startswith("This move would give 2 people access.")
    assert [c["subject"] for c in error.extra["access_changes"]] == [EDITOR, READER]


def test_perform_with_allow_widening_is_the_console_path(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    get_raw: Callable[..., Any],
) -> None:
    """The web application, once a person has confirmed, runs the same steps with
    the boundary check lifted and gets the report back."""
    seed_grant(READER, "/public", Permission.READ)
    result = perform(ctx, FROM, TO, existing["version"], allow_widening=True)
    assert result["access_changes"] == [
        {"subject": READER, "direction": "gains", "permission": "read", "via": "/public"}
    ]
    assert result["seq"] == 2
    assert parse(get_raw(TO)).body == BODY
    assert parse(get_raw(FROM)).frontmatter["moved_to"] == TO


def test_perform_without_allow_widening_matches_the_tool(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    exists_raw: Callable[[str], bool],
) -> None:
    seed_grant(READER, "/public", Permission.READ)
    with pytest.raises(ToolError) as info:
        perform(ctx, FROM, TO, existing["version"], allow_widening=False)
    assert (info.value.status, info.value.code) == (403, "boundary_change")
    assert not exists_raw(TO)


def test_perform_validates_like_the_tool(ctx: ToolContext, minter: FakeMinter) -> None:
    for args in (("/Private/x.md", TO), (FROM, "/public/index.md"), (FROM, FROM)):
        with pytest.raises(ToolError) as info:
            perform(ctx, args[0], args[1], "v", allow_widening=True)
        assert info.value.status == 400
    with pytest.raises(ToolError) as info:
        perform(ctx, FROM, TO, "", allow_widening=True)
    assert info.value.status == 400
    assert minter.mint_count == 0


def test_same_folder_rename_proceeds_with_no_changes(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    get_raw: Callable[..., Any],
) -> None:
    """A rename inside one folder crosses no boundary: the reader of /private could
    read it before and can read it after, so nothing is reported and it proceeds."""
    seed_grant(READER, "/private", Permission.READ)
    renamed = "/private/notes/bar.md"
    result = call(TOOL, ctx, **{"from": FROM, "to": renamed, "if_version": existing["version"]})
    assert result["access_changes"] == []
    assert result["seq"] == 2
    assert parse(get_raw(renamed)).body == BODY
    assert call(READ, make_reader(ctx, READER), path=renamed)["content"] == BODY


def make_reader(ctx: ToolContext, subject: str) -> ToolContext:
    return ToolContext(
        subject=subject,
        scopes=ctx.scopes,
        grants=ctx.grants,
        minter=ctx.minter,
        settings=ctx.settings,
    )


def test_resume_completes_even_when_someone_gains(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    put_raw: Callable[..., str],
    get_raw: Callable[..., Any],
) -> None:
    """The pointer already stands (a console move that died after step 5, say): the
    source has committed, and the only way the article is readable anywhere is to
    finish. The report still comes back so the caller can surface it."""
    seed_grant(READER, "/public", Permission.READ)
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "seq": 2})
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert result["seq"] == 2
    assert result["access_changes"] == [
        {"subject": READER, "direction": "gains", "permission": "read", "via": "/public"}
    ]
    assert parse(get_raw(TO)).body == BODY


def test_moving_out_of_a_granted_prefix_reports_loses(
    ctx: ToolContext,
    seed_grant: Callable[..., None],
) -> None:
    """Narrowing proceeds on the tool surface: the reader is left with the pointer,
    which tells them it moved and where (§4.6, §5.3)."""
    seed_grant(READER, "/public", Permission.READ)
    created = call(CREATE, ctx, path=TO, content=BODY, frontmatter=FM)
    result = call(TOOL, ctx, **{"from": TO, "to": FROM, "if_version": created["version"]})
    assert result["access_changes"] == [
        {"subject": READER, "direction": "loses", "permission": "read", "via": "/public"}
    ]
    assert call(READ, ctx, path=FROM)["content"] == BODY
    reader = make_reader(ctx, READER)
    assert call(READ, reader, path=TO)["kind"] == "forward_reference"
    expect_error(READ, reader, 404, "not_found", path=FROM)


def test_owner_of_root_sees_no_change(ctx: ToolContext, existing: dict[str, Any]) -> None:
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert result["access_changes"] == []


def test_permission_drop_reports_the_higher_permission_lost(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
) -> None:
    """Editor keeps read everywhere but loses write: ``loses write via /private``."""
    seed_grant(EDITOR, "/", Permission.READ)
    seed_grant(EDITOR, "/private", Permission.WRITE)
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert result["access_changes"] == [
        {"subject": EDITOR, "direction": "loses", "permission": "write", "via": "/private"}
    ]


def test_article_grant_at_source_is_a_loss_and_display_name_is_attached(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
) -> None:
    """§4.6: an article grant is positional too — it stays on the vacated path as
    pointer access. The PROFILE row decorates the entry."""
    seed_grant(READER, FROM, Permission.READ)
    ctx.grants.table.put_item(
        Item={"pk": f"U#{READER}", "sk": "PROFILE", "display_name": "Rita Reader"}
    )
    result = call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert result["access_changes"] == [
        {
            "subject": READER,
            "direction": "loses",
            "permission": "read",
            "via": FROM,
            "display_name": "Rita Reader",
        }
    ]


def test_changes_are_sorted_by_subject_and_mixed(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
) -> None:
    """One gain is enough to refuse; the envelope carries the whole report, losses
    included, sorted by subject."""
    seed_grant("user_b", "/public/racing", Permission.WRITE)
    seed_grant("user_a", "/private/notes", Permission.OWN)
    seed_grant("user_c", "/", Permission.WRITE)  # unchanged either side
    error = expect_error(
        TOOL,
        ctx,
        403,
        "boundary_change",
        **{"from": FROM, "to": TO, "if_version": existing["version"]},
    )
    assert error.extra["access_changes"] == [
        {"subject": "user_a", "direction": "loses", "permission": "own", "via": "/private/notes"},
        {"subject": "user_b", "direction": "gains", "permission": "write", "via": "/public/racing"},
    ]


def test_impact_is_computed_before_any_write(
    ctx: ToolContext,
    existing: dict[str, Any],
    seed_grant: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    exists_raw: Callable[[str], bool],
) -> None:
    """A failing impact lookup (say, DynamoDB is down) leaves both keys untouched."""
    seed_grant(READER, "/public", Permission.READ)

    def down(*_: Any, **__: Any) -> Any:
        raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(type(ctx.grants), "subjects_reaching", down)
    with pytest.raises(RuntimeError):
        call(TOOL, ctx, **{"from": FROM, "to": TO, "if_version": existing["version"]})
    assert (
        parse(
            ctx.minter.s3(ctx.subject, Shape.READ, FROM)
            .get_object(Bucket=ctx.settings.bucket, Key=key_for(FROM))["Body"]
            .read()
        ).type
        == "doc"
    )
    assert not exists_raw(TO)


def test_profile_lookup_failure_still_reports(
    ctx: ToolContext, existing: dict[str, Any], seed_grant: Callable[..., None]
) -> None:
    seed_grant(READER, "/public", Permission.READ)
    changes = access_changes(
        ToolContext(
            subject=ctx.subject,
            scopes=ctx.scopes,
            grants=_Profileless(ctx.grants),
            minter=ctx.minter,
            settings=ctx.settings,
        ),
        FROM,
        TO,
    )
    assert changes == [
        {"subject": READER, "direction": "gains", "permission": "read", "via": "/public"}
    ]


class _Profileless:
    """A grant store whose table refuses point reads: the report must not need them."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def subjects_reaching(self, path: str) -> Any:
        return self._real.subjects_reaching(path)

    @property
    def table(self) -> Any:
        raise RuntimeError("table unavailable")
