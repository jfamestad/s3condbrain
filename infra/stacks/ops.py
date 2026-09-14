"""Ops stack — HANDOFF §8.8, §12.5, §12.7, increment G.

Everything here watches or copies; nothing here serves a request.

* **Alerts topic** ``wiki-<env>-alerts`` — every alarm and rule below publishes to it.
  An email subscription is attached when context ``alertEmail`` is given. The topic
  is not KMS-encrypted on purpose: messages carry resource names and metric values,
  never article content, and an encrypted topic needs key-policy grants for three
  more service principals. Its resource policy lets ``events.amazonaws.com`` publish
  **only on behalf of this stack's rules** (``aws:SourceArn`` lists their ARNs) — the
  CDK ``SnsTopic`` target would grant the service principal unconditionally, which
  is any rule in any account, so the rules bind through ``_AlertsTarget`` instead.
  The CloudWatch alarms need no statement: same-account alarm actions are allowed
  without one, and none is written.
* **CloudTrail** — one multi-region trail: management events (all), **S3 data events
  for the wiki bucket, write-only** (§12.7 — "what touched the bucket outside the
  application"), log-file validation on. Delivered to its own log bucket (SSE-KMS with
  the storage key, public access blocked, TLS enforced, expires after
  ``cfg.log_retention_days``) and to a CloudWatch log group with the same retention.
  CDK's ``Trail`` only names the key; the two statements CloudTrail needs on the key
  policy (``GenerateDataKey*`` under its encryption context, ``DescribeKey``) are
  added here to the storage stack's key.
* **Bucket-policy watch** — an EventBridge rule on the CloudTrail management events
  that change a bucket's policy, ACL, public-access block, versioning, Object Lock,
  encryption or lifecycle, for any bucket in the account (§8.8: "an alarm on any
  bucket policy change or public grant"). The account-level public-access-block calls
  are included.
* **Alarms** (§12.7) — API ``5XXError`` ≥ 1 / 5 min; authorizer ``Errors`` ≥ 20 / 5 min
  (every rejection is a raise, so this is the authentication-failure spike); MCP
  ``Errors`` ≥ 1 / 5 min; MCP ``Throttles`` ≥ 1 / 5 min; a metric filter on the MCP
  log group for ``listing_refresh_failed`` / ``listing_rebuilt_unpersisted`` with an
  alarm at ≥ 5 / 15 min.
* **Break-glass role** (§8.8, §8.9) — ``wiki-<env>-break-glass``: the only principal
  holding ``s3:DeleteObjectVersion`` and ``s3:BypassGovernanceRetention`` on the wiki
  bucket. Trusted by this account's IAM (so an administrator's SSO session can switch
  to it in the console), one-hour sessions, never attached to a function. An
  EventBridge rule alerts on every ``AssumeRole`` of it; every delete it performs is
  a write data event on the trail.
* **AWS Backup** (§12.5) — a vault encrypted with the storage key, a plan taking a
  daily snapshot of the grant table kept ``cfg.backup_retention_days`` days. The
  bucket's backup is its own versioning plus Object Lock; the rate-limit table holds
  nothing worth keeping. DynamoDB backups cannot be shared to another account, so
  "alarm on any snapshot shared beyond this account" has nothing to watch here
  (see ``docs/RUNBOOK.md``). AWS Config is out of scope.

Depends on ``StorageStack`` (bucket, key, table), ``ComputeStack`` (the two functions
and their log groups) and ``ApiStack`` (the REST API name). Nothing depends on this
stack, so it can be added or torn down independently.

Outputs: AlertsTopicArn, TrailArn, TrailLogBucketName, BackupVaultName, BreakGlassRoleArn.
"""

from __future__ import annotations

import aws_cdk as cdk
import jsii
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_backup as backup
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.storage import StorageStack

