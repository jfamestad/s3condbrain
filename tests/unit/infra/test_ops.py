"""Synth-time assertions over the ops stack — HANDOFF §8.8, §12.5, §12.7.

Each one is an operational control that would deploy fine if absent and only be
missed on the day it was needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.config import load
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.ops import BUCKET_GUARD_EVENTS, OpsStack
from infra.stacks.storage import StorageStack

CERT_ARN = "arn:aws:acm:us-west-2:123456789012:certificate/00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Synth:
    storage_stack: StorageStack
    compute_stack: ComputeStack
    api_stack: ApiStack
    ops_stack: OpsStack
    storage: dict[str, Any]
    compute: dict[str, Any]
    ops: dict[str, Any]


def _build_code_root(root: Path) -> Path:
    for name in ("authorizer", "mcp", "web"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "placeholder.py").write_text("# built by make build\n")
    return root


def _synth(env_name: str, code_root: Path, context: dict[str, str] | None = None) -> Synth:
    app = cdk.App(context={"env": env_name, "certificateArn": CERT_ARN, **(context or {})})
    cfg = load(env_name)
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    storage = StorageStack(app, "storage", cfg=cfg, env=aws_env)
    compute = ComputeStack(
        app, "compute", cfg=cfg, storage=storage, code_root=code_root, env=aws_env
    )
    api = ApiStack(app, "api", cfg=cfg, compute=compute, env=aws_env)
    ops = OpsStack(app, "ops", cfg=cfg, storage=storage, compute=compute, api=api, env=aws_env)
    return Synth(
        storage_stack=storage,
        compute_stack=compute,
        api_stack=api,
        ops_stack=ops,
        storage=Template.from_stack(storage).to_json(),
        compute=Template.from_stack(compute).to_json(),
        ops=Template.from_stack(ops).to_json(),
    )


@pytest.fixture(scope="module")
def dev(tmp_path_factory: pytest.TempPathFactory) -> Synth:
    return _synth("dev", _build_code_root(tmp_path_factory.mktemp("build-dev")))


@pytest.fixture(scope="module")
def prod(tmp_path_factory: pytest.TempPathFactory) -> Synth:
    return _synth("prod", _build_code_root(tmp_path_factory.mktemp("build-prod")))


# ------------------------------------------------------------------ helpers


def _resources(template: dict[str, Any], type_: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == type_}


def _only(template: dict[str, Any], type_: str) -> dict[str, Any]:
    found = _resources(template, type_)
    assert len(found) == 1, f"expected exactly one {type_}, found {sorted(found)}"
    return next(iter(found.values()))


def _logical_id(stack: cdk.Stack, construct_id: str) -> str:
    child = stack.node.find_child(construct_id).node.default_child
    assert isinstance(child, cdk.CfnElement)
    return stack.get_logical_id(child)


def _alarms(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        r["Properties"]["AlarmName"]: r["Properties"]
        for r in _resources(template, "AWS::CloudWatch::Alarm").values()
    }


def _is_import_of(value: Any, stack_name: str, fragment: str) -> bool:
    """True when ``value`` is an Fn::ImportValue of an export from ``stack_name``
    whose name mentions ``fragment`` (the producing construct's logical-id stem)."""
    if not isinstance(value, dict) or "Fn::ImportValue" not in value:
        return False
    name = value["Fn::ImportValue"]
    return name.startswith(f"{stack_name}:") and fragment in name


# ------------------------------------------------------------------- trail


def test_trail_logs_bucket_writes_only_with_validation(dev: Synth) -> None:
    trail = _only(dev.ops, "AWS::CloudTrail::Trail")["Properties"]
    assert trail["EnableLogFileValidation"] is True
    assert trail["IsLogging"] is True
    assert trail["IsMultiRegionTrail"] is True
    assert trail["IncludeGlobalServiceEvents"] is True
    assert trail["TrailName"] == "wiki-dev"

    selectors = trail["EventSelectors"]
    [management] = [s for s in selectors if "DataResources" not in s]
    assert management == {"IncludeManagementEvents": True, "ReadWriteType": "All"}

    [data] = [s for s in selectors if "DataResources" in s]
    assert data["ReadWriteType"] == "WriteOnly", "§12.7: S3 data events are write-only"
    assert data["IncludeManagementEvents"] is False
    [resource] = data["DataResources"]
    assert resource["Type"] == "AWS::S3::Object"
    [value] = resource["Values"]
    # arn:...:wiki-bucket + "/" — the whole bucket, nothing else.
    bucket_arn, suffix = value["Fn::Join"][1]
    assert _is_import_of(bucket_arn, "storage", "Bucket")
    assert suffix == "/"


def test_trail_is_encrypted_with_the_storage_key(dev: Synth) -> None:
    trail = _only(dev.ops, "AWS::CloudTrail::Trail")["Properties"]
    assert _is_import_of(trail["KMSKeyId"], "storage", "Key")

    key = dev.storage["Resources"][_logical_id(dev.storage_stack, "Key")]["Properties"]
    by_sid = {s.get("Sid"): s for s in key["KeyPolicy"]["Statement"]}
    encrypt = by_sid["AllowCloudTrailToEncryptLogs"]
    assert encrypt["Principal"] == {"Service": "cloudtrail.amazonaws.com"}
    assert encrypt["Action"] == "kms:GenerateDataKey*"
    assert "aws:SourceArn" in encrypt["Condition"]["StringEquals"]
    assert "kms:EncryptionContext:aws:cloudtrail:arn" in encrypt["Condition"]["StringLike"]
    assert json.dumps(encrypt["Condition"]["StringEquals"]["aws:SourceArn"]).endswith(
        ':trail/wiki-dev"]]}'
    )
    describe = by_sid["AllowCloudTrailToDescribeKey"]
    assert describe["Action"] == "kms:DescribeKey"


def test_trail_bucket_locked_down_and_expiring(dev: Synth, prod: Synth) -> None:
    for synth, deletion in ((dev, "Delete"), (prod, "Retain")):
        resource = _only(synth.ops, "AWS::S3::Bucket")
        bucket = resource["Properties"]
        assert bucket["PublicAccessBlockConfiguration"] == {
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        }
        [rule] = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        assert _is_import_of(
            rule["ServerSideEncryptionByDefault"]["KMSMasterKeyID"], "storage", "Key"
        )
        [lifecycle] = bucket["LifecycleConfiguration"]["Rules"]
        assert lifecycle["ExpirationInDays"] == 90
        assert resource["DeletionPolicy"] == deletion

        policy = _only(synth.ops, "AWS::S3::BucketPolicy")["Properties"]["PolicyDocument"]
        assert any(
            s["Effect"] == "Deny"
            and s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            for s in policy["Statement"]
        )
        assert any(
            s.get("Principal") == {"Service": "cloudtrail.amazonaws.com"}
            and "s3:PutObject" in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
            for s in policy["Statement"]
        )

    assert "Custom::S3AutoDeleteObjects" not in {r["Type"] for r in prod.ops["Resources"].values()}


def test_trail_cloudwatch_log_group_ninety_days(dev: Synth) -> None:
    trail = _only(dev.ops, "AWS::CloudTrail::Trail")["Properties"]
    group_id = trail["CloudWatchLogsLogGroupArn"]["Fn::GetAtt"][0]
    group = dev.ops["Resources"][group_id]
    assert group["Type"] == "AWS::Logs::LogGroup"
    assert group["Properties"]["RetentionInDays"] == 90
    assert "CloudWatchLogsRoleArn" in trail


# ------------------------------------------------------------------ alerts


def test_alerts_topic_and_optional_email(dev: Synth, tmp_path: Path) -> None:
    topic = _only(dev.ops, "AWS::SNS::Topic")["Properties"]
    assert topic["TopicName"] == "wiki-dev-alerts"
    assert not _resources(dev.ops, "AWS::SNS::Subscription")
    warnings = [m.data for m in dev.ops_stack.node.metadata if m.type == "aws:cdk:warning"]
    assert any("alertEmail" in str(w) for w in warnings)

    with_email = _synth("dev", _build_code_root(tmp_path), {"alertEmail": "ops@example.com"})
    sub = _only(with_email.ops, "AWS::SNS::Subscription")["Properties"]
    assert sub["Protocol"] == "email"
    assert sub["Endpoint"] == "ops@example.com"
    assert "Outputs" in with_email.ops and "AlertsTopicArn" in with_email.ops["Outputs"]


def _rule(template: dict[str, Any], name: str) -> dict[str, Any]:
    [rule] = [
        r["Properties"]
        for r in _resources(template, "AWS::Events::Rule").values()
        if r["Properties"]["Name"] == name
    ]
    return rule


def test_bucket_guard_rule_watches_policy_and_public_access_changes(dev: Synth) -> None:
    rule = _rule(dev.ops, "wiki-dev-bucket-guard")
    assert rule["State"] == "ENABLED"
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.s3"]
    assert pattern["detail-type"] == ["AWS API Call via CloudTrail"]
    assert pattern["detail"]["eventSource"] == ["s3.amazonaws.com"]
    names = set(pattern["detail"]["eventName"])
    assert names == set(BUCKET_GUARD_EVENTS)
    assert {
        "PutBucketPolicy",
        "DeleteBucketPolicy",
        "PutBucketAcl",
        "PutBucketPublicAccessBlock",
        "DeleteBucketPublicAccessBlock",
        "PutAccountPublicAccessBlock",
        "DeleteAccountPublicAccessBlock",
        "PutBucketVersioning",
        "PutObjectLockConfiguration",
    } <= names
    topic_id = _logical_id(dev.ops_stack, "Alerts")
    [target] = rule["Targets"]
    assert target["Arn"] == {"Ref": topic_id}
    # EventBridge must be allowed to publish: the topic policy names the service.
    policy = _only(dev.ops, "AWS::SNS::TopicPolicy")["Properties"]["PolicyDocument"]
    assert any(
        s.get("Principal") == {"Service": "events.amazonaws.com"} and s["Action"] == "sns:Publish"
        for s in policy["Statement"]
    )


# ------------------------------------------------------------------ alarms


def test_every_alarm_notifies_the_alerts_topic(dev: Synth) -> None:
    topic_id = _logical_id(dev.ops_stack, "Alerts")
    alarms = _alarms(dev.ops)
    assert set(alarms) == {
        "wiki-dev-api5xx",
        "wiki-dev-authfailures",
        "wiki-dev-mcperrors",
        "wiki-dev-mcpthrottles",
        "wiki-dev-listingfailures",
    }
    for name, alarm in alarms.items():
        assert alarm["AlarmActions"] == [{"Ref": topic_id}], name
        assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold", name
        assert alarm["TreatMissingData"] == "notBreaching", name
        assert alarm["EvaluationPeriods"] == 1, name
        assert alarm["Statistic"] == "Sum", name


def test_api_5xx_alarm(dev: Synth) -> None:
    alarm = _alarms(dev.ops)["wiki-dev-api5xx"]
    assert alarm["Namespace"] == "AWS/ApiGateway"
    assert alarm["MetricName"] == "5XXError"
    assert alarm["Threshold"] == 1
    assert alarm["Period"] == 300
    assert alarm["Dimensions"] == [{"Name": "ApiName", "Value": "wiki-dev"}]


def test_authorizer_failure_spike_alarm(dev: Synth) -> None:
    alarm = _alarms(dev.ops)["wiki-dev-authfailures"]
    assert alarm["Namespace"] == "AWS/Lambda"
    assert alarm["MetricName"] == "Errors"
    assert alarm["Threshold"] == 20
    assert alarm["Period"] == 300
    [dim] = alarm["Dimensions"]
    assert dim["Name"] == "FunctionName"
    assert _is_import_of(dim["Value"], "compute", "Authorizer")


def test_mcp_error_and_throttle_alarms(dev: Synth) -> None:
    alarms = _alarms(dev.ops)
    for name, metric in (("wiki-dev-mcperrors", "Errors"), ("wiki-dev-mcpthrottles", "Throttles")):
        alarm = alarms[name]
        assert alarm["Namespace"] == "AWS/Lambda"
        assert alarm["MetricName"] == metric
        assert alarm["Threshold"] == 1
        assert alarm["Period"] == 300
        [dim] = alarm["Dimensions"]
        assert dim["Name"] == "FunctionName"
        assert _is_import_of(dim["Value"], "compute", "Mcp")
        assert not _is_import_of(dim["Value"], "compute", "Authorizer")


def test_listing_failure_metric_filter_and_alarm(dev: Synth) -> None:
    mf = _only(dev.ops, "AWS::Logs::MetricFilter")["Properties"]
    assert mf["FilterPattern"] == '?"listing_refresh_failed" ?"listing_rebuilt_unpersisted"'
    assert _is_import_of(mf["LogGroupName"], "compute", "McpLogs")
    [transform] = mf["MetricTransformations"]
    assert transform == {
        "DefaultValue": 0,
        "MetricName": "ListingRebuildFailures",
        "MetricNamespace": "wiki/dev",
        "MetricValue": "1",
    }
    alarm = _alarms(dev.ops)["wiki-dev-listingfailures"]
    assert alarm["Namespace"] == "wiki/dev"
    assert alarm["MetricName"] == "ListingRebuildFailures"
    assert alarm["Threshold"] == 5
    assert alarm["Period"] == 900


# ------------------------------------------------------------- break glass


def test_break_glass_role_is_the_only_holder_of_hard_delete(dev: Synth) -> None:
    role_id = _logical_id(dev.ops_stack, "BreakGlass")
    role = dev.ops["Resources"][role_id]["Properties"]
    assert role["RoleName"] == "wiki-dev-break-glass"
    assert role["MaxSessionDuration"] == 3600
    [trust] = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Action"] == "sts:AssumeRole"
    assert json.dumps(trust["Principal"]).endswith(':root"]]}}'), "trusts this account's IAM"
    assert "Service" not in trust["Principal"], "never a function"

    [inline] = role["Policies"]
    statements = inline["PolicyDocument"]["Statement"]
    actions = {
        a
        for st in statements
        for a in ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])
    }
    assert {"s3:DeleteObjectVersion", "s3:BypassGovernanceRetention"} <= actions
    assert not [a for a in actions if a.endswith("*")], actions
    [objects] = [st for st in statements if "s3:DeleteObjectVersion" in st["Action"]]
    assert json.dumps(objects["Resource"]).endswith('"/a/*"]]}'), "article keys only"
    assert "kms:GenerateDataKey" not in actions, "reads and deletes; never writes content"

    # Nothing else in any stack holds either permission (§8.8).
    for name, template in (("storage", dev.storage), ("compute", dev.compute), ("ops", dev.ops)):
        for lid, res in template["Resources"].items():
            if lid == role_id or res["Type"] not in ("AWS::IAM::Role", "AWS::IAM::Policy"):
                continue
            blob = json.dumps(res)
            assert "BypassGovernanceRetention" not in blob, (name, lid)
            assert "DeleteObjectVersion" not in blob, (name, lid)


