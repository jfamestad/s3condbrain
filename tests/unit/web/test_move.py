"""The move page (HANDOFF §4.6, §4.7, §11.6): the human surface for a widening move.

The viewer (``SUBJECT``) owns ``/family``; Dana reads ``/family/shared``. Moving
``/family/private/notes.md`` to ``/family/shared/notes.md`` gives Dana access — the
tool surface refuses it, this page previews it and, on confirm, performs it with the
same pointer-first steps. Articles are seeded straight into the moto bucket; grants
through ``GrantStore``; nothing here touches the MCP tool handlers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.auth.admin import GrantAdmin
from app.auth.types import Grant, Permission
from app.config import Settings
from app.storage.articles import key_for
from app.storage.markdown import parse, serialize
from app.web import session as sess
from app.web.views import move as move_view
from tests.unit.web.conftest import SESSION_KEY, SUBJECT, call, csrf_for, event

DANA = "user_dana"
FROM = "/family/private/notes.md"
TO = "/family/shared/notes.md"
BODY = "The body.\n"
FM = {"type": "doc", "title": "Notes", "seq": 1}
GAINS_SENTENCE = "Dana gains read (via /family/shared)"
LOSES_SENTENCE = "Dana loses read (via /family/shared)"


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def put_raw(bucket: Any, settings: Settings) -> Callable[..., str]:
    """Write one object directly; returns its ``version`` token (the ETag, unquoted)."""

    def _put(path: str, frontmatter: dict[str, Any], body: str = "") -> str:
        response = bucket.put_object(
            Bucket=settings.bucket,
            Key=key_for(path),
            Body=serialize(frontmatter, body),
            Metadata={"actor": f"human:{SUBJECT}", "kind": "write"},
        )
        return str(response["ETag"]).strip('"')

    return _put


@pytest.fixture
def top(bucket: Any, settings: Settings) -> Callable[[str], Any | None]:
    """The parsed current object at ``path``, or ``None`` when nothing is there."""

    def _top(path: str) -> Any | None:
        try:
            body = bucket.get_object(Bucket=settings.bucket, Key=key_for(path))["Body"].read()
        except ClientError:
            return None
        return parse(body)

    return _top


@pytest.fixture
def grant(admin: GrantAdmin) -> Callable[..., None]:
    def _grant(node: str, permission: str, subject: str = SUBJECT) -> None:
        admin.store.put_grant(
            Grant(subject, node, Permission(permission), "bootstrap", "2026-01-01T00:00:00Z")
        )

    return _grant


@pytest.fixture
def dana(admin: GrantAdmin, grant: Callable[..., None]) -> None:
    admin.create_profile(DANA, "dana@example.com", "Dana", "active")
    grant("/family/shared", "read", DANA)


@pytest.fixture
def as_owner(profile: Any, dana: None, grant: Callable[..., None]) -> None:
    """The viewer owns ``/family`` — write on both ends and own over the destination."""
    grant("/family", "own")


@pytest.fixture
def as_writer(profile: Any, dana: None, grant: Callable[..., None]) -> None:
    """The viewer writes ``/family`` but owns nothing: enough for the tool's rule,
    not for this page's."""
    grant("/family", "write")


@pytest.fixture
def article(put_raw: Callable[..., str]) -> str:
    return put_raw(FROM, FM, BODY)


@pytest.fixture
def cookie(session_cookie: str) -> str:
    return session_cookie


