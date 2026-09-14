"""Build step 3 — credential minting, offline (HANDOFF §4.8, §8.5, §8.8, §11.3).

Three policy shapes, each with a negative assertion: READ carries no ListBucket,
LIST cannot escape its ``s3:prefix``, WRITE adds exactly PutObject and GenerateDataKey.
The ``aws``-marked tests at the bottom prove the refusals come from AWS, not from us.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from app.auth.credentials import (
    CredentialMinter,
    Shape,
    list_prefix,
    listing_key,
    s3_key,
    s3_resource,
    session_policy,
)
from app.config import MAX_PATH_LENGTH

# Snapshot of the process environment before conftest's autouse fixture replaces the
# AWS variables with fakes. The ``aws``-marked tests restore these so they run against
# real credentials. Cheap: no network, no credential resolution.
_REAL_ENV: dict[str, str] = dict(os.environ)

BUCKET = "wiki-test"
KMS = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000"
ROLE = "arn:aws:iam::123456789012:role/wiki-storage"


def _statements(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return policy["Statement"]


def _actions(policy: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for st in _statements(policy):
        a = st["Action"]
        out.update([a] if isinstance(a, str) else a)
    return out


def _by_action(policy: dict[str, Any], action: str) -> dict[str, Any]:
    for st in _statements(policy):
        a = st["Action"]
        if action in ([a] if isinstance(a, str) else a):
            return st
    raise AssertionError(f"no statement carries {action}")


def _compact(policy: dict[str, Any]) -> str:
    return json.dumps(policy, separators=(",", ":"))


# --- key and resource derivation ------------------------------------------------------


class TestS3Key:
    def test_article(self) -> None:
        assert s3_key("/racing/x.md") == "a/racing/x.md"

    def test_deep_article(self) -> None:
        assert s3_key("/racing/setup/rear-bar.md") == "a/racing/setup/rear-bar.md"

    def test_root_article(self) -> None:
        assert s3_key("/x.md") == "a/x.md"

    def test_folder(self) -> None:
        assert s3_key("/racing") == "a/racing/"

    def test_folder_trailing_slash(self) -> None:
        assert s3_key("/racing/") == "a/racing/"

    def test_root(self) -> None:
        assert s3_key("/") == "a/"

    @pytest.mark.parametrize("bad", ["", "racing/x.md", "x.md"])
    def test_relative_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            s3_key(bad)


class TestS3Resource:
    def test_article_is_exact_key(self) -> None:
        assert s3_resource(BUCKET, "/racing/x.md") == f"arn:aws:s3:::{BUCKET}/a/racing/x.md"

    def test_folder_is_prefix_wildcard(self) -> None:
        assert s3_resource(BUCKET, "/racing") == f"arn:aws:s3:::{BUCKET}/a/racing/*"

    def test_nested_folder(self) -> None:
        assert s3_resource(BUCKET, "/racing/setup") == f"arn:aws:s3:::{BUCKET}/a/racing/setup/*"

    def test_root(self) -> None:
        assert s3_resource(BUCKET, "/") == f"arn:aws:s3:::{BUCKET}/a/*"

    def test_article_arn_carries_no_wildcard(self) -> None:
        assert "*" not in s3_resource(BUCKET, "/racing/setup/rear-bar.md")

    def test_never_escapes_the_article_prefix(self) -> None:
        for p in ["/", "/racing", "/racing/x.md", "/sys"]:
            assert s3_resource(BUCKET, p).startswith(f"arn:aws:s3:::{BUCKET}/a/")


class TestListPrefix:
    def test_folder(self) -> None:
        assert list_prefix("/racing") == "a/racing/"

    def test_nested_folder(self) -> None:
        assert list_prefix("/racing/setup") == "a/racing/setup/"

    def test_root(self) -> None:
        assert list_prefix("/") == "a/"

    def test_article_uses_parent_folder(self) -> None:
        assert list_prefix("/racing/setup/rear-bar.md") == "a/racing/setup/"

    def test_root_article_uses_root(self) -> None:
        assert list_prefix("/x.md") == "a/"

    def test_always_ends_with_slash(self) -> None:
        for p in ["/", "/a", "/a/b", "/a/b/c.md"]:
            assert list_prefix(p).endswith("/")


# --- the three shapes ---------------------------------------------------------------------


class TestReadShape:
    def test_object_actions_on_exact_resource(self) -> None:
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing/x.md")
        st = _by_action(pol, "s3:GetObject")
        assert set(st["Action"]) == {"s3:GetObject", "s3:GetObjectVersion"}
        assert st["Effect"] == "Allow"
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/x.md"

    def test_folder_read_scopes_to_prefix(self) -> None:
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing")
        assert _by_action(pol, "s3:GetObject")["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/*"

    def test_kms_decrypt_on_the_one_key(self) -> None:
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing/x.md")
        st = _by_action(pol, "kms:Decrypt")
        assert st["Resource"] == KMS
        assert set(st["Action"]) == {"kms:Decrypt"}

    def test_no_list_bucket_anywhere(self) -> None:
        """§8.5: an unconditioned ListBucket is a disclosure, so READ must not carry it."""
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing")
        assert "s3:ListBucket" not in _actions(pol)
        assert "s3:List*" not in _actions(pol)
        assert "s3:*" not in _actions(pol)
        # ListBucketVersions is allowed (history, §8.3) but only pinned to this path.
        [versions] = [st for st in _statements(pol) if "s3:ListBucketVersions" in st["Action"]]
        assert versions["Action"] == ["s3:ListBucketVersions"]
        assert versions["Condition"] == {"StringLike": {"s3:prefix": ["a/racing/*"]}}

    def test_no_write_actions(self) -> None:
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing/x.md")
        assert _actions(pol) == {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:ListBucketVersions",
            "kms:Decrypt",
        }
        [versions] = [st for st in _statements(pol) if "s3:ListBucketVersions" in st["Action"]]
        assert versions["Condition"] == {"StringLike": {"s3:prefix": ["a/racing/x.md*"]}}

    def test_only_allow_statements_on_the_bucket_or_key(self) -> None:
        pol = session_policy(Shape.READ, BUCKET, KMS, "/racing/x.md")
        for st in _statements(pol):
            assert st["Effect"] == "Allow"
            res = st["Resource"]
            for r in [res] if isinstance(res, str) else res:
                if r == f"arn:aws:s3:::{BUCKET}":
                    assert st["Action"] == ["s3:ListBucketVersions"] and "Condition" in st
                    continue
                assert r.startswith(f"arn:aws:s3:::{BUCKET}/a/") or r == KMS


class TestListShape:
    def test_includes_read(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        assert {"s3:GetObject", "s3:GetObjectVersion", "kms:Decrypt"} <= _actions(pol)
        assert _by_action(pol, "s3:GetObject")["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/*"

    def test_list_bucket_on_bucket_arn_with_prefix_condition(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        st = _by_action(pol, "s3:ListBucket")
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}"
        cond = st["Condition"]
        assert list(cond) == ["StringLike"]
        assert cond["StringLike"] == {"s3:prefix": ["a/racing/*"]}

    def test_prefix_pattern_ends_with_slash_star(self) -> None:
        for p in ["/", "/racing", "/racing/setup"]:
            pol = session_policy(Shape.LIST, BUCKET, KMS, p)
            pats = _by_action(pol, "s3:ListBucket")["Condition"]["StringLike"]["s3:prefix"]
            assert len(pats) == 1
            assert pats[0].endswith("/*")
            assert pats[0].startswith("a/")

    def test_root_prefix(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/")
        st = _by_action(pol, "s3:ListBucket")
        assert st["Condition"]["StringLike"] == {"s3:prefix": ["a/*"]}

    def test_no_string_equals_and_no_empty_prefix_option(self) -> None:
        """The whole point: a listing cannot escape the prefix. No ``""`` escape hatch."""
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        st = _by_action(pol, "s3:ListBucket")
        assert "StringEquals" not in st["Condition"]
        for pats in st["Condition"]["StringLike"].values():
            assert "" not in pats
            assert "*" not in pats  # a bare "*" would match every prefix

    def test_list_bucket_is_never_unconditioned(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        for st in _statements(pol):
            a = st["Action"]
            if "s3:ListBucket" in ([a] if isinstance(a, str) else a):
                assert "Condition" in st

    def test_article_path_lists_its_parent(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing/x.md")
        st = _by_action(pol, "s3:ListBucket")
        assert st["Condition"]["StringLike"] == {"s3:prefix": ["a/racing/*"]}

    def test_no_write_actions(self) -> None:
        pol = session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        assert _actions(pol) == {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "kms:Decrypt",
            "s3:ListBucket",
            "s3:ListBucketVersions",
        }


class TestWriteShape:
    def test_adds_put_object_to_object_statement(self) -> None:
        pol = session_policy(Shape.WRITE, BUCKET, KMS, "/racing/x.md")
        st = _by_action(pol, "s3:PutObject")
        assert set(st["Action"]) == {"s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"}
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/x.md"

    def test_adds_generate_data_key_to_kms_statement(self) -> None:
        pol = session_policy(Shape.WRITE, BUCKET, KMS, "/racing/x.md")
        st = _by_action(pol, "kms:GenerateDataKey")
        assert set(st["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
        assert st["Resource"] == KMS

    def test_exact_action_set(self) -> None:
        pol = session_policy(Shape.WRITE, BUCKET, KMS, "/racing/x.md")
        assert _actions(pol) == {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:ListBucketVersions",
            "s3:PutObject",
            "kms:Decrypt",
            "kms:GenerateDataKey",
        }

    def test_no_list_bucket(self) -> None:
        pol = session_policy(Shape.WRITE, BUCKET, KMS, "/racing/x.md")
        assert "s3:ListBucket" not in _actions(pol)

    def test_never_delete(self) -> None:
        for shape in Shape:
            pol = session_policy(shape, BUCKET, KMS, "/racing/x.md")
            assert "Delete" not in _compact(pol)
            assert "Bypass" not in _compact(pol)


class TestPolicyDocument:
    def test_version_and_no_sid(self) -> None:
        for shape in Shape:
            pol = session_policy(shape, BUCKET, KMS, "/racing/x.md")
            assert pol["Version"] == "2012-10-17"
            assert set(pol) == {"Version", "Statement"}
            for st in _statements(pol):
                assert "Sid" not in st

    def test_is_json_serialisable(self) -> None:
        for shape in Shape:
            json.dumps(session_policy(shape, BUCKET, KMS, "/racing/x.md"))

    def test_unknown_shape_raises(self) -> None:
        with pytest.raises(ValueError):
            session_policy("delete", BUCKET, KMS, "/racing/x.md")  # type: ignore[arg-type]

    @pytest.mark.parametrize("shape", [Shape.READ, Shape.WRITE])
    def test_size_bound_for_max_length_article_path(self, shape: Shape) -> None:
        # §10.2 caps paths at MAX_PATH_LENGTH so every shape fits STS's 2 KB inline limit.
        path = "/" + "/".join(["d" * 20] * 23) + "/" + "x" * 25 + ".md"
        assert len(path) == MAX_PATH_LENGTH
        assert len(_compact(session_policy(shape, BUCKET, KMS, path))) < 2048

    @pytest.mark.parametrize("shape", [Shape.READ, Shape.WRITE])
    def test_size_bound_for_max_length_folder_path(self, shape: Shape) -> None:
        path = "/" + "/".join(["d" * 20] * 24) + "/" + "x" * 7
        assert len(path) == MAX_PATH_LENGTH
        assert len(_compact(session_policy(shape, BUCKET, KMS, path))) < 2048

    def test_list_size_bound_for_1000_char_article_path(self) -> None:
        # The prefix condition names the parent folder, so the article name does not
        # count twice.
        path = "/racing/" + "x" * 989 + ".md"
        assert len(path) == 1000
        assert len(_compact(session_policy(Shape.LIST, BUCKET, KMS, path))) < 2048

    def test_list_size_bound_for_long_folder_path(self) -> None:
        # LIST names the folder twice (object resource + prefix condition), so it cannot
        # fit a 1000-character folder inside 2048 bytes. Pin the depth it does support.
        path = "/" + "/".join(["d" * 20] * 33) + "/" + "x" * 6
        assert len(path) == 700
        assert len(_compact(session_policy(Shape.LIST, BUCKET, KMS, path))) < 2048


# --- CredentialMinter with a fake STS ---------------------------------------------------


class FakeSts:
    """Records every AssumeRole call and returns canned, distinguishable credentials."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        n = len(self.calls)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{n:016d}",
                "SecretAccessKey": f"secret-{n}",
                "SessionToken": f"token-{n}",
                "Expiration": "2026-09-13T00:15:00Z",
            },
            "AssumedRoleUser": {"AssumedRoleId": "AROA:wiki", "Arn": ROLE},
        }


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sts() -> FakeSts:
    return FakeSts()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def minter(sts: FakeSts, clock: Clock) -> CredentialMinter:
    return CredentialMinter(
        role_arn=ROLE,
        bucket=BUCKET,
        kms_key_arn=KMS,
        cache_seconds=300,
        sts_client=sts,
        clock=clock,
    )


