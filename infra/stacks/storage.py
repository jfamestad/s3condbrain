"""Storage stack — HANDOFF §8.2, §8.8, §8.9, build step 1.

Creates: KMS key (rotation on) · bucket (versioned, SSE-KMS with that key, public access
blocked, TLS enforced, Object Lock prod-only with 1-year governance default retention,
lifecycle expiring non-current listing versions once retention lapses) · grant table
(pk/sk, GSI ``gs1`` on gs1pk/gs1sk, pay-per-request, SSE-KMS with the same key, PITR) ·
rate-limit table ``wiki-<env>-ratelimit`` (pk/sk, TTL on ``ttl``, same key, no PITR —
it holds fixed-window counters and nothing worth restoring, §12.6).

The storage role — the outer bound the session policies narrow from — lives in
``ComputeStack`` because its trust policy names the MCP function's role, and a
key-policy statement pointing back at it from here would be a cyclic cross-stack
reference. The key's default policy delegates to IAM for this account, so the role's
own inline policy is sufficient (§8.8).

The CloudWatch alarm on bucket-policy change (§8.8), the trail, and backups live in
``OpsStack``; the MCP role's ``UpdateItem`` on the rate-limit table is granted in
``ComputeStack``.

Exposes ``key``, ``bucket``, ``table``, ``ratelimit_table``.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import EnvConfig

#: Object Lock default retention in production (§8.9 — "one year is the decided period").
OBJECT_LOCK_RETENTION_DAYS = 365

#: Non-current ``_listing.json`` versions are expired after this many days. Must be
#: >= ``OBJECT_LOCK_RETENTION_DAYS`` so the lifecycle rule never fights the lock.
LISTING_NONCURRENT_EXPIRY_DAYS = 365

#: Tag the data plane sets on listing writes (increment A) so the lifecycle rule can
#: single them out without touching article history.
LISTING_TAG_KEY = "wiki:listing"
LISTING_TAG_VALUE = "true"

#: DynamoDB TTL attribute on the rate-limit table (``app.auth.ratelimit`` writes it as
#: epoch seconds on every counter row).
RATELIMIT_TTL_ATTRIBUTE = "ttl"


class StorageStack(cdk.Stack):
    """Bucket, grant table, and the one customer-managed key that covers both."""

    def __init__(self, scope: Construct, id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        self._removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY

        self.key = self._create_key()
        self.bucket = self._create_bucket()
        self.table = self._create_table()
        self.ratelimit_table = self._create_ratelimit_table()

        cdk.CfnOutput(self, "BucketName", value=self.bucket.bucket_name)
        cdk.CfnOutput(self, "GrantTableName", value=self.table.table_name)
        cdk.CfnOutput(self, "RateLimitTableName", value=self.ratelimit_table.table_name)
        cdk.CfnOutput(self, "KmsKeyArn", value=self.key.key_arn)

    def _create_key(self) -> kms.Key:
        return kms.Key(
            self,
            "Key",
            description=f"wiki-{self.cfg.name}: bucket and grant table (HANDOFF §8.8)",
            alias=f"alias/wiki-{self.cfg.name}",
            enable_key_rotation=True,
            removal_policy=self._removal,
        )

    def _create_bucket(self) -> s3.Bucket:
        lock = self.cfg.object_lock
        if lock and LISTING_NONCURRENT_EXPIRY_DAYS < OBJECT_LOCK_RETENTION_DAYS:
            raise ValueError("listing expiry must not be shorter than Object Lock retention")

        lifecycle = s3.LifecycleRule(
            id="expire-noncurrent-listings",
            enabled=True,
            tag_filters={LISTING_TAG_KEY: LISTING_TAG_VALUE},
            noncurrent_version_expiration=Duration.days(LISTING_NONCURRENT_EXPIRY_DAYS),
        )

        bucket = s3.Bucket(
            self,
            "Bucket",
            versioned=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            object_lock_enabled=True if lock else None,
            object_lock_default_retention=(
                s3.ObjectLockRetention.governance(Duration.days(OBJECT_LOCK_RETENTION_DAYS))
                if lock
                else None
            ),
            lifecycle_rules=[lifecycle],
            removal_policy=self._removal,
            auto_delete_objects=not self.cfg.retain_data,
        )
        # §12.3: the bucket answers to nothing outside this account. Public access
        # blocking stops anonymous reads; this stops a principal in another account
        # that a mistaken bucket policy or ACL might one day admit. Service principals
        # (auto-delete custom resource, CloudTrail) are all in-account.
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="DenyOtherAccounts",
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["s3:*"],
                resources=[bucket.bucket_arn, bucket.arn_for_objects("*")],
                conditions={"StringNotEquals": {"aws:PrincipalAccount": self.account}},
            )
        )
        return bucket

    def _create_table(self) -> dynamodb.Table:
        table = dynamodb.Table(
            self,
            "Grants",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            # RETAIN only guards a CloudFormation delete; this guards a direct
            # DeleteTable by any principal. Off in dev so `make destroy` still works.
            deletion_protection=self.cfg.retain_data,
            removal_policy=self._removal,
        )
        table.add_global_secondary_index(
            index_name="gs1",
            partition_key=dynamodb.Attribute(name="gs1pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gs1sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        return table

    def _create_ratelimit_table(self) -> dynamodb.Table:
        """Fixed-window counters (§12.6): ``pk = RL#<subject>``, ``sk = <window>``.

        Its own table so the MCP role's ``UpdateItem`` never touches the grant table
        (§4.7, §8.8). Rows expire through ``ttl``; nothing here is backed up.
        """
        return dynamodb.Table(
            self,
            "RateLimit",
            table_name=f"wiki-{self.cfg.name}-ratelimit",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.key,
            time_to_live_attribute=RATELIMIT_TTL_ATTRIBUTE,
            removal_policy=self._removal,
        )
