"""The web application under real-S3 semantics (HANDOFF §8.5 ``absent_on_denied``).

Same wrapper as ``tests/unit/tools/test_real_s3_semantics.py``: a READ or WRITE
credential answers a missing key with 403, as S3 does without ``s3:ListBucket``;
LIST and MAINTAIN see moto's honest 404. The move page's preview and confirm, and
the read view's article and version pages, must read that 403 as absence.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.auth.admin import GrantAdmin
from app.auth.credentials import Shape
from app.auth.types import Grant, Permission
from app.config import Settings
from app.storage.articles import key_for
from app.storage.markdown import parse, serialize
from app.web import app as web_app
from app.web import session as sess
from app.web.views import move as move_view
from tests.unit.tools.test_real_s3_semantics import RealS3
from tests.unit.web.conftest import SUBJECT, FakeMinter, call, csrf_for, event

DANA = "user_dana"
FROM = "/family/private/notes.md"
TO = "/family/shared/notes.md"
BODY = "The body.\n"
FM = {"type": "doc", "title": "Notes", "seq": 1}
GAINS_SENTENCE = "Dana gains read (via /family/shared)"

_KEY_SCOPED = frozenset({Shape.READ, Shape.WRITE})


class RealS3WebMinter(FakeMinter):
    """The web conftest's fake minter, answering per shape as ``RealS3Minter`` does."""

    def s3(self, subject: str, shape: Shape, path: str) -> Any:
        self.mint(subject, shape, path)
        return RealS3(self._s3) if Shape(shape) in _KEY_SCOPED else self._s3


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def real_s3(monkeypatch: pytest.MonkeyPatch, bucket: Any) -> RealS3WebMinter:
    """Replace the conftest's minter before the first request builds the context."""
    minter = RealS3WebMinter(bucket)
    monkeypatch.setattr(web_app, "CredentialMinter", lambda *a, **k: minter)
    return minter


@pytest.fixture
def put_raw(bucket: Any, settings: Settings) -> Callable[..., str]:
    """Write one object directly; returns its S3 version id."""

    def _put(path: str, frontmatter: dict[str, Any], body: str = "") -> str:
        response = bucket.put_object(
            Bucket=settings.bucket,
            Key=key_for(path),
            Body=serialize(frontmatter, body),
            Metadata={"actor": f"human:{SUBJECT}", "kind": "write"},
        )
        return str(response["VersionId"])

    return _put


@pytest.fixture
def top(bucket: Any, settings: Settings) -> Callable[[str], Any | None]:
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
def as_owner(profile: Any, admin: GrantAdmin, grant: Callable[..., None]) -> None:
    """The viewer owns ``/family``; Dana reads ``/family/shared`` so the move widens."""
    admin.create_profile(DANA, "dana@example.com", "Dana", "active")
    grant("/family/shared", "read", DANA)
    grant("/family", "own")


@pytest.fixture
def cookie(session_cookie: str) -> str:
    return session_cookie


def get(path: str, cookie: str, **query: str) -> dict[str, Any]:
    return call(event("GET", path, cookies={sess.SESSION_COOKIE: cookie}, query=query or None))


def post(cookie: str, form: dict[str, str]) -> dict[str, Any]:
    data = {**form, "csrf": csrf_for(cookie)}
    return call(event("POST", "/app/admin/move", cookies={sess.SESSION_COOKIE: cookie}, form=data))


def preview(cookie: str, from_path: str = FROM, to_path: str = TO) -> dict[str, Any]:
    return post(cookie, {"action": "preview", "from": from_path, "to": to_path})


def confirm(cookie: str, version: str) -> dict[str, Any]:
    return post(cookie, {"action": "confirm", "from": FROM, "to": TO, "if_version": version})


# --- the move page -----------------------------------------------------------------------


def test_preview_to_an_empty_destination_is_clear(
    as_owner: None, put_raw: Callable[..., str], cookie: str, real_s3: RealS3WebMinter
) -> None:
    put_raw(FROM, FM, BODY)
    out = preview(cookie)
    assert out["statusCode"] == 200
    assert GAINS_SENTENCE in out["body"] and "already occupies" not in out["body"]
    # Both probes went through the wrapper: READ on each end, nothing wider.
    assert (SUBJECT, "read", FROM) in real_s3.minted
    assert (SUBJECT, "read", TO) in real_s3.minted


