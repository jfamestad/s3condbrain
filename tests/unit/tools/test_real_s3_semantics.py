"""Real-S3 semantics the moto suite cannot show (HANDOFF §8.5 ``absent_on_denied``).

Real S3 answers ``HeadObject``/``GetObject`` on a key that does not exist with **403
AccessDenied, not 404**, unless the caller holds ``s3:ListBucket``. READ and WRITE
session policies deliberately carry no ``ListBucket``; LIST and MAINTAIN do. moto
answers 404 everywhere, so every tool that probes a possibly-absent key under a READ
or WRITE credential behaves differently in production than under the plain fixtures.

``RealS3`` wraps the moto client to answer as production does: a head or get that
moto refuses with 404 comes back as 403. Existing keys are untouched. ``RealS3Minter``
hands that wrapper out for READ and WRITE credentials and the plain client for LIST
and MAINTAIN, exactly as the shapes differ in §8.5 — so listing upkeep still sees its
honest 404s. The tests then walk every write tool and the reads under the wrapper.

``make security`` against a deployed instance exercises the same thing against S3
itself; this file is the unit-level stand-in.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.auth.credentials import Shape
from app.auth.types import Permission
from app.mcp.protocol import ToolContext
from app.mcp.tools.archive_article import TOOL as ARCHIVE
from app.mcp.tools.create_article import TOOL as CREATE
from app.mcp.tools.move_article import TOOL as MOVE
from app.mcp.tools.read_article import TOOL as READ
from app.mcp.tools.read_version import TOOL as READ_VERSION
from app.mcp.tools.unarchive_article import TOOL as UNARCHIVE
from app.mcp.tools.update_article import TOOL as UPDATE
from app.storage.articles import META_KIND, ArticleStore, key_for
from app.storage.markdown import parse
from tests.unit.tools.conftest import OWNER, FakeMinter, call, expect_error

_NOT_FOUND = frozenset({"NoSuchKey", "NotFound", "NoSuchVersion", "404"})
_KEY_SCOPED = frozenset({Shape.READ, Shape.WRITE})


def _access_denied(operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        operation,
    )


def _is_not_found(error: ClientError) -> bool:
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
    return code in _NOT_FOUND or status == 404


class RealS3:
    """The moto client with production's answer for a missing key on head/get.

    Only ``head_object`` and ``get_object`` are touched, and only when moto would
    have said 404: that 404 becomes 403 AccessDenied, as S3 answers a caller without
    ``s3:ListBucket``. Every other verb, and every existing key, passes straight
    through.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def head_object(self, **kwargs: Any) -> Any:
        try:
            return self._inner.head_object(**kwargs)
        except ClientError as error:
            if _is_not_found(error):
                raise _access_denied("HeadObject") from None
            raise

    def get_object(self, **kwargs: Any) -> Any:
        try:
            return self._inner.get_object(**kwargs)
        except ClientError as error:
            if _is_not_found(error):
                raise _access_denied("GetObject") from None
            raise


class RealS3Minter(FakeMinter):
    """``FakeMinter`` that answers as the §8.5 shapes do: READ and WRITE (no
    ``ListBucket``) get the ``RealS3`` wrapper; LIST and MAINTAIN the plain client."""

    def mint(self, subject: str, shape: Shape, path: str) -> Any:
        self.mint_count += 1
        self.calls.append((subject, Shape(shape), path))
        client = RealS3(self._s3) if Shape(shape) in _KEY_SCOPED else self._s3
        return SimpleNamespace(client=lambda _name: client)


PATH = "/racing/setup/rear-bar.md"
FROM = "/racing/notes/rear-bar.md"
TO = "/racing/setup/rear-bar-moved.md"
FM = {"type": "doc", "title": "Rear bar", "tags": ["racing"]}
BODY = "the body\n"


@pytest.fixture
def real_ctx(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None], bucket: Any
) -> ToolContext:
    """An owner of ``/`` whose READ and WRITE credentials answer like real S3."""
    seed_grant(OWNER, "/", Permission.OWN)
    return make_ctx(OWNER, minter_override=RealS3Minter(bucket))