#: CloudTrail management events that change who can reach a bucket or what can be
#: deleted from it. ``PutAccountPublicAccessBlock`` / ``DeleteAccountPublicAccessBlock``
#: are the account-level S3 Control calls; they also arrive with ``s3.amazonaws.com``
#: as the event source.
BUCKET_GUARD_EVENTS: tuple[str, ...] = (
    "PutBucketPolicy",
    "DeleteBucketPolicy",
    "PutBucketAcl",
    "PutBucketPublicAccessBlock",
    "DeleteBucketPublicAccessBlock",
    "PutAccountPublicAccessBlock",
    "DeleteAccountPublicAccessBlock",
    "PutBucketVersioning",
    "PutObjectLockConfiguration",
    "PutBucketEncryption",
    "DeleteBucketEncryption",
    "PutBucketLifecycle",
    "DeleteBucketLifecycle",
    "DeleteBucket",
)

#: Log lines from the listing projection (increment A) that mean the cache could not
#: be refreshed or persisted. The data plane still answers from a live rebuild, so a
#: burst is a warning, not an outage; one or two is normal contention.
LISTING_FAILURE_TERMS: tuple[str, ...] = ("listing_refresh_failed", "listing_rebuilt_unpersisted")

ALARM_PERIOD = Duration.minutes(5)
API_5XX_THRESHOLD = 1
AUTHORIZER_ERROR_THRESHOLD = 20
MCP_ERROR_THRESHOLD = 1
MCP_THROTTLE_THRESHOLD = 1
LISTING_FAILURE_PERIOD = Duration.minutes(15)
LISTING_FAILURE_THRESHOLD = 5

#: When the daily grant-table snapshot starts (UTC). Chosen for a quiet hour in
#: US-Pacific; the table is tiny so the window barely matters.
BACKUP_HOUR_UTC = "09"

_RETENTION_BY_DAYS: dict[int, logs.RetentionDays] = {
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    120: logs.RetentionDays.FOUR_MONTHS,
    150: logs.RetentionDays.FIVE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
}


def _retention(days: int) -> logs.RetentionDays:
    """Map a day count to the CloudWatch Logs enum; only the values Logs offers."""
    try:
        return _RETENTION_BY_DAYS[days]
    except KeyError as e:
        raise ValueError(
            f"log_retention_days={days} is not a CloudWatch Logs retention value; "
            f"expected one of {sorted(_RETENTION_BY_DAYS)}"
        ) from e


@jsii.implements(events.IRuleTarget)
class _AlertsTarget:
    """An SNS rule target that grants nothing.

    ``aws_events_targets.SnsTopic.bind`` calls ``topic.grant_publish`` for the bare
    ``events.amazonaws.com`` principal — no ``aws:SourceArn``, so every EventBridge
    rule anywhere could publish here. This target only wires the ARN; the stack writes
    the one conditioned statement itself once every rule exists
    (``OpsStack._restrict_alerts_publishers``).
    """

    def __init__(self, topic: sns.ITopic) -> None:
        self._topic = topic

    def bind(self, rule: events.IRule, id: str | None = None) -> events.RuleTargetConfig:
        del rule, id
        return events.RuleTargetConfig(arn=self._topic.topic_arn, target_resource=self._topic)