def test_preview_of_a_missing_source_is_404(as_owner: None, cookie: str) -> None:
    out = preview(cookie, from_path="/family/private/none.md")
    assert out["statusCode"] == 404 and move_view.NO_SUCH_SOURCE in out["body"]


def test_preview_of_an_occupied_destination_is_409(
    as_owner: None, put_raw: Callable[..., str], cookie: str
) -> None:
    put_raw(FROM, FM, BODY)
    put_raw(TO, FM, "already here")
    out = preview(cookie)
    assert out["statusCode"] == 409 and "already occupies the destination" in out["body"]


def test_confirm_to_an_empty_destination_performs_the_move(
    as_owner: None,
    put_raw: Callable[..., str],
    cookie: str,
    top: Callable[[str], Any | None],
    bucket: Any,
    settings: Settings,
) -> None:
    """The tool's step 1 heads the empty destination under WRITE — 403 in production.
    Before ``absent_on_denied`` this page answered every confirm with 403."""
    put_raw(FROM, FM, BODY)
    version = bucket.head_object(Bucket=settings.bucket, Key=key_for(FROM))["ETag"].strip('"')
    out = confirm(cookie, version)
    assert out["statusCode"] == 200, out["body"][:500]
    assert GAINS_SENTENCE in out["body"]
    moved = top(TO)
    assert moved is not None and moved.body == BODY and moved.frontmatter["moved_from"] == FROM
    pointer = top(FROM)
    assert pointer is not None and pointer.type == "pointer"
    assert pointer.frontmatter["moved_to"] == TO


def test_confirm_finishes_a_half_complete_move(
    as_owner: None, put_raw: Callable[..., str], cookie: str, top: Callable[[str], Any | None]
) -> None:
    put_raw(FROM, FM, BODY)
    put_raw(FROM, {"type": "pointer", "moved_to": TO, "seq": 2})
    assert "half-way" in preview(cookie)["body"]
    out = confirm(cookie, "whatever")
    assert out["statusCode"] == 200
    moved = top(TO)
    assert moved is not None and moved.body == BODY and moved.frontmatter["seq"] == 2


# --- the read view -----------------------------------------------------------------------


def test_article_read_existing_and_missing(
    profile: Any, grant: Callable[..., None], put_raw: Callable[..., str], cookie: str
) -> None:
    grant("/racing", "read")
    put_raw("/racing/x.md", {"type": "doc", "title": "X", "seq": 1}, "Body.\n")
    out = get("/app/a/racing/x.md", cookie)
    assert out["statusCode"] == 200 and "Body." in out["body"]
    assert get("/app/a/racing/ghost.md", cookie)["statusCode"] == 404


def test_version_read_existing_and_missing(
    profile: Any, grant: Callable[..., None], put_raw: Callable[..., str], cookie: str
) -> None:
    grant("/racing", "read")
    v1 = put_raw("/racing/x.md", {"type": "doc", "title": "X", "seq": 1}, "Old.\n")
    put_raw("/racing/x.md", {"type": "doc", "title": "X", "seq": 2}, "New.\n")
    out = get("/app/v/racing/x.md", cookie, version=v1)
    assert out["statusCode"] == 200 and "Old." in out["body"]
    assert get("/app/v/racing/x.md", cookie, version="nope")["statusCode"] == 404
    assert get("/app/v/racing/ghost.md", cookie, version=v1)["statusCode"] == 404


def test_history_of_an_existing_path_still_renders(
    profile: Any, grant: Callable[..., None], put_raw: Callable[..., str], cookie: str
) -> None:
    """The per-version peeks are of versions just listed: no flag, and no 403."""
    grant("/racing", "read")
    put_raw("/racing/x.md", {"type": "doc", "title": "X", "seq": 1}, "Old.\n")
    put_raw("/racing/x.md", {"type": "doc", "title": "X", "seq": 2}, "New.\n")
    out = get("/app/history/racing/x.md", cookie)
    assert out["statusCode"] == 200