class TestCredentialMinter:
    def test_starts_at_zero(self, minter: CredentialMinter) -> None:
        assert minter.mint_count == 0

    def test_mint_calls_assume_role_with_scoped_policy(
        self, minter: CredentialMinter, sts: FakeSts
    ) -> None:
        session = minter.mint("user_01", Shape.READ, "/racing/x.md")
        assert isinstance(session, boto3.Session)
        assert minter.mint_count == 1
        call = sts.calls[0]
        assert call["RoleArn"] == ROLE
        assert call["DurationSeconds"] == 900
        assert json.loads(call["Policy"]) == session_policy(Shape.READ, BUCKET, KMS, "/racing/x.md")

    def test_session_carries_the_minted_credentials(
        self, minter: CredentialMinter, sts: FakeSts
    ) -> None:
        session = minter.mint("user_01", Shape.READ, "/racing/x.md")
        creds = session.get_credentials()
        assert creds is not None
        assert creds.access_key == "ASIA0000000000000001"
        assert creds.secret_key == "secret-1"
        assert creds.token == "token-1"

    def test_same_key_hits_cache(self, minter: CredentialMinter) -> None:
        a = minter.mint("user_01", Shape.READ, "/racing/x.md")
        b = minter.mint("user_01", Shape.READ, "/racing/x.md")
        assert a is b
        assert minter.mint_count == 1

    def test_different_shape_mints_again(self, minter: CredentialMinter) -> None:
        minter.mint("user_01", Shape.READ, "/racing/x.md")
        minter.mint("user_01", Shape.WRITE, "/racing/x.md")
        assert minter.mint_count == 2

    def test_different_path_mints_again(self, minter: CredentialMinter) -> None:
        minter.mint("user_01", Shape.READ, "/racing/x.md")
        minter.mint("user_01", Shape.READ, "/racing/y.md")
        assert minter.mint_count == 2

    def test_different_subject_mints_again(self, minter: CredentialMinter) -> None:
        minter.mint("user_01", Shape.READ, "/racing/x.md")
        minter.mint("user_02", Shape.READ, "/racing/x.md")
        assert minter.mint_count == 2

    def test_expired_entry_is_reminted(
        self, minter: CredentialMinter, clock: Clock, sts: FakeSts
    ) -> None:
        a = minter.mint("user_01", Shape.READ, "/racing/x.md")
        clock.now += 299
        assert minter.mint("user_01", Shape.READ, "/racing/x.md") is a
        assert minter.mint_count == 1
        clock.now += 2  # past cache_seconds
        b = minter.mint("user_01", Shape.READ, "/racing/x.md")
        assert b is not a
        assert minter.mint_count == 2
        assert b.get_credentials().access_key == "ASIA0000000000000002"

    def test_cache_never_outlives_the_credential(self, sts: FakeSts, clock: Clock) -> None:
        # cache_seconds above the AssumeRole duration would hand out dead credentials.
        m = CredentialMinter(ROLE, BUCKET, KMS, cache_seconds=86_400, sts_client=sts, clock=clock)
        m.mint("user_01", Shape.READ, "/racing/x.md")
        clock.now += 900
        m.mint("user_01", Shape.READ, "/racing/x.md")
        assert m.mint_count == 2

    def test_mint_count_matches_calls(self, minter: CredentialMinter, sts: FakeSts) -> None:
        for p in ["/a.md", "/b.md", "/c.md", "/a.md"]:
            minter.mint("user_01", Shape.READ, p)
        assert minter.mint_count == 3 == len(sts.calls)

    def test_role_session_name_carries_subject(
        self, minter: CredentialMinter, sts: FakeSts
    ) -> None:
        minter.mint("user_01HZX", Shape.READ, "/racing/x.md")
        assert sts.calls[0]["RoleSessionName"] == "wiki-user_01HZX"

    def test_role_session_name_is_sanitised_and_bounded(
        self, minter: CredentialMinter, sts: FakeSts
    ) -> None:
        minter.mint("we ird/sub|ject:é" + "x" * 100, Shape.READ, "/racing/x.md")
        name = sts.calls[0]["RoleSessionName"]
        assert name.startswith("wiki-")
        assert len(name) <= 64
        assert all(c.isalnum() or c in "_=,.@-" for c in name)

    def test_shape_and_path_change_the_policy(self, minter: CredentialMinter, sts: FakeSts) -> None:
        minter.mint("user_01", Shape.LIST, "/racing")
        pol = json.loads(sts.calls[0]["Policy"])
        assert pol == session_policy(Shape.LIST, BUCKET, KMS, "/racing")
        assert "s3:ListBucket" in _actions(pol)

    def test_s3_returns_client_from_minted_session(self, minter: CredentialMinter) -> None:
        client = minter.s3("user_01", Shape.READ, "/racing/x.md")
        assert client.meta.service_model.service_name == "s3"
        assert minter.mint_count == 1
        minter.s3("user_01", Shape.READ, "/racing/x.md")
        assert minter.mint_count == 1

    def test_policy_is_compact_json(self, minter: CredentialMinter, sts: FakeSts) -> None:
        minter.mint("user_01", Shape.READ, "/racing/x.md")
        policy = sts.calls[0]["Policy"]
        assert isinstance(policy, str)
        assert ": " not in policy and ", " not in policy