class OpsStack(cdk.Stack):
    """Trail, alerts, alarms, bucket-policy watch, and the grant-table backup plan."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        compute: ComputeStack,
        api: ApiStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        self.storage = storage
        self.compute = compute
        self.api = api
        self._removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY
        self._retention = _retention(cfg.log_retention_days)

        self.alerts = self._create_alerts_topic()
        self.trail_bucket = self._create_trail_bucket()
        self.trail_log_group = self._create_trail_log_group()
        self._grant_trail_key_use()
        self.trail = self._create_trail()
        self.bucket_guard = self._create_bucket_guard_rule()
        self.alarms = self._create_alarms()
        self.break_glass_role = self._create_break_glass_role()
        self.break_glass_watch = self._create_break_glass_rule()
        self._restrict_alerts_publishers([self.bucket_guard, self.break_glass_watch])
        self.backup_vault = self._create_backup_vault()
        self.backup_plan = self._create_backup_plan()

        cdk.CfnOutput(self, "AlertsTopicArn", value=self.alerts.topic_arn)
        cdk.CfnOutput(self, "TrailArn", value=self.trail.trail_arn)
        cdk.CfnOutput(self, "TrailLogBucketName", value=self.trail_bucket.bucket_name)
        cdk.CfnOutput(self, "BackupVaultName", value=self.backup_vault.backup_vault_name)
        cdk.CfnOutput(self, "BreakGlassRoleArn", value=self.break_glass_role.role_arn)

    # ---------------------------------------------------------------- alerts

    def _create_alerts_topic(self) -> sns.Topic:
        topic = sns.Topic(
            self,
            "Alerts",
            topic_name=f"wiki-{self.cfg.name}-alerts",
            display_name=f"wiki-{self.cfg.name} alerts",
        )
        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            topic.add_subscription(subscriptions.EmailSubscription(alert_email))
        else:
            cdk.Annotations.of(self).add_warning_v2(
                "wiki:no-alert-email",
                "No `alertEmail` context; the alerts topic has no subscriber. "
                "Pass -c alertEmail=you@example.com or subscribe after deploy.",
            )
        return topic

    def _restrict_alerts_publishers(self, rules: list[events.Rule]) -> None:
        """The topic's one service statement: EventBridge may publish, for these rules.

        ``aws:SourceArn`` is the rule ARN EventBridge presents when it delivers; a rule
        in another account (or another stack here) is not on the list and is refused.
        ``aws:SourceAccount`` is belt and braces for the same thing.
        """
        self.alerts.add_to_resource_policy(
            iam.PolicyStatement(
                sid="EventBridgeRulesInThisStack",
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.alerts.topic_arn],
                conditions={
                    "ArnEquals": {"aws:SourceArn": [rule.rule_arn for rule in rules]},
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            )
        )

    # ----------------------------------------------------------------- trail

    def _create_trail_bucket(self) -> s3.Bucket:
        return s3.Bucket(
            self,
            "TrailBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.storage.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-trail-logs",
                    enabled=True,
                    expiration=Duration.days(self.cfg.log_retention_days),
                )
            ],
            removal_policy=self._removal,
            auto_delete_objects=not self.cfg.retain_data,
        )

    def _create_trail_log_group(self) -> logs.LogGroup:
        return logs.LogGroup(
            self,
            "TrailLogs",
            retention=self._retention,
            removal_policy=self._removal,
        )

    @property
    def _trail_name(self) -> str:
        return f"wiki-{self.cfg.name}"

    def _grant_trail_key_use(self) -> None:
        """Key-policy statements CloudTrail needs to write SSE-KMS log files.

        Documented shape (CloudTrail user guide, "Configure AWS KMS key policies"):
        ``GenerateDataKey*`` for the service principal, pinned to this trail by
        ``aws:SourceArn`` and to CloudTrail's encryption context; ``DescribeKey`` so the
        trail can be created. Readers decrypt through the key's IAM delegation.
        """
        key = self.storage.key
        trail_arn = cdk.Stack.of(key).format_arn(
            service="cloudtrail", resource="trail", resource_name=self._trail_name
        )
        any_trail_here = cdk.Stack.of(key).format_arn(
            service="cloudtrail", region="*", resource="trail", resource_name="*"
        )
        cloudtrail_principal = iam.ServicePrincipal("cloudtrail.amazonaws.com")
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailToEncryptLogs",
                principals=[cloudtrail_principal],
                actions=["kms:GenerateDataKey*"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:SourceArn": trail_arn},
                    "StringLike": {"kms:EncryptionContext:aws:cloudtrail:arn": any_trail_here},
                },
            )
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudTrailToDescribeKey",
                principals=[cloudtrail_principal],
                actions=["kms:DescribeKey"],
                resources=["*"],
            )
        )

    def _create_trail(self) -> cloudtrail.Trail:
        trail = cloudtrail.Trail(
            self,
            "Trail",
            trail_name=self._trail_name,
            bucket=self.trail_bucket,
            encryption_key=self.storage.key,
            enable_file_validation=True,
            is_multi_region_trail=True,
            include_global_service_events=True,
            management_events=cloudtrail.ReadWriteType.ALL,
            send_to_cloud_watch_logs=True,
            cloud_watch_log_group=self.trail_log_group,
        )
        # §12.7: write operations only. Reads at S3 level cost more than they reveal;
        # the application log already records every read and denial.
        trail.add_s3_event_selector(
            [cloudtrail.S3EventSelector(bucket=self.storage.bucket)],
            read_write_type=cloudtrail.ReadWriteType.WRITE_ONLY,
            include_management_events=False,
        )
        return trail

    # ---------------------------------------------------------- bucket guard

    def _create_bucket_guard_rule(self) -> events.Rule:
        rule = events.Rule(
            self,
            "BucketGuard",
            rule_name=f"wiki-{self.cfg.name}-bucket-guard",
            description="S3 bucket policy / public-access / retention changes (HANDOFF §8.8)",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["s3.amazonaws.com"],
                    "eventName": list(BUCKET_GUARD_EVENTS),
                },
            ),
        )
        rule.add_target(_AlertsTarget(self.alerts))
        return rule

    # ---------------------------------------------------------------- alarms

    def _alarm(
        self,
        id: str,
        *,
        metric: cloudwatch.IMetric,
        threshold: int,
        description: str,
        period_note: str,
    ) -> cloudwatch.Alarm:
        alarm = cloudwatch.Alarm(
            self,
            id,
            alarm_name=f"wiki-{self.cfg.name}-{id.lower()}",
            alarm_description=f"{description} ({period_note}). See docs/RUNBOOK.md.",
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.alerts))
        return alarm

    def _create_alarms(self) -> dict[str, cloudwatch.Alarm]:
        api = self.api.api
        authorizer = self.compute.authorizer_fn
        mcp = self.compute.mcp_fn

        listing_failures = logs.MetricFilter(
            self,
            "ListingFailureFilter",
            log_group=mcp.log_group,
            filter_pattern=logs.FilterPattern.any_term(*LISTING_FAILURE_TERMS),
            metric_namespace=f"wiki/{self.cfg.name}",
            metric_name="ListingRebuildFailures",
            metric_value="1",
            default_value=0,
        )

        return {
            "api_5xx": self._alarm(
                "Api5xx",
                metric=api.metric_server_error(period=ALARM_PERIOD, statistic="Sum"),
                threshold=API_5XX_THRESHOLD,
                description="API Gateway returned a 5xx",
                period_note=f"sum >= {API_5XX_THRESHOLD} over 5 min",
            ),
            "auth_failures": self._alarm(
                "AuthFailures",
                metric=authorizer.metric_errors(period=ALARM_PERIOD, statistic="Sum"),
                threshold=AUTHORIZER_ERROR_THRESHOLD,
                description="Authentication-failure spike: authorizer rejections",
                period_note=f"sum >= {AUTHORIZER_ERROR_THRESHOLD} over 5 min",
            ),
            "mcp_errors": self._alarm(
                "McpErrors",
                metric=mcp.metric_errors(period=ALARM_PERIOD, statistic="Sum"),
                threshold=MCP_ERROR_THRESHOLD,
                description="MCP function invocation errors",
                period_note=f"sum >= {MCP_ERROR_THRESHOLD} over 5 min",
            ),
            "mcp_throttles": self._alarm(
                "McpThrottles",
                metric=mcp.metric_throttles(period=ALARM_PERIOD, statistic="Sum"),
                threshold=MCP_THROTTLE_THRESHOLD,
                description="MCP function throttled by Lambda concurrency",
                period_note=f"sum >= {MCP_THROTTLE_THRESHOLD} over 5 min",
            ),
            "listing_failures": self._alarm(
                "ListingFailures",
                metric=listing_failures.metric(period=LISTING_FAILURE_PERIOD, statistic="Sum"),
                threshold=LISTING_FAILURE_THRESHOLD,
                description="Listing projection could not be refreshed or persisted",
                period_note=f"sum >= {LISTING_FAILURE_THRESHOLD} over 15 min",
            ),
        }

    # ----------------------------------------------------------- break glass

    @property
    def _break_glass_role_name(self) -> str:
        return f"wiki-{self.cfg.name}-break-glass"

    def _create_break_glass_role(self) -> iam.Role:
        """Hard delete and Object Lock bypass, for a person in the console (§8.8).

        Trusts the account's own IAM so that an administrator (SSO permission set or
        IAM principal with ``sts:AssumeRole`` on this ARN) can switch to it; nothing
        that runs code is ever granted that. Scoped to article keys and the one key.
        The procedure is ``docs/RUNBOOK.md`` "Hard delete".
        """
        bucket = self.storage.bucket
        policy = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    sid="HardDeleteArticleVersions",
                    actions=[
                        "s3:DeleteObjectVersion",
                        "s3:BypassGovernanceRetention",
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "s3:GetObjectRetention",
                        "s3:PutObjectRetention",
                    ],
                    resources=[bucket.arn_for_objects("a/*")],
                ),
                iam.PolicyStatement(
                    sid="ListVersionsWithinArticles",
                    actions=["s3:ListBucket", "s3:ListBucketVersions"],
                    resources=[bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": ["a/*"]}},
                ),
                iam.PolicyStatement(
                    sid="KeyUse",
                    actions=["kms:Decrypt", "kms:DescribeKey"],
                    resources=[self.storage.key.key_arn],
                ),
            ]
        )
        return iam.Role(
            self,
            "BreakGlass",
            role_name=self._break_glass_role_name,
            assumed_by=iam.AccountRootPrincipal(),
            max_session_duration=Duration.hours(1),
            description="wiki break-glass: hard delete + Object Lock bypass, humans only (§8.8)",
            inline_policies={"HardDelete": policy},
        )

    def _create_break_glass_rule(self) -> events.Rule:
        rule = events.Rule(
            self,
            "BreakGlassUsed",
            rule_name=f"wiki-{self.cfg.name}-break-glass-used",
            description="Someone assumed the break-glass role (HANDOFF §8.8)",
            event_pattern=events.EventPattern(
                source=["aws.sts"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["sts.amazonaws.com"],
                    "eventName": ["AssumeRole"],
                    "requestParameters": {"roleArn": [self.break_glass_role.role_arn]},
                },
            ),
        )
        rule.add_target(_AlertsTarget(self.alerts))
        return rule

    # ---------------------------------------------------------------- backup

    def _create_backup_vault(self) -> backup.BackupVault:
        return backup.BackupVault(
            self,
            "Vault",
            backup_vault_name=f"wiki-{self.cfg.name}-grants",
            encryption_key=self.storage.key,
            removal_policy=self._removal,
        )

    def _create_backup_plan(self) -> backup.BackupPlan:
        plan = backup.BackupPlan(
            self,
            "Plan",
            backup_plan_name=f"wiki-{self.cfg.name}-grants-daily",
            backup_vault=self.backup_vault,
            backup_plan_rules=[
                backup.BackupPlanRule(
                    rule_name="daily",
                    backup_vault=self.backup_vault,
                    schedule_expression=events.Schedule.cron(hour=BACKUP_HOUR_UTC, minute="0"),
                    start_window=Duration.hours(1),
                    completion_window=Duration.hours(3),
                    delete_after=Duration.days(self.cfg.backup_retention_days),
                )
            ],
        )
        plan.add_selection(
            "Grants",
            backup_selection_name="grant-table",
            resources=[backup.BackupResource.from_dynamo_db_table(self.storage.table)],
            allow_restores=True,
        )
        return plan