def test_break_glass_assume_role_is_alerted(dev: Synth) -> None:
    rule = _rule(dev.ops, "wiki-dev-break-glass-used")
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.sts"]
    assert pattern["detail"]["eventName"] == ["AssumeRole"]
    role_id = _logical_id(dev.ops_stack, "BreakGlass")
    assert pattern["detail"]["requestParameters"]["roleArn"] == [{"Fn::GetAtt": [role_id, "Arn"]}]
    [target] = rule["Targets"]
    assert target["Arn"] == {"Ref": _logical_id(dev.ops_stack, "Alerts")}


# ------------------------------------------------------------------ backup


def test_backup_plan_snapshots_grant_table_daily(dev: Synth, prod: Synth) -> None:
    plan = _only(dev.ops, "AWS::Backup::BackupPlan")["Properties"]["BackupPlan"]
    assert plan["BackupPlanName"] == "wiki-dev-grants-daily"
    [rule] = plan["BackupPlanRule"]
    assert rule["ScheduleExpression"].startswith("cron(")
    assert rule["Lifecycle"] == {"DeleteAfterDays": 35}
    assert "MoveToColdStorageAfterDays" not in rule["Lifecycle"]
    vault_id = _logical_id(dev.ops_stack, "Vault")
    assert rule["TargetBackupVault"] == {"Fn::GetAtt": [vault_id, "BackupVaultName"]}

    selection = _only(dev.ops, "AWS::Backup::BackupSelection")["Properties"]["BackupSelection"]
    [resource] = selection["Resources"]
    assert _is_import_of(resource, "storage", "Grants")
    assert not _is_import_of(resource, "storage", "RateLimit")

    for synth in (dev, prod):
        vault = _only(synth.ops, "AWS::Backup::BackupVault")
        assert _is_import_of(vault["Properties"]["EncryptionKeyArn"], "storage", "Key")
    assert _only(dev.ops, "AWS::Backup::BackupVault")["DeletionPolicy"] == "Delete"
    assert _only(prod.ops, "AWS::Backup::BackupVault")["DeletionPolicy"] == "Retain"


