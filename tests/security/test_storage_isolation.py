"""§12.8 item 4 — storage answers to nothing but the storage role — and the
automatable part of item 7 (account-level public access blocking, encryption).

What this file can prove from the operator's laptop, honestly:

* Anonymous requests are refused (an UNSIGNED client, no credentials at all).
* The bucket is not public by policy and every public-access block is on, at the
  bucket and at the account.
* The bucket policy contains a Deny for every principal outside this account.
* Bucket and table are encrypted with a customer-managed KMS key.
* No IAM user — directly or through a group — holds a policy that names the bucket
  or the table.

What it cannot: "GetObject from a role in another account" needs a second account,
and "GetItem from any principal other than the two application roles" cannot be
asserted from a principal that *is* allowed. The README says so. DynamoDB does not
accept unsigned requests at all, so an anonymous GetItem would prove nothing.

Ambient credentials come from ``aws_session`` (see conftest.py); the anonymous
client is built with ``botocore.UNSIGNED`` and never sees them.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore import UNSIGNED
from botocore.client import Config
from botocore.exceptions import ClientError

pytestmark = pytest.mark.security

PROBE_KEY = "a/probe.md"
DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "403", "Forbidden"})
PRINCIPAL_CONDITION_KEYS = (
    "aws:principalaccount",
    "aws:principalarn",
    "aws:principalorgid",
    "aws:principalorgpaths",
)


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _http_status(exc: ClientError) -> int:
    return int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))


def _assert_access_denied(exc: ClientError) -> None:
    code, status = _error_code(exc), _http_status(exc)
    assert code in DENIED_CODES or status == 403, (
        f"expected AccessDenied, got {code} (HTTP {status}) — a 404 here would mean the "
        "bucket name is wrong or, worse, that anonymous callers can tell keys apart"
    )


def _skip_if_denied(exc: ClientError, operation: str) -> None:
    """The operator's role may lack a read-only permission this test needs. That is
    a skip with the permission named, not a pass and not a failure."""
    if _error_code(exc) in DENIED_CODES or _http_status(exc) == 403:
        pytest.skip(f"ambient credentials lack {operation}: {exc}")
    raise exc


# ------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def anon_s3(region: str) -> Iterator[Any]:
    client = boto3.client("s3", region_name=region, config=Config(signature_version=UNSIGNED))
    yield client
    client.close()


@pytest.fixture(scope="module")
def s3(aws_session: boto3.Session) -> Any:
    return aws_session.client("s3")


@pytest.fixture(scope="module")
def dynamodb(aws_session: boto3.Session) -> Any:
    return aws_session.client("dynamodb")


@pytest.fixture(scope="module")
def iam(aws_session: boto3.Session) -> Any:
    return aws_session.client("iam")


@pytest.fixture(scope="module")
def kms(aws_session: boto3.Session) -> Any:
    return aws_session.client("kms")


# ------------------------------------------------------------ anonymous access


def test_anonymous_get_object_denied(anon_s3: Any, bucket_name: str) -> None:
    with pytest.raises(ClientError) as info:
        anon_s3.get_object(Bucket=bucket_name, Key=PROBE_KEY)
    _assert_access_denied(info.value)


def test_anonymous_list_denied(anon_s3: Any, bucket_name: str) -> None:
    """The failure mode of a leaky ListBucket is a disclosure, not an error (§11.3
    step 3), so this asserts refusal — not an empty page."""
    with pytest.raises(ClientError) as info:
        anon_s3.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
    _assert_access_denied(info.value)


# ------------------------------------------------------------ bucket posture


def test_bucket_policy_status_not_public(s3: Any, bucket_name: str) -> None:
    try:
        status = s3.get_bucket_policy_status(Bucket=bucket_name)["PolicyStatus"]
    except ClientError as exc:
        if _error_code(exc) == "NoSuchBucketPolicy":
            pytest.fail("bucket has no policy; §12.3 requires one denying foreign principals")
        _skip_if_denied(exc, "s3:GetBucketPolicyStatus")
        raise
    assert status["IsPublic"] is False, status


def test_bucket_policy_denies_foreign_principals(s3: Any, bucket_name: str) -> None:
    """§12.3: "a bucket policy … that deny every principal not in this account".
    Concretely: a Deny statement over Principal ``*`` conditioned on the caller's
    account, ARN or organisation."""
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket_name)["Policy"])
    except ClientError as exc:
        if _error_code(exc) == "NoSuchBucketPolicy":
            pytest.fail("bucket has no policy; §12.3 requires one denying foreign principals")
        _skip_if_denied(exc, "s3:GetBucketPolicy")
        raise
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    assert any(_is_deny_foreign(s) for s in statements), (
        "no Deny-all-but-this-account statement found in the bucket policy: "
        f"{json.dumps(policy)[:800]}"
    )


def _is_deny_foreign(statement: dict[str, Any]) -> bool:
    if statement.get("Effect") != "Deny":
        return False
    principal = statement.get("Principal")
    if not (principal == "*" or (isinstance(principal, dict) and principal.get("AWS") == "*")):
        return False
    condition = json.dumps(statement.get("Condition", {})).lower()
    return any(key in condition for key in PRINCIPAL_CONDITION_KEYS)


def test_bucket_public_access_block_all_on(s3: Any, bucket_name: str) -> None:
    try:
        config = s3.get_public_access_block(Bucket=bucket_name)["PublicAccessBlockConfiguration"]
    except ClientError as exc:
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            pytest.fail("bucket has no public access block configuration")
        _skip_if_denied(exc, "s3:GetBucketPublicAccessBlock")
        raise
    expected = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    assert {k: config.get(k) for k in expected} == expected, config


def test_account_public_access_block_all_on(aws_session: boto3.Session, account_id: str) -> None:
    """§12.8 item 7 / §8.8: public access blocking at the *account* level."""
    s3control = aws_session.client("s3control")
    try:
        config = s3control.get_public_access_block(AccountId=account_id)[
            "PublicAccessBlockConfiguration"
        ]
    except ClientError as exc:
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            pytest.fail("no account-level public access block configuration")
        _skip_if_denied(exc, "s3:GetAccountPublicAccessBlock")
        raise
    assert all(
        config.get(k) is True
        for k in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
    ), config


# ------------------------------------------------------------ encryption (§8.8)


def _bucket_key_id(s3: Any, bucket_name: str) -> str | None:
    try:
        rules = s3.get_bucket_encryption(Bucket=bucket_name)["ServerSideEncryptionConfiguration"][
            "Rules"
        ]
    except ClientError as exc:
        _skip_if_denied(exc, "s3:GetEncryptionConfiguration")
        raise
    for rule in rules:
        default = rule.get("ApplyServerSideEncryptionByDefault", {})
        if default.get("SSEAlgorithm") == "aws:kms":
            return default.get("KMSMasterKeyID")
    return None


def _table_sse(dynamodb: Any, table_name: str) -> dict[str, Any]:
    try:
        table = dynamodb.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        _skip_if_denied(exc, "dynamodb:DescribeTable")
        raise
    return table.get("SSEDescription", {})


def test_bucket_encrypted_with_kms(s3: Any, bucket_name: str) -> None:
    assert _bucket_key_id(s3, bucket_name), "bucket default encryption is not SSE-KMS with a key"


def test_table_sse_is_kms(dynamodb: Any, table_name: str) -> None:
    """GetItem from the operator's own role would be allowed, so it proves nothing;
    what can be asserted is that the table is encrypted under KMS (§8.8)."""
    sse = _table_sse(dynamodb, table_name)
    assert sse.get("SSEType") == "KMS", f"table SSE is not KMS: {sse}"
    assert sse.get("Status") == "ENABLED", sse
    assert sse.get("KMSMasterKeyArn"), sse


def test_storage_keys_are_customer_managed(
    s3: Any, dynamodb: Any, kms: Any, bucket_name: str, table_name: str
) -> None:
    """§8.8: one customer-managed key, not the AWS-managed defaults."""
    bucket_key = _bucket_key_id(s3, bucket_name)
    table_key = _table_sse(dynamodb, table_name).get("KMSMasterKeyArn")
    assert bucket_key and table_key, (bucket_key, table_key)
    for key_id in (bucket_key, table_key):
        try:
            meta = kms.describe_key(KeyId=key_id)["KeyMetadata"]
        except ClientError as exc:
            _skip_if_denied(exc, "kms:DescribeKey")
            raise
        assert meta["KeyManager"] == "CUSTOMER", f"{key_id} is AWS-managed: {meta['KeyManager']}"


# ------------------------------------------------------------ IAM users


def _paginate(client: Any, operation: str, key: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
    for page in client.get_paginator(operation).paginate(**kwargs):
        yield from page[key]


def _attached_policy_documents(iam: Any, arns: list[str]) -> Iterator[tuple[str, str]]:
    for arn in arns:
        version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
        document = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"][
            "Document"
        ]
        yield arn, json.dumps(document)


def _user_policy_documents(iam: Any, user: str) -> Iterator[tuple[str, str]]:
    """Every policy a user holds: inline, attached, and via each group."""
    for name in _paginate(iam, "list_user_policies", "PolicyNames", UserName=user):
        doc = iam.get_user_policy(UserName=user, PolicyName=name)["PolicyDocument"]
        yield f"inline:{name}", json.dumps(doc)
    attached = _paginate(iam, "list_attached_user_policies", "AttachedPolicies", UserName=user)
    yield from _attached_policy_documents(iam, [p["PolicyArn"] for p in attached])
    for group in _paginate(iam, "list_groups_for_user", "Groups", UserName=user):
        group_name = group["GroupName"]
        for name in _paginate(iam, "list_group_policies", "PolicyNames", GroupName=group_name):
            doc = iam.get_group_policy(GroupName=group_name, PolicyName=name)["PolicyDocument"]
            yield f"group:{group_name}/inline:{name}", json.dumps(doc)
        attached = _paginate(
            iam, "list_attached_group_policies", "AttachedPolicies", GroupName=group_name
        )
        yield from _attached_policy_documents(iam, [p["PolicyArn"] for p in attached])


def test_no_iam_user_policy_names_bucket_or_table(
    iam: Any, bucket_name: str, table_name: str
) -> None:
    """§12.3: "no IAM user, anywhere, with a policy naming either". A sweep over every
    user's inline, attached and group policies for the bucket or table name. Zero
    users is the intended state and passes; a wildcard (``Resource: *``) policy
    names neither and is *not* caught — see README."""
    try:
        users = [u["UserName"] for u in _paginate(iam, "list_users", "Users")]
    except ClientError as exc:
        _skip_if_denied(exc, "iam:ListUsers")
        raise

    offenders: list[str] = []
    keyed: list[str] = []
    for user in users:
        try:
            for label, document in _user_policy_documents(iam, user):
                if bucket_name in document or table_name in document:
                    offenders.append(f"{user} via {label}")
            if any(
                k["Status"] == "Active"
                for k in _paginate(iam, "list_access_keys", "AccessKeyMetadata", UserName=user)
            ):
                keyed.append(user)
        except ClientError as exc:
            _skip_if_denied(exc, f"iam read on user {user!r}")
            raise

    if keyed:
        warnings.warn(
            f"IAM users with active access keys (§8.8 says none, anywhere): {keyed}",
            UserWarning,
            stacklevel=1,
        )
    assert not offenders, f"IAM user policies naming the bucket or table: {offenders}"