class TestCredentialMinterWithMoto:
    def test_assume_role_called_with_policy_kwarg(self, aws: None, mocker) -> None:
        """A real boto3 STS client accepts the exact call shape; moto takes any role ARN."""
        sts_client = boto3.client("sts", region_name=os.environ["AWS_DEFAULT_REGION"])
        spy = mocker.patch.object(sts_client, "assume_role", wraps=sts_client.assume_role)
        m = CredentialMinter(ROLE, BUCKET, KMS, cache_seconds=60, sts_client=sts_client)
        session = m.mint("user_01", Shape.READ, "/racing/x.md")
        assert spy.call_count == 1
        kwargs = spy.call_args.kwargs
        assert kwargs["RoleArn"] == ROLE
        assert kwargs["RoleSessionName"] == "wiki-user_01"
        assert kwargs["DurationSeconds"] == 900
        assert json.loads(kwargs["Policy"]) == session_policy(
            Shape.READ, BUCKET, KMS, "/racing/x.md"
        )
        creds = session.get_credentials()
        assert creds is not None and creds.token
        assert m.mint_count == 1

    def test_default_sts_client_is_constructed(self, aws: None) -> None:
        m = CredentialMinter(ROLE, BUCKET, KMS)
        m.mint("user_01", Shape.READ, "/racing/x.md")
        assert m.mint_count == 1


