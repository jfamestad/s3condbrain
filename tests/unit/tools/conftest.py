"""Tool-test fixtures: a real ``ToolContext`` over moto S3 and DynamoDB.

The grant store is the real ``GrantStore`` against the moto table. The minter is a
fake that hands back the moto S3 client and counts how often it was asked — the
zero-grant tests assert that count stays at 0 (§11.3 step 9).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from app.auth.credentials import Shape
from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission
from app.config import Settings
from app.errors import ToolError
from app.mcp.protocol import Tool, ToolContext
from app.storage.articles import key_for
from app.storage.markdown import serialize

OWNER = "user_owner"
NOBODY = "user_nobody"


class FakeMinter:
    """Stands in for ``CredentialMinter``: same surface, no STS.

    Attributes:
        mint_count: Times ``mint``/``s3`` were called.
        calls: ``(subject, shape, path)`` per call, for asserting the shape and target.
    """

    def __init__(self, s3_client: Any) -> None:
        self._s3 = s3_client
        self.mint_count = 0
        self.calls: list[tuple[str, Shape, str]] = []

    def mint(self, subject: str, shape: Shape, path: str) -> Any:
        self.mint_count += 1
        self.calls.append((subject, Shape(shape), path))
        return SimpleNamespace(client=lambda _name: self._s3)

    def s3(self, subject: str, shape: Shape, path: str) -> Any:
        return self.mint(subject, shape, path).client("s3")


class DenyingS3:
    """An S3 client whose every verb answers 403 — what a credential minted for one
    prefix does when asked about another."""

    def _deny(self, operation: str) -> ClientError:
        return ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "Access Denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            operation,
        )

    def get_object(self, **_: Any) -> Any:
        raise self._deny("GetObject")

    def head_object(self, **_: Any) -> Any:
        raise self._deny("HeadObject")

    def put_object(self, **_: Any) -> Any:
        raise self._deny("PutObject")

    def get_paginator(self, _name: str) -> Any:
        return self

    def paginate(self, **_: Any) -> Any:
        raise self._deny("ListObjectsV2")


@pytest.fixture
def grant_store(grant_table: Any, settings: Settings) -> GrantStore:
    resource = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    return GrantStore(settings.grant_table, dynamodb_resource=resource)


@pytest.fixture
def seed_grant(grant_store: GrantStore) -> Callable[[str, str, Permission], None]:
    def _seed(subject: str, node: str, permission: Permission) -> None:
        grant_store.put_grant(
            Grant(
                subject=subject,
                node=node,
                permission=permission,
                granted_by="bootstrap",
                granted_at="2026-01-01T00:00:00Z",
            )
        )

    return _seed


@pytest.fixture
def minter(bucket: Any) -> FakeMinter:
    return FakeMinter(bucket)


@pytest.fixture
def make_ctx(
    settings: Settings, grant_store: GrantStore, minter: FakeMinter
) -> Callable[..., ToolContext]:
    def _make(subject: str = OWNER, minter_override: Any = None) -> ToolContext:
        return ToolContext(
            subject=subject,
            scopes=frozenset({"wiki.read", "wiki.write"}),
            grants=grant_store,
            minter=minter_override or minter,
            settings=settings,
            request_id="req-test",
        )

    return _make


@pytest.fixture
def ctx(make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None]) -> ToolContext:
    """The skeleton's one grant row: ``own`` on ``/`` (§11.4)."""
    seed_grant(OWNER, "/", Permission.OWN)
    return make_ctx(OWNER)


@pytest.fixture
def nobody(make_ctx: Callable[..., ToolContext]) -> ToolContext:
    """A subject with no grants at all."""
    return make_ctx(NOBODY)


@pytest.fixture
def denied_ctx(
    make_ctx: Callable[..., ToolContext], seed_grant: Callable[..., None]
) -> ToolContext:
    """Holds the grant, but S3 refuses: exercises the AccessDenied mappings."""
    seed_grant(OWNER, "/", Permission.OWN)
    return make_ctx(OWNER, minter_override=FakeMinter(DenyingS3()))


@pytest.fixture
def put_raw(bucket: Any, settings: Settings) -> Callable[..., str]:
    """Write an object directly (bypassing the tools) and return its version token."""

    def _put(path: str, frontmatter: dict[str, Any], body: str = "", **metadata: str) -> str:
        response = bucket.put_object(
            Bucket=settings.bucket,
            Key=key_for(path),
            Body=serialize(frontmatter, body),
            Metadata=metadata,
        )
        return response["ETag"].strip('"')

    return _put


@pytest.fixture
def head_raw(bucket: Any, settings: Settings) -> Callable[[str], dict[str, Any]]:
    def _head(path: str) -> dict[str, Any]:
        return bucket.head_object(Bucket=settings.bucket, Key=key_for(path))

    return _head


@pytest.fixture
def get_raw(bucket: Any, settings: Settings) -> Callable[[str], bytes]:
    def _get(path: str) -> bytes:
        return bucket.get_object(Bucket=settings.bucket, Key=key_for(path))["Body"].read()

    return _get


def call(tool: Tool, ctx: ToolContext, **args: Any) -> dict[str, Any]:
    return tool.handler(ctx, args)


def expect_error(tool: Tool, ctx: ToolContext, status: int, code: str, **args: Any) -> ToolError:
    with pytest.raises(ToolError) as info:
        tool.handler(ctx, args)
    error = info.value
    assert (error.status, error.code) == (status, code), error.structured()
    return error


__all__ = [
    "NOBODY",
    "OWNER",
    "DenyingS3",
    "FakeMinter",
    "call",
    "expect_error",
]
