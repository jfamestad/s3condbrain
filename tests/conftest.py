"""Shared fixtures. Workers add test-local fixtures in their own modules or in a
conftest.py inside their own subdirectory — not here."""

from __future__ import annotations

import os
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from app.config import Settings

TEST_ENV = {
    "AWS_DEFAULT_REGION": "us-west-2",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "WIKI_BUCKET": "wiki-test",
    "GRANT_TABLE": "wiki-grants-test",
    "STORAGE_ROLE_ARN": "arn:aws:iam::123456789012:role/wiki-storage",
    "KMS_KEY_ARN": "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000",
    "AUTHKIT_DOMAIN": "https://test.authkit.app",
    "CANONICAL_MCP_URL": "https://wiki-dev.famestad.com/mcp",
    "ALLOWED_ORIGINS": "https://claude.ai",
    "MCP_STRICT_HEADERS": "false",
}


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture
def aws() -> Iterator[None]:
    """Moto for every AWS client created inside the test."""
    with mock_aws():
        yield


@pytest.fixture
def bucket(aws: None, settings: Settings):
    s3 = boto3.client("s3", region_name=os.environ["AWS_DEFAULT_REGION"])
    s3.create_bucket(
        Bucket=settings.bucket,
        CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_DEFAULT_REGION"]},
    )
    s3.put_bucket_versioning(
        Bucket=settings.bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    return s3


@pytest.fixture
def grant_table(aws: None, settings: Settings):
    ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    table = ddb.create_table(
        TableName=settings.grant_table,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gs1pk", "AttributeType": "S"},
            {"AttributeName": "gs1sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gs1",
                "KeySchema": [
                    {"AttributeName": "gs1pk", "KeyType": "HASH"},
                    {"AttributeName": "gs1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    table.wait_until_exists()
    return table