# --- Against real AWS (skipped unless -m aws and the env is present) ---------------------

_REAL_KEYS = ("WIKI_BUCKET", "STORAGE_ROLE_ARN", "KMS_KEY_ARN")
_AWS_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_PROFILE",
)


@pytest.fixture
def real_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Undo conftest's fake AWS environment and restore what the process started with."""
    missing = [k for k in _REAL_KEYS if not _REAL_ENV.get(k)]
    if missing:
        pytest.skip(f"real AWS env not set: {', '.join(missing)}")
    for k in _AWS_VARS + _REAL_KEYS:
        if k in _REAL_ENV:
            monkeypatch.setenv(k, _REAL_ENV[k])
        else:
            monkeypatch.delenv(k, raising=False)
    return {k: _REAL_ENV[k] for k in _REAL_KEYS}


@pytest.fixture
def real_minter(real_env: dict[str, str]) -> CredentialMinter:
    return CredentialMinter(
        role_arn=real_env["STORAGE_ROLE_ARN"],
        bucket=real_env["WIKI_BUCKET"],
        kms_key_arn=real_env["KMS_KEY_ARN"],
        cache_seconds=60,
        sts_client=boto3.client("sts"),
    )


def _error_code(exc: ClientError) -> str:
    return exc.response["Error"]["Code"]