def test_backup_role_can_back_up_and_restore(dev: Synth) -> None:
    selection = _only(dev.ops, "AWS::Backup::BackupSelection")["Properties"]["BackupSelection"]
    role_id = selection["IamRoleArn"]["Fn::GetAtt"][0]
    managed = json.dumps(dev.ops["Resources"][role_id]["Properties"]["ManagedPolicyArns"])
    assert "AWSBackupServiceRolePolicyForBackup" in managed
    assert "AWSBackupServiceRolePolicyForRestores" in managed


# ----------------------------------------------------------------- outputs


def test_ops_outputs(dev: Synth) -> None:
    assert {
        "AlertsTopicArn",
        "TrailArn",
        "TrailLogBucketName",
        "BackupVaultName",
        "BreakGlassRoleArn",
    } <= set(dev.ops["Outputs"])


def test_ops_stack_is_a_leaf(dev: Synth) -> None:
    """Nothing may depend on ops, so it can be added or removed independently."""
    for stack in (dev.storage_stack, dev.compute_stack, dev.api_stack):
        assert dev.ops_stack not in stack.dependencies
    assert {s.node.id for s in dev.ops_stack.dependencies} >= {"storage", "compute"}


def test_bad_log_retention_is_rejected() -> None:
    from infra.stacks.ops import _retention

    with pytest.raises(ValueError, match="log_retention_days=91"):
        _retention(91)