def get(path: str, cookie: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    return call(event("GET", path, query=query, cookies={sess.SESSION_COOKIE: cookie}))


def post(cookie: str, form: dict[str, str], *, csrf: bool = True) -> dict[str, Any]:
    data = {**form, "csrf": csrf_for(cookie)} if csrf else dict(form)
    return call(event("POST", "/app/admin/move", cookies={sess.SESSION_COOKIE: cookie}, form=data))


def preview(cookie: str, from_path: str = FROM, to_path: str = TO, **kw: Any) -> dict[str, Any]:
    return post(cookie, {"action": "preview", "from": from_path, "to": to_path}, **kw)


def confirm(
    cookie: str, version: str, from_path: str = FROM, to_path: str = TO, **kw: Any
) -> dict[str, Any]:
    form = {"action": "confirm", "from": from_path, "to": to_path, "if_version": version}
    return post(cookie, form, **kw)


def assert_untouched(top: Callable[[str], Any | None], version: str, bucket: Any, settings: Any):
    source = top(FROM)
    assert source is not None and source.type == "doc"
    head = bucket.head_object(Bucket=settings.bucket, Key=key_for(FROM))
    assert head["ETag"].strip('"') == version
    assert top(TO) is None


# --- session gate ------------------------------------------------------------------------


def test_requires_login() -> None:
    out = call(event("GET", "/app/admin/move"))
    assert out["statusCode"] == 303 and "/app/login" in out["headers"]["Location"]
    out = call(event("POST", "/app/admin/move", form={"action": "preview"}))
    assert out["statusCode"] == 303 and "/app/login" in out["headers"]["Location"]


def test_post_without_csrf_is_403(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    assert preview(cookie, csrf=False)["statusCode"] == 403
    assert confirm(cookie, article, csrf=False)["statusCode"] == 403
    assert_untouched(top, article, bucket, settings)


# --- the form ----------------------------------------------------------------------------


def test_form_is_linked_from_admin_and_prefills_from(as_owner: None, cookie: str) -> None:
    index = get("/app/admin", cookie)["body"]
    assert "/app/admin/move" in index and "Move an article across a permission boundary" in index
    body = get("/app/admin/move", cookie, {"from": FROM})["body"]
    assert f'name="from" required value="{FROM}"' in body
    assert 'name="action" value="preview"' in body
    assert 'name="csrf" value="' in body
    assert "Confirm the move" not in body


def test_owner_of_nothing_sees_the_sentence(profile: Any, cookie: str) -> None:
    body = get("/app/admin/move", cookie)["body"]
    assert "You don't own any part of the tree" in body
    assert 'name="from"' not in body


# --- preview -----------------------------------------------------------------------------


def test_preview_shows_who_gains_and_writes_nothing(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    out = preview(cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert GAINS_SENTENCE in body
    assert "1 person will be able to read this article" in body
    assert f'name="if_version" value="{article}"' in body
    assert 'name="action" value="confirm"' in body
    assert_untouched(top, article, bucket, settings)


def test_preview_of_a_narrowing_move_says_who_loses(
    as_owner: None, put_raw: Callable[..., str], cookie: str
) -> None:
    put_raw(TO, FM, BODY)
    body = preview(cookie, from_path=TO, to_path=FROM)["body"]
    assert LOSES_SENTENCE in body
    assert "will be able to read" not in body


def test_preview_of_a_rename_reports_nobody(as_owner: None, article: str, cookie: str) -> None:
    body = preview(cookie, to_path="/family/private/renamed.md")["body"]
    assert "Nobody gains or loses access" in body
    assert "Dana" not in body


def test_preview_names_by_email_when_there_is_no_display_name(
    as_owner: None, article: str, cookie: str, admin: GrantAdmin
) -> None:
    admin.table.update_item(
        Key={"pk": f"U#{DANA}", "sk": "PROFILE"},
        UpdateExpression="SET display_name = :n",
        ExpressionAttributeValues={":n": ""},
    )
    body = preview(cookie)["body"]
    assert "dana@example.com gains read (via /family/shared)" in body


def test_preview_of_a_missing_source_is_404(as_owner: None, cookie: str) -> None:
    out = preview(cookie, from_path="/family/private/none.md")
    assert out["statusCode"] == 404 and move_view.NO_SUCH_SOURCE in out["body"]


def test_preview_of_an_occupied_destination_is_409(
    as_owner: None, article: str, put_raw: Callable[..., str], cookie: str
) -> None:
    put_raw(TO, FM, "already here")
    out = preview(cookie)
    assert out["statusCode"] == 409 and "already occupies the destination" in out["body"]


# --- confirm -----------------------------------------------------------------------------


def test_confirm_performs_the_move_and_reports(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    out = confirm(cookie, article)
    assert out["statusCode"] == 200
    body = out["body"]
    assert GAINS_SENTENCE in body
    assert f"Earlier versions remain at {FROM}" in body
    assert f"/app/a{TO}" in body
    moved = top(TO)
    assert moved is not None and moved.body == BODY
    assert moved.frontmatter["moved_from"] == FROM and moved.frontmatter["seq"] == 2
    head = bucket.head_object(Bucket=settings.bucket, Key=key_for(TO))
    assert head["Metadata"] == {"actor": f"human:{SUBJECT}", "kind": "moved_in", "moved-from": FROM}
    pointer = top(FROM)
    assert pointer is not None and pointer.type == "pointer"
    assert pointer.frontmatter["moved_to"] == TO
    # The reader the tool refused this for can now read it, and finds a pointer at the old path.
    dana_cookie = sess.issue_session(SESSION_KEY, DANA, "dana@example.com", "Dana", 12)
    assert get(f"/app/a{TO}", dana_cookie)["statusCode"] == 200
    assert get(f"/app/a{FROM}", dana_cookie)["statusCode"] == 404


def test_confirm_with_a_stale_version_moves_nothing(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    out = confirm(cookie, "not-the-version")
    assert out["statusCode"] == 409
    assert move_view.STALE in out["body"]
    assert "Preview the move" in out["body"]  # the form is back, prefilled
    assert f'name="from" required value="{FROM}"' in out["body"]
    assert_untouched(top, article, bucket, settings)


def test_confirm_without_a_version_is_400(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    out = post(cookie, {"action": "confirm", "from": FROM, "to": TO})
    assert out["statusCode"] == 400 and move_view.NOT_PREVIEWED in out["body"]
    assert_untouched(top, article, bucket, settings)


def test_unknown_action_is_400(as_owner: None, article: str, cookie: str) -> None:
    assert post(cookie, {"action": "go", "from": FROM, "to": TO})["statusCode"] == 400


def test_confirm_finishes_a_half_complete_move(
    as_owner: None,
    article: str,
    put_raw: Callable[..., str],
    cookie: str,
    top: Callable[[str], Any | None],
) -> None:
    """A pointer already at the source naming the destination (§8.3): the preview
    says so and the confirm completes it from the version beneath."""
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "seq": 2})
    body = preview(cookie)["body"]
    assert "half-way" in body and GAINS_SENTENCE in body
    out = confirm(cookie, "whatever")
    assert out["statusCode"] == 200
    moved = top(TO)
    assert moved is not None and moved.body == BODY and moved.frontmatter["seq"] == 2


# --- authorization -----------------------------------------------------------------------


def test_writer_without_own_on_the_destination_is_403(
    as_writer: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    out = preview(cookie)
    assert out["statusCode"] == 403 and "You do not own /family/shared" in out["body"]
    out = confirm(cookie, article)
    assert out["statusCode"] == 403
    assert_untouched(top, article, bucket, settings)


def test_owner_elsewhere_without_write_on_the_source_is_403(
    profile: Any,
    dana: None,
    grant: Callable[..., None],
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
) -> None:
    grant("/family/shared", "own")
    out = preview(cookie)
    assert out["statusCode"] == 403 and move_view.NEED_WRITE in out["body"]
    assert confirm(cookie, article)["statusCode"] == 403
    assert_untouched(top, article, bucket, settings)


def test_own_on_the_destination_folder_itself_suffices(
    profile: Any,
    dana: None,
    grant: Callable[..., None],
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
) -> None:
    grant("/family/private", "write")
    grant("/family/shared", "own")
    assert preview(cookie)["statusCode"] == 200
    assert confirm(cookie, article)["statusCode"] == 200
    assert top(TO) is not None


# --- malformed paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_path", "to_path"),
    [
        ("/Family/private/notes.md", TO),
        ("family/private/notes.md", TO),
        (FROM, "/family/shared/index.md"),
        (FROM, "/family/shared/_notes.md"),
        (FROM, "/family/shared/notes"),
        (FROM, FROM),
        ("", TO),
        (FROM, ""),
    ],
)
def test_malformed_paths_are_400_and_write_nothing(
    as_owner: None,
    article: str,
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Any,
    from_path: str,
    to_path: str,
) -> None:
    assert preview(cookie, from_path, to_path)["statusCode"] == 400
    assert confirm(cookie, article, from_path, to_path)["statusCode"] == 400
    assert_untouched(top, article, bucket, settings)