@pytest.mark.aws
class TestAgainstAws:
    """§11.3 step 3: refusals must come from AWS, not from our code."""

    def test_read_cannot_reach_another_prefix(
        self, real_minter: CredentialMinter, real_env: dict[str, str]
    ) -> None:
        s3 = real_minter.s3("test-subject", Shape.READ, "/racing")
        with pytest.raises(ClientError) as exc:
            s3.get_object(Bucket=real_env["WIKI_BUCKET"], Key="a/private/x.md")
        assert _error_code(exc.value) == "AccessDenied"

    def test_list_cannot_escape_its_prefix(
        self, real_minter: CredentialMinter, real_env: dict[str, str]
    ) -> None:
        s3 = real_minter.s3("test-subject", Shape.LIST, "/racing")
        with pytest.raises(ClientError) as exc:
            s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"])
        assert _error_code(exc.value) == "AccessDenied"
        with pytest.raises(ClientError) as exc:
            s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"], Prefix="a/")
        assert _error_code(exc.value) == "AccessDenied"
        with pytest.raises(ClientError) as exc:
            s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"], Prefix="a/private/")
        assert _error_code(exc.value) == "AccessDenied"

    def test_list_within_its_prefix_is_allowed(
        self, real_minter: CredentialMinter, real_env: dict[str, str]
    ) -> None:
        # Positive control: a broken role would pass every negative test for free.
        s3 = real_minter.s3("test-subject", Shape.LIST, "/racing")
        out = s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"], Prefix="a/racing/")
        assert out["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_read_cannot_list_at_all(
        self, real_minter: CredentialMinter, real_env: dict[str, str]
    ) -> None:
        s3 = real_minter.s3("test-subject", Shape.READ, "/racing")
        with pytest.raises(ClientError) as exc:
            s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"], Prefix="a/racing/")
        assert _error_code(exc.value) == "AccessDenied"
        with pytest.raises(ClientError) as exc:
            s3.list_objects_v2(Bucket=real_env["WIKI_BUCKET"])
        assert _error_code(exc.value) == "AccessDenied"

    def test_read_cannot_write(
        self, real_minter: CredentialMinter, real_env: dict[str, str]
    ) -> None:
        s3 = real_minter.s3("test-subject", Shape.READ, "/racing")
        with pytest.raises(ClientError) as exc:
            s3.put_object(Bucket=real_env["WIKI_BUCKET"], Key="a/racing/probe.md", Body=b"x")
        assert _error_code(exc.value) == "AccessDenied"