@pytest.fixture
def exists_raw(bucket: Any, settings: Any) -> Callable[[str], bool]:
    def _exists(path: str) -> bool:
        response = bucket.list_object_versions(Bucket=settings.bucket, Prefix=key_for(path))
        return any(v["Key"] == key_for(path) for v in response.get("Versions", []))

    return _exists


# --- the wrapper itself -------------------------------------------------------------------


def test_wrapper_answers_a_missing_key_with_403_and_an_existing_one_normally(
    bucket: Any, settings: Any, put_raw: Callable[..., str]
) -> None:
    real = RealS3(bucket)
    for verb in (real.head_object, real.get_object):
        with pytest.raises(ClientError) as info:
            verb(Bucket=settings.bucket, Key=key_for(PATH))
        assert info.value.response["Error"]["Code"] == "AccessDenied"
        assert info.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    put_raw(PATH, {**FM, "seq": 1}, BODY)
    assert real.head_object(Bucket=settings.bucket, Key=key_for(PATH))["ContentLength"] > 0
    assert real.get_object(Bucket=settings.bucket, Key=key_for(PATH))["Body"].read()
    # Other verbs are the moto client's own.
    assert real.list_object_versions(Bucket=settings.bucket, Prefix=key_for(PATH))["Versions"]


def test_minter_wraps_only_the_key_scoped_shapes(bucket: Any) -> None:
    minter = RealS3Minter(bucket)
    assert isinstance(minter.s3(OWNER, Shape.READ, PATH), RealS3)
    assert isinstance(minter.s3(OWNER, Shape.WRITE, PATH), RealS3)
    assert minter.s3(OWNER, Shape.LIST, "/racing") is bucket
    assert minter.s3(OWNER, Shape.MAINTAIN, "/racing") is bucket


# --- create -------------------------------------------------------------------------------


def test_create_at_an_empty_path_succeeds(
    real_ctx: ToolContext, get_raw: Callable[..., Any]
) -> None:
    result = call(CREATE, real_ctx, path=PATH, content=BODY, frontmatter=FM)
    assert result["seq"] == 1
    assert parse(get_raw(PATH)).body == BODY


def test_create_on_an_occupied_path_still_classifies_the_occupant(
    real_ctx: ToolContext,
) -> None:
    """The post-412 look is at a key that exists by then, so it needs no flag."""
    call(CREATE, real_ctx, path=PATH, content=BODY, frontmatter=FM)
    expect_error(CREATE, real_ctx, 409, "exists", path=PATH, content="x", frontmatter=FM)


# --- update / archive / unarchive on a missing path: 404, never 403 ----------------------


def test_update_of_a_missing_path_is_404(real_ctx: ToolContext) -> None:
    expect_error(UPDATE, real_ctx, 404, "not_found", path=PATH, content="x", if_version="v")


def test_archive_of_a_missing_path_is_404(real_ctx: ToolContext) -> None:
    expect_error(ARCHIVE, real_ctx, 404, "not_found", path=PATH, if_version="v")


def test_unarchive_of_a_missing_path_is_404(real_ctx: ToolContext) -> None:
    expect_error(UNARCHIVE, real_ctx, 404, "not_found", path=PATH)


def test_update_archive_unarchive_cycle_on_an_existing_path(
    real_ctx: ToolContext, get_raw: Callable[..., Any]
) -> None:
    """Existing keys answer normally under the wrapper, so the whole lifecycle runs."""
    created = call(CREATE, real_ctx, path=PATH, content=BODY, frontmatter=FM)
    updated = call(UPDATE, real_ctx, path=PATH, content="v2\n", if_version=created["version"])
    assert updated["seq"] == 2
    archived = call(ARCHIVE, real_ctx, path=PATH, if_version=updated["version"])
    assert archived["archived"] is True
    expect_error(READ, real_ctx, 404, "not_found", path=PATH)
    restored = call(UNARCHIVE, real_ctx, path=PATH, if_version=archived["version"])
    assert restored["seq"] == 4
    assert parse(get_raw(PATH)).body == "v2\n"