# --- the MAINTAIN shape (increment A, §8.6 / §8.9) ----------------------------------------


class TestMaintainShape:
    """WRITE + LIST over the folder, plus ``s3:PutObjectTagging`` on that folder's
    ``_listing.json`` key only — a tagged ``PutObject`` needs the tagging permission."""

    def test_exact_action_set(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        assert _actions(pol) == {
            "s3:GetObject",
            "s3:GetObjectVersion",
            "s3:PutObject",
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "s3:ListBucket",
            "s3:ListBucketVersions",
            "s3:PutObjectTagging",
        }

    def test_object_statement_is_the_folder_prefix(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        st = _by_action(pol, "s3:PutObject")
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/*"
        assert set(st["Action"]) == {"s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"}

    def test_list_bucket_carries_the_prefix_condition(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        st = _by_action(pol, "s3:ListBucket")
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}"
        assert st["Condition"] == {"StringLike": {"s3:prefix": ["a/racing/*"]}}

    def test_tagging_is_on_the_listing_key_only(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        st = _by_action(pol, "s3:PutObjectTagging")
        assert st["Action"] == ["s3:PutObjectTagging"]
        assert st["Resource"] == f"arn:aws:s3:::{BUCKET}/a/racing/_listing.json"
        assert "*" not in st["Resource"]

    def test_root(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/")
        assert _by_action(pol, "s3:PutObject")["Resource"] == f"arn:aws:s3:::{BUCKET}/a/*"
        assert _by_action(pol, "s3:ListBucket")["Condition"]["StringLike"] == {"s3:prefix": ["a/*"]}
        assert (
            _by_action(pol, "s3:PutObjectTagging")["Resource"]
            == f"arn:aws:s3:::{BUCKET}/a/_listing.json"
        )

    def test_article_path_means_its_parent_folder(self) -> None:
        assert session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing/x.md") == session_policy(
            Shape.MAINTAIN, BUCKET, KMS, "/racing"
        )
        assert session_policy(Shape.MAINTAIN, BUCKET, KMS, "/x.md") == session_policy(
            Shape.MAINTAIN, BUCKET, KMS, "/"
        )

    def test_no_tagging_in_the_other_shapes(self) -> None:
        for shape in (Shape.READ, Shape.LIST, Shape.WRITE):
            assert "Tagging" not in _compact(session_policy(shape, BUCKET, KMS, "/racing"))

    def test_kms_generate_data_key(self) -> None:
        pol = session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        st = _by_action(pol, "kms:GenerateDataKey")
        assert set(st["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey"}
        assert st["Resource"] == KMS

    def test_listing_key_helper(self) -> None:
        assert listing_key("/") == "a/_listing.json"
        assert listing_key("/racing") == "a/racing/_listing.json"
        assert listing_key("/racing/setup/rear-bar.md") == "a/racing/setup/_listing.json"

    def test_size_bound_for_long_folder_path(self) -> None:
        # MAINTAIN names the folder three times (object resource, prefix condition,
        # listing key), so it fits less depth than LIST. Pin what it does support:
        # far beyond any folder a person would create.
        path = "/" + "/".join(["d" * 20] * 21) + "/" + "x" * 8
        assert len(path) == 450
        assert len(_compact(session_policy(Shape.MAINTAIN, BUCKET, KMS, path))) < 2048

    def test_minter_uses_the_shape(self, minter: CredentialMinter, sts: FakeSts) -> None:
        minter.mint("user_01", Shape.MAINTAIN, "/racing")
        pol = json.loads(sts.calls[0]["Policy"])
        assert pol == session_policy(Shape.MAINTAIN, BUCKET, KMS, "/racing")
        assert "s3:PutObjectTagging" in _actions(pol)
        minter.mint("user_01", Shape.WRITE, "/racing")
        assert minter.mint_count == 2  # a different shape is a different credential