# --- move ---------------------------------------------------------------------------------


def test_move_to_an_empty_destination_succeeds_end_to_end(
    real_ctx: ToolContext, get_raw: Callable[..., Any], head_raw: Callable[..., Any]
) -> None:
    """The regression this file exists for: step 1 heads an empty destination under
    the WRITE credential for that key, and real S3 says 403. That is "empty"."""
    created = call(CREATE, real_ctx, path=FROM, content=BODY, frontmatter=FM)
    result = call(MOVE, real_ctx, **{"from": FROM, "to": TO, "if_version": created["version"]})
    assert result["to"] == TO and result["seq"] == 2
    pointer = parse(get_raw(FROM))
    assert pointer.type == "pointer" and pointer.frontmatter["moved_to"] == TO
    moved = parse(get_raw(TO))
    assert moved.body == BODY and moved.frontmatter["moved_from"] == FROM
    assert head_raw(TO)["Metadata"][META_KIND] == "moved_in"
    assert call(READ, real_ctx, path=FROM)["kind"] == "forward_reference"
    assert call(READ, real_ctx, path=TO)["content"] == BODY


def test_move_of_a_missing_source_is_404(
    real_ctx: ToolContext, exists_raw: Callable[[str], bool]
) -> None:
    expect_error(MOVE, real_ctx, 404, "not_found", **{"from": FROM, "to": TO, "if_version": "v"})
    assert not exists_raw(TO)


def test_move_to_an_occupied_destination_is_still_409(real_ctx: ToolContext) -> None:
    created = call(CREATE, real_ctx, path=FROM, content=BODY, frontmatter=FM)
    call(CREATE, real_ctx, path=TO, content="taken\n", frontmatter=FM)
    expect_error(
        MOVE,
        real_ctx,
        409,
        "conflict",
        **{"from": FROM, "to": TO, "if_version": created["version"]},
    )


def test_resume_after_a_crash_completes_the_move(
    real_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    get_raw: Callable[..., Any],
    exists_raw: Callable[[str], bool],
) -> None:
    """The half-complete path (§8.3) under real semantics: the retry heads the empty
    destination (403 → empty), reads the pointer at the source, and finishes from
    the content version beneath it."""
    created = call(CREATE, real_ctx, path=FROM, content=BODY, frontmatter=FM)
    real_put_new = ArticleStore.put_new

    def crash(*_: Any, **__: Any) -> Any:
        raise ConnectionError("transport failure")

    monkeypatch.setattr(ArticleStore, "put_new", crash)
    with pytest.raises(ConnectionError):
        call(MOVE, real_ctx, **{"from": FROM, "to": TO, "if_version": created["version"]})
    assert parse(get_raw(FROM)).type == "pointer" and not exists_raw(TO)
    expect_error(READ, real_ctx, 404, "not_found", path=TO)

    monkeypatch.setattr(ArticleStore, "put_new", real_put_new)
    result = call(MOVE, real_ctx, **{"from": FROM, "to": TO, "if_version": created["version"]})
    assert result["seq"] == 2
    assert parse(get_raw(TO)).body == BODY


# --- reads --------------------------------------------------------------------------------


def test_read_article_missing_is_404(real_ctx: ToolContext) -> None:
    expect_error(READ, real_ctx, 404, "not_found", path=PATH)


def test_read_version_missing_is_404(real_ctx: ToolContext) -> None:
    expect_error(READ_VERSION, real_ctx, 404, "not_found", path=PATH, version_id="v")
    call(CREATE, real_ctx, path=PATH, content=BODY, frontmatter=FM)
    expect_error(READ_VERSION, real_ctx, 404, "not_found", path=PATH, version_id="nope")


def test_read_article_existing_is_unaffected(real_ctx: ToolContext) -> None:
    created = call(CREATE, real_ctx, path=PATH, content=BODY, frontmatter=FM)
    got = call(READ, real_ctx, path=PATH)
    assert got["content"] == BODY and got["version"] == created["version"]


__all__ = ["RealS3", "RealS3Minter"]
