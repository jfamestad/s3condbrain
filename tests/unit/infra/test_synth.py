"""Synth-time assertions over the three stacks — HANDOFF §8.8, §9, §12.6.

These are the IAM and gateway properties the design leans on. Each one is something
that would deploy fine if wrong and only show up as a disclosure later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.app import build
from infra.config import EnvConfig, guard_account, load
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.storage import StorageStack

CERT_ARN = "arn:aws:acm:us-west-2:123456789012:certificate/00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Synth:
    storage_stack: StorageStack
    compute_stack: ComputeStack
    api_stack: ApiStack
    storage: dict[str, Any]
    compute: dict[str, Any]
    api: dict[str, Any]


def _build_code_root(root: Path) -> Path:
    for name in ("authorizer", "mcp"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "placeholder.py").write_text("# built by make build\n")
    return root


def _synth(
    env_name: str,
    code_root: Path,
    context: dict[str, str] | None = None,
    cfg: EnvConfig | None = None,
) -> Synth:
    app = cdk.App(context={"env": env_name, "certificateArn": CERT_ARN, **(context or {})})
    cfg = cfg or load(env_name)
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    storage = StorageStack(app, "storage", cfg=cfg, env=aws_env)
    compute = ComputeStack(
        app, "compute", cfg=cfg, storage=storage, code_root=code_root, env=aws_env
    )
    api = ApiStack(app, "api", cfg=cfg, compute=compute, env=aws_env)
    return Synth(
        storage_stack=storage,
        compute_stack=compute,
        api_stack=api,
        storage=Template.from_stack(storage).to_json(),
        compute=Template.from_stack(compute).to_json(),
        api=Template.from_stack(api).to_json(),
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


def _statements(doc: dict[str, Any]) -> list[dict[str, Any]]:
    return list(doc.get("Statement", []))


def _actions(stmt: dict[str, Any]) -> list[str]:
    action = stmt.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def _role_statements(template: dict[str, Any], role_id: str) -> list[dict[str, Any]]:
    """Every statement in the role's inline policies plus attached AWS::IAM::Policy docs."""
    resources = template["Resources"]
    out: list[dict[str, Any]] = []
    for inline in resources[role_id]["Properties"].get("Policies", []):
        out.extend(_statements(inline["PolicyDocument"]))
    for res in resources.values():
        if res["Type"] != "AWS::IAM::Policy":
            continue
        if any(r.get("Ref") == role_id for r in res["Properties"].get("Roles", [])):
            out.extend(_statements(res["Properties"]["PolicyDocument"]))
    return out


def _role_actions(template: dict[str, Any], role_id: str) -> list[str]:
    return [a for stmt in _role_statements(template, role_id) for a in _actions(stmt)]


def _function_by_handler(template: dict[str, Any], handler: str) -> tuple[str, dict[str, Any]]:
    for lid, res in _resources(template, "AWS::Lambda::Function").items():
        if res["Properties"].get("Handler") == handler:
            return lid, res
    raise AssertionError(f"no function with handler {handler}")


def _role_id_of(fn: dict[str, Any]) -> str:
    return fn["Properties"]["Role"]["Fn::GetAtt"][0]


def _logical_id(stack: cdk.Stack, construct_id: str) -> str:
    child = stack.node.find_child(construct_id).node.default_child
    assert isinstance(child, cdk.CfnElement)
    return stack.get_logical_id(child)


# ------------------------------------------------------------------ storage


def test_bucket_versioned_and_public_access_blocked(dev: Synth) -> None:
    bucket = _only(dev.storage, "AWS::S3::Bucket")["Properties"]
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    assert bucket["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert bucket["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}


def test_bucket_sse_kms_with_stack_key(dev: Synth) -> None:
    bucket = _only(dev.storage, "AWS::S3::Bucket")["Properties"]
    key_id = _logical_id(dev.storage_stack, "Key")
    [rule] = bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert rule["BucketKeyEnabled"] is True
    assert rule["ServerSideEncryptionByDefault"] == {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": {"Fn::GetAtt": [key_id, "Arn"]},
    }
    assert _only(dev.storage, "AWS::KMS::Key")["Properties"]["EnableKeyRotation"] is True


def test_bucket_enforces_ssl(dev: Synth) -> None:
    policy = _only(dev.storage, "AWS::S3::BucketPolicy")["Properties"]["PolicyDocument"]
    denies = [
        s
        for s in _statements(policy)
        if s["Effect"] == "Deny"
        and s.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
    ]
    assert denies, "expected an aws:SecureTransport deny statement"


def test_dev_bucket_has_no_object_lock(dev: Synth) -> None:
    bucket = _only(dev.storage, "AWS::S3::Bucket")["Properties"]
    assert "ObjectLockEnabled" not in bucket
    assert "ObjectLockConfiguration" not in bucket
    assert _only(dev.storage, "AWS::S3::Bucket").get("DeletionPolicy") == "Delete"


def test_prod_bucket_object_lock_governance_one_year(prod: Synth) -> None:
    resource = _only(prod.storage, "AWS::S3::Bucket")
    bucket = resource["Properties"]
    assert bucket["ObjectLockEnabled"] is True
    assert bucket["ObjectLockConfiguration"] == {
        "ObjectLockEnabled": "Enabled",
        "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 365}},
    }
    assert resource["DeletionPolicy"] == "Retain"
    assert "Custom::S3AutoDeleteObjects" not in {
        r["Type"] for r in prod.storage["Resources"].values()
    }


def test_listing_lifecycle_rule_does_not_undercut_retention(prod: Synth) -> None:
    bucket = _only(prod.storage, "AWS::S3::Bucket")["Properties"]
    [rule] = bucket["LifecycleConfiguration"]["Rules"]
    assert rule["TagFilters"] == [{"Key": "wiki:listing", "Value": "true"}]
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] >= 365
    assert "Expiration" not in rule  # current versions are never expired


def test_grant_table_keys_and_index(dev: Synth) -> None:
    tables = _resources(dev.storage, "AWS::DynamoDB::Table")
    grants = [t for k, t in tables.items() if k.startswith("Grants")]
    assert len(grants) == 1, sorted(tables)
    table = grants[0]["Properties"]
    assert table["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    [gsi] = table["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == "gs1"
    assert gsi["KeySchema"] == [
        {"AttributeName": "gs1pk", "KeyType": "HASH"},
        {"AttributeName": "gs1sk", "KeyType": "RANGE"},
    ]
    assert gsi["Projection"] == {"ProjectionType": "ALL"}
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["SSESpecification"]["SSEType"] == "KMS"
    assert table["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}


# ------------------------------------------------------------------ compute


def test_functions_are_arm64_python313(dev: Synth) -> None:
    fns = _resources(dev.compute, "AWS::Lambda::Function")
    handlers = {f["Properties"]["Handler"] for f in fns.values()}
    assert handlers == {
        "app.authorizer.handler.handle",
        "app.mcp.handler.handle",
        "app.web.handler.handle",
    }
    for fn in fns.values():
        assert fn["Properties"]["Runtime"] == "python3.13"
        assert fn["Properties"]["Architectures"] == ["arm64"]
        assert fn["Properties"]["TracingConfig"] == {"Mode": "Active"}
        assert "S3Bucket" in fn["Properties"]["Code"], "expected an asset, not inline code"


def test_function_sizing(dev: Synth) -> None:
    _, auth = _function_by_handler(dev.compute, "app.authorizer.handler.handle")
    _, mcp = _function_by_handler(dev.compute, "app.mcp.handler.handle")
    assert (auth["Properties"]["MemorySize"], auth["Properties"]["Timeout"]) == (256, 10)
    assert (mcp["Properties"]["MemorySize"], mcp["Properties"]["Timeout"]) == (512, 30)
    for group in _resources(dev.compute, "AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 90


def test_function_environment(dev: Synth) -> None:
    _, auth = _function_by_handler(dev.compute, "app.authorizer.handler.handle")
    _, mcp = _function_by_handler(dev.compute, "app.mcp.handler.handle")
    auth_env = auth["Properties"]["Environment"]["Variables"]
    mcp_env = mcp["Properties"]["Environment"]["Variables"]
    assert set(auth_env) == {
        "AUTHKIT_DOMAIN",
        "JWKS_URL",
        "CANONICAL_MCP_URL",
        "RESOURCE_METADATA_URL",
        "LOG_LEVEL",
    }
    assert auth_env["CANONICAL_MCP_URL"] == "https://wiki-dev.famestad.com/mcp"
    assert auth_env["RESOURCE_METADATA_URL"] == (
        "https://wiki-dev.famestad.com/.well-known/oauth-protected-resource/mcp"
    )
    assert auth_env["JWKS_URL"] == auth_env["AUTHKIT_DOMAIN"] + "/oauth2/jwks"
    assert set(mcp_env) >= set(auth_env) | {
        "WIKI_BUCKET",
        "GRANT_TABLE",
        "STORAGE_ROLE_ARN",
        "KMS_KEY_ARN",
        "ALLOWED_ORIGINS",
        "CREDENTIAL_CACHE_SECONDS",
        "MCP_STRICT_HEADERS",
        "MCP_PROTOCOL_VERSION",
    }
    assert mcp_env["MCP_STRICT_HEADERS"] == ("true" if load("dev").strict_mcp_headers else "false")
    assert mcp_env["MCP_PROTOCOL_VERSION"] == "2026-07-28"
    assert mcp_env["CREDENTIAL_CACHE_SECONDS"] == "900"
    assert mcp_env["ALLOWED_ORIGINS"] == "https://claude.ai"
    storage_role_id = _logical_id(dev.compute_stack, "StorageRole")
    assert mcp_env["STORAGE_ROLE_ARN"] == {"Fn::GetAtt": [storage_role_id, "Arn"]}


def test_strict_mcp_headers_follows_config(tmp_path: Path) -> None:
    """§6.2: the safety valve is a config field, not a literal in the stack."""
    assert load("dev").strict_mcp_headers is False
    assert load("prod").strict_mcp_headers is False, "flip deliberately; see infra/config.py"
    strict = replace(load("dev"), strict_mcp_headers=True)
    synth = _synth("dev", _build_code_root(tmp_path), cfg=strict)
    _, mcp = _function_by_handler(synth.compute, "app.mcp.handler.handle")
    assert mcp["Properties"]["Environment"]["Variables"]["MCP_STRICT_HEADERS"] == "true"


def _annotations(stack: cdk.Stack, type_: str) -> list[str]:
    return [str(m.data) for c in stack.node.find_all() for m in c.node.metadata if m.type == type_]


def test_missing_web_client_id_warns_in_dev_and_errors_in_prod(dev: Synth, prod: Synth) -> None:
    """An empty WORKOS_CLIENT_ID never deploys silently; prod does not deploy at all."""
    assert load("dev").workos_web_client_id == "" and load("prod").workos_web_client_id == ""
    for synth in (dev, prod):
        _, web = _function_by_handler(synth.compute, "app.web.handler.handle")
        assert web["Properties"]["Environment"]["Variables"]["WORKOS_CLIENT_ID"] == ""

    dev_warnings = _annotations(dev.compute_stack, "aws:cdk:warning")
    dev_errors = _annotations(dev.compute_stack, "aws:cdk:error")
    assert any("workosWebClientId" in w for w in dev_warnings), dev_warnings
    assert not any("workosWebClientId" in e for e in dev_errors)

    prod_errors = _annotations(prod.compute_stack, "aws:cdk:error")
    assert any("workosWebClientId" in e for e in prod_errors), prod_errors
    assert not any(
        "workosWebClientId" in w for w in _annotations(prod.compute_stack, "aws:cdk:warning")
    )


def test_web_client_id_from_config_or_context_is_silent(tmp_path: Path) -> None:
    by_context = _synth(
        "prod", _build_code_root(tmp_path / "ctx"), {"workosWebClientId": "client_ctx"}
    )
    by_config = _synth(
        "prod",
        _build_code_root(tmp_path / "cfg"),
        cfg=replace(load("prod"), workos_web_client_id="client_cfg"),
    )
    for synth, expected in ((by_context, "client_ctx"), (by_config, "client_cfg")):
        _, web = _function_by_handler(synth.compute, "app.web.handler.handle")
        assert web["Properties"]["Environment"]["Variables"]["WORKOS_CLIENT_ID"] == expected
        for type_ in ("aws:cdk:error", "aws:cdk:warning"):
            annotations = _annotations(synth.compute_stack, type_)
            assert not any("workosWebClientId" in a for a in annotations), annotations


def test_authorizer_role_has_no_data_permissions(dev: Synth) -> None:
    _, auth = _function_by_handler(dev.compute, "app.authorizer.handler.handle")
    role_id = _role_id_of(auth)
    forbidden = ("dynamodb:", "s3:", "sts:", "kms:")
    offending = [a for a in _role_actions(dev.compute, role_id) if a.startswith(forbidden)]
    assert offending == []
    managed = json.dumps(dev.compute["Resources"][role_id]["Properties"]["ManagedPolicyArns"])
    assert "AWSLambdaBasicExecutionRole" in managed
    assert managed.count("iam::aws:policy") == 1, "authorizer gets the basic execution policy only"


def test_mcp_role_has_no_s3_and_is_table_read_only(dev: Synth) -> None:
    _, mcp = _function_by_handler(dev.compute, "app.mcp.handler.handle")
    role_id = _role_id_of(mcp)
    actions = _role_actions(dev.compute, role_id)
    assert not [a for a in actions if a.startswith("s3:")], actions
    assert not [a for a in actions if a == "s3:*" or a == "*"], actions
    mutating = {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:*"}
    # The grant table is read-only for this role. The only mutating DynamoDB action it
    # may hold is UpdateItem on the rate-limit counters table (§12.6), never the grants.
    grant_table_id = next(k for k in dev.storage["Resources"] if k.startswith("Grants"))
    for st in _role_statements(dev.compute, role_id):
        acts = set(_actions(st))
        if not (acts & mutating):
            continue
        assert acts == {"dynamodb:UpdateItem"}, acts
        assert grant_table_id not in json.dumps(st["Resource"]), st
        assert "ratelimit" in json.dumps(st["Resource"]).lower() or "RateLimit" in json.dumps(
            st["Resource"]
        ), st
    assert not [a for a in actions if a.startswith("dynamodb:Batch") and "Write" in a], actions
    assert "sts:AssumeRole" in actions
    assert {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"} <= set(actions)
    managed = json.dumps(dev.compute["Resources"][role_id]["Properties"]["ManagedPolicyArns"])
    assert "AWSLambdaBasicExecutionRole" in managed
    assert "AWSXRayDaemonWriteAccess" in managed


def test_mcp_role_assume_targets_only_storage_role(dev: Synth) -> None:
    _, mcp = _function_by_handler(dev.compute, "app.mcp.handler.handle")
    storage_role_id = _logical_id(dev.compute_stack, "StorageRole")
    assume = [
        s
        for s in _role_statements(dev.compute, _role_id_of(mcp))
        if "sts:AssumeRole" in _actions(s)
    ]
    assert len(assume) == 1
    assert assume[0]["Resource"] == {"Fn::GetAtt": [storage_role_id, "Arn"]}


def test_storage_role_trusts_only_the_two_functions(dev: Synth) -> None:
    """Assumed by the MCP and web functions' roles and by nothing else — never a
    person, never a service principal (§8.8)."""
    _, mcp = _function_by_handler(dev.compute, "app.mcp.handler.handle")
    _, web = _function_by_handler(dev.compute, "app.web.handler.handle")
    storage_role = dev.compute["Resources"][_logical_id(dev.compute_stack, "StorageRole")]
    trust = storage_role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    principals = [json.dumps(t["Principal"], sort_keys=True) for t in trust]
    expected = {
        json.dumps({"AWS": {"Fn::GetAtt": [_role_id_of(fn), "Arn"]}}, sort_keys=True)
        for fn in (mcp, web)
    }
    assert set(principals) == expected, principals
    assert storage_role["Properties"]["MaxSessionDuration"] == 3600


def test_storage_role_outer_bound(dev: Synth) -> None:
    role_id = _logical_id(dev.compute_stack, "StorageRole")
    statements = _role_statements(dev.compute, role_id)
    actions = [a for s in statements for a in _actions(s)]
    assert not [a for a in actions if a.startswith("s3:Delete")], actions
    assert "s3:BypassGovernanceRetention" not in actions
    assert not [a for a in actions if a.endswith("*")], actions
    assert set(actions) == {
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:DescribeKey",
    }
    assert all(s["Effect"] == "Allow" for s in statements)

    [listing] = [s for s in statements if "s3:ListBucket" in _actions(s)]
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["a/*"]}}
    assert set(_actions(listing)) == {"s3:ListBucket", "s3:ListBucketVersions"}, (
        "listing actions must not share a statement with object actions"
    )
    [tagging] = [s for s in statements if "s3:PutObjectTagging" in _actions(s)]
    assert _actions(tagging) == ["s3:PutObjectTagging"]
    assert "_listing.json" in json.dumps(tagging["Resource"]), "tagging is for listings only"

    [objects] = [s for s in statements if "s3:GetObject" in _actions(s)]
    assert json.dumps(objects["Resource"]).endswith('"/a/*"]]}'), objects["Resource"]


def test_inline_placeholder_when_code_root_missing(tmp_path: Path) -> None:
    synth = _synth("dev", tmp_path / "does-not-exist")
    for fn in _resources(synth.compute, "AWS::Lambda::Function").values():
        assert "ZipFile" in fn["Properties"]["Code"]
    warnings = [
        m.data
        for c in synth.compute_stack.node.find_all()
        for m in c.node.metadata
        if m.type == "aws:cdk:warning"
    ]
    assert any("make build" in str(w) for w in warnings)


# ---------------------------------------------------------------------- api


def test_request_authorizer_zero_cache(dev: Synth) -> None:
    auth = _only(dev.api, "AWS::ApiGateway::Authorizer")["Properties"]
    assert auth["Type"] == "REQUEST"
    assert auth["AuthorizerResultTtlInSeconds"] == 0
    assert auth["IdentitySource"] == "method.request.header.Authorization"


def test_gateway_responses(dev: Synth) -> None:
    responses = {
        r["Properties"]["ResponseType"]: r["Properties"]
        for r in _resources(dev.api, "AWS::ApiGateway::GatewayResponse").values()
    }
    unauthorized = responses["UNAUTHORIZED"]
    assert unauthorized["StatusCode"] == "401"
    assert unauthorized["ResponseParameters"] == {
        "gatewayresponse.header.WWW-Authenticate": (
            "'Bearer resource_metadata=\"https://wiki-dev.famestad.com"
            '/.well-known/oauth-protected-resource/mcp", scope="wiki.read"\''
        ),
        "gatewayresponse.header.Access-Control-Allow-Origin": "'https://claude.ai'",
        "gatewayresponse.header.Access-Control-Expose-Headers": "'WWW-Authenticate'",
    }
    assert unauthorized["ResponseTemplates"] == {"application/json": '{"error":"unauthorized"}'}

    assert responses["MISSING_AUTHENTICATION_TOKEN"]["StatusCode"] == "404"
    assert responses["MISSING_AUTHENTICATION_TOKEN"]["ResponseTemplates"] == {
        "application/json": "{}"
    }
    assert "StatusCode" not in responses["DEFAULT_4XX"]
    assert responses["DEFAULT_4XX"]["ResponseTemplates"] == {
        "application/json": '{"error":"request refused"}'
    }
    assert responses["DEFAULT_5XX"]["StatusCode"] == "500"
    assert responses["DEFAULT_5XX"]["ResponseTemplates"] == {
        "application/json": '{"error":"server error"}'
    }


def test_stage_throttle_and_settings(dev: Synth) -> None:
    stage = _only(dev.api, "AWS::ApiGateway::Stage")["Properties"]
    assert stage["StageName"] == "v1"
    assert stage["TracingEnabled"] is True
    [settings] = stage["MethodSettings"]
    assert settings["ThrottlingRateLimit"] == 20
    assert settings["ThrottlingBurstLimit"] == 50
    assert settings["LoggingLevel"] == "ERROR"
    assert settings["MetricsEnabled"] is True
    assert settings["DataTraceEnabled"] is False
    api = _only(dev.api, "AWS::ApiGateway::RestApi")["Properties"]
    assert api["EndpointConfiguration"] == {"Types": ["REGIONAL"]}


def _methods(synth: Synth) -> dict[tuple[str, str], dict[str, Any]]:
    """Map (path, verb) -> method properties by walking the resource tree."""
    resources = synth.api["Resources"]
    api_id = next(iter(_resources(synth.api, "AWS::ApiGateway::RestApi")))

    def path_of(ref: dict[str, Any]) -> str:
        if "Fn::GetAtt" in ref:
            assert ref["Fn::GetAtt"] == [api_id, "RootResourceId"]
            return ""
        res = resources[ref["Ref"]]["Properties"]
        return path_of(res["ParentId"]) + "/" + res["PathPart"]

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for method in _resources(synth.api, "AWS::ApiGateway::Method").values():
        props = method["Properties"]
        out[(path_of(props["ResourceId"]), props["HttpMethod"])] = props
    return out


def test_route_table(dev: Synth) -> None:
    methods = _methods(dev)
    assert set(methods) == {
        ("/mcp", "POST"),
        ("/mcp", "GET"),
        ("/mcp", "DELETE"),
        ("/mcp", "OPTIONS"),
        ("/.well-known/oauth-protected-resource", "GET"),
        ("/.well-known/oauth-protected-resource/mcp", "GET"),
        ("/app", "ANY"),
        ("/app/{proxy+}", "ANY"),
    }
    authorizer_id = next(iter(_resources(dev.api, "AWS::ApiGateway::Authorizer")))
    post = methods[("/mcp", "POST")]
    assert post["AuthorizationType"] == "CUSTOM"
    assert post["AuthorizerId"] == {"Ref": authorizer_id}
    assert post["Integration"]["Type"] == "AWS_PROXY"
    web_routes = {("/app", "ANY"), ("/app/{proxy+}", "ANY")}
    for key, props in methods.items():
        if key == ("/mcp", "POST"):
            continue
        assert props["AuthorizationType"] == "NONE", key
        expected_type = "AWS_PROXY" if key in web_routes else "MOCK"
        assert props["Integration"]["Type"] == expected_type, key


def test_mcp_get_and_delete_are_405(dev: Synth) -> None:
    methods = _methods(dev)
    for verb in ("GET", "DELETE"):
        props = methods[("/mcp", verb)]
        [response] = props["Integration"]["IntegrationResponses"]
        assert response["StatusCode"] == "405"
        assert response["ResponseParameters"] == {"method.response.header.Allow": "'POST, OPTIONS'"}
        assert props["Integration"]["RequestTemplates"] == {
            "application/json": '{"statusCode": 405}'
        }
        assert [m["StatusCode"] for m in props["MethodResponses"]] == ["405"]


def test_mcp_cors_preflight(dev: Synth) -> None:
    options = _methods(dev)[("/mcp", "OPTIONS")]
    [response] = options["Integration"]["IntegrationResponses"]
    headers = response["ResponseParameters"]
    assert headers["method.response.header.Access-Control-Allow-Origin"] == "'https://claude.ai'"
    assert headers["method.response.header.Access-Control-Allow-Methods"] == "'POST,OPTIONS'"
    assert headers["method.response.header.Access-Control-Allow-Headers"] == (
        "'authorization,content-type,mcp-protocol-version,mcp-method,mcp-name,mcp-param-*'"
    )
    assert headers["method.response.header.Access-Control-Expose-Headers"] == "'WWW-Authenticate'"
    assert headers["method.response.header.Access-Control-Max-Age"] == "'3600'"


def test_protected_resource_metadata_documents(dev: Synth) -> None:
    methods = _methods(dev)
    expected = {
        "resource": "https://wiki-dev.famestad.com/mcp",
        "authorization_servers": [load("dev").authkit_domain],
        "scopes_supported": ["wiki.read", "wiki.write"],
        "bearer_methods_supported": ["header"],
    }
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        props = methods[(path, "GET")]
        [response] = props["Integration"]["IntegrationResponses"]
        assert response["StatusCode"] == "200"
        assert json.loads(response["ResponseTemplates"]["application/json"]) == expected
        headers = response["ResponseParameters"]
        assert headers["method.response.header.Content-Type"] == "'application/json'"
        assert headers["method.response.header.Access-Control-Allow-Origin"] == "'*'"


def test_custom_domain_from_certificate_arn(dev: Synth) -> None:
    domain = _only(dev.api, "AWS::ApiGateway::DomainName")["Properties"]
    assert domain["DomainName"] == "wiki-dev.famestad.com"
    assert domain["RegionalCertificateArn"] == CERT_ARN
    assert domain["SecurityPolicy"] == "TLS_1_2"
    assert domain["EndpointConfiguration"] == {"Types": ["REGIONAL"]}
    mapping = _only(dev.api, "AWS::ApiGateway::BasePathMapping")["Properties"]
    assert "BasePath" not in mapping
    assert "AWS::Route53::RecordSet" not in {r["Type"] for r in dev.api["Resources"].values()}
    assert "DomainTarget" in dev.api["Outputs"]


def test_missing_certificate_arn_is_a_synth_error(tmp_path: Path) -> None:
    app = cdk.App(context={"env": "dev"})
    cfg = load("dev")
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    storage = StorageStack(app, "storage", cfg=cfg, env=aws_env)
    compute = ComputeStack(
        app, "compute", cfg=cfg, storage=storage, code_root=_build_code_root(tmp_path), env=aws_env
    )
    api = ApiStack(app, "api", cfg=cfg, compute=compute, env=aws_env)
    errors = [m.data for m in api.node.metadata if m.type == "aws:cdk:error"]
    assert errors and "certificateArn" in str(errors[0])
    assert not _resources(Template.from_stack(api).to_json(), "AWS::ApiGateway::DomainName")


def test_budget(dev: Synth, tmp_path: Path) -> None:
    budget = _only(dev.api, "AWS::Budgets::Budget")["Properties"]
    assert budget["Budget"]["BudgetLimit"] == {"Amount": 20, "Unit": "USD"}
    assert budget["Budget"]["TimeUnit"] == "MONTHLY"
    assert budget["Budget"]["BudgetType"] == "COST"
    assert "NotificationsWithSubscribers" not in budget

    with_email = _synth("dev", _build_code_root(tmp_path), {"alertEmail": "ops@example.com"})
    budget = _only(with_email.api, "AWS::Budgets::Budget")["Properties"]
    [notification] = budget["NotificationsWithSubscribers"]
    assert notification["Subscribers"] == [
        {"SubscriptionType": "EMAIL", "Address": "ops@example.com"}
    ]


def test_outputs(dev: Synth) -> None:
    outputs = dev.api["Outputs"]
    assert {"ApiUrl", "CanonicalMcpUrl", "ResourceMetadataUrl", "DomainTarget"} <= set(outputs)
    assert outputs["CanonicalMcpUrl"]["Value"] == "https://wiki-dev.famestad.com/mcp"
    Template.from_stack(dev.api_stack).has_output("ResourceMetadataUrl", Match.object_like({}))


# ------------------------------------------------------ storage: rate limits


def test_ratelimit_table_ttl_and_encryption(dev: Synth, prod: Synth) -> None:
    """§12.6 counters: own table, ``ttl`` expiry, same CMK, removal follows cfg."""
    for synth, deletion in ((dev, "Delete"), (prod, "Retain")):
        tables = _resources(synth.storage, "AWS::DynamoDB::Table")
        [(_, resource)] = [(k, t) for k, t in tables.items() if k.startswith("RateLimit")]
        table = resource["Properties"]
        assert table["TableName"] == f"wiki-{synth.storage_stack.cfg.name}-ratelimit"
        assert table["KeySchema"] == [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ]
        assert table["BillingMode"] == "PAY_PER_REQUEST"
        assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
        key_id = _logical_id(synth.storage_stack, "Key")
        assert table["SSESpecification"] == {
            "SSEEnabled": True,
            "SSEType": "KMS",
            "KMSMasterKeyId": {"Fn::GetAtt": [key_id, "Arn"]},
        }
        assert "PointInTimeRecoverySpecification" not in table
        assert resource["DeletionPolicy"] == deletion
    assert "RateLimitTableName" in dev.storage["Outputs"]


def test_bucket_policy_denies_other_accounts(dev: Synth) -> None:
    """§12.3: the bucket answers to nothing outside this account."""
    [policy] = _resources(dev.storage, "AWS::S3::BucketPolicy").values()
    denies = [
        st
        for st in policy["Properties"]["PolicyDocument"]["Statement"]
        if st["Effect"] == "Deny" and st.get("Sid") == "DenyOtherAccounts"
    ]
    assert len(denies) == 1
    [deny] = denies
    assert deny["Principal"] == {"AWS": "*"}
    assert deny["Action"] == "s3:*"
    assert "StringNotEquals" in deny["Condition"]
    assert "aws:PrincipalAccount" in deny["Condition"]["StringNotEquals"]


# ------------------------------------------------------- account guard (§9.6)

PROD_ACCOUNT = "000000000000"


def _app(env_name: str, tmp_path: Path, **context: str) -> cdk.App:
    return cdk.App(
        context={
            "env": env_name,
            "certificateArn": CERT_ARN,
            "codeRoot": str(_build_code_root(tmp_path)),
            "workosWebClientId": "client_test",
            **context,
        }
    )


def test_prod_without_pinned_account_refuses_to_synth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact failure an operator sees from `cdk synth -c env=prod` before step 1."""
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    assert load("prod").account is None, "config.py pins prod now; retarget this test"
    with pytest.raises(SystemExit) as exc:
        build(_app("prod", tmp_path))
    message = str(exc.value)
    assert "env 'prod' has account=None in infra/config.py" in message
    assert "first-deploy checklist step 1" in message
    assert "-c prodAccount=" in message


def test_prod_account_context_override_lets_ci_synth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CDK_DEFAULT_ACCOUNT", raising=False)
    app = _app("prod", tmp_path, prodAccount=PROD_ACCOUNT)
    cfg = build(app)
    assert cfg.account == PROD_ACCOUNT
    assembly = app.synth()
    for stack in ("storage", "compute", "api", "ops"):
        environment = assembly.get_stack_by_name(f"wiki-prod-{stack}").environment
        assert (environment.account, environment.region) == (PROD_ACCOUNT, "us-west-2")
    # The override is prod-only: dev never picks it up.
    assert load("dev", prod_account=PROD_ACCOUNT).account is None


def test_mismatched_cli_account_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "111111111111")
    with pytest.raises(SystemExit) as exc:
        build(_app("prod", tmp_path, prodAccount=PROD_ACCOUNT))
    assert str(exc.value) == (
        f"credentials resolve to account 111111111111 but infra/config.py pins "
        f"{PROD_ACCOUNT} for env prod"
    )
    # Same rule for any env that pins: a pinned dev is refused the same way.
    pinned_dev = replace(load("dev"), account="222222222222")
    with pytest.raises(SystemExit, match="pins 222222222222 for env dev"):
        guard_account(pinned_dev, "111111111111")


def test_matching_cli_account_synths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", PROD_ACCOUNT)
    app = _app("prod", tmp_path, prodAccount=PROD_ACCOUNT)
    build(app)
    assert app.synth().get_stack_by_name("wiki-prod-storage").environment.account == PROD_ACCOUNT


def test_dev_account_may_float(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dev with account=None synths whatever the CLI resolves to, or nothing at all."""
    assert load("dev").account is None
    guard_account(load("dev"), None)
    guard_account(load("dev"), "123456789012")
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "123456789012")
    app = _app("dev", tmp_path)
    assert build(app).account is None
    environment = app.synth().get_stack_by_name("wiki-dev-storage").environment
    assert environment.account == "unknown-account", "env-agnostic: resolved at deploy time"


def test_pinned_account_must_be_twelve_digits() -> None:
    with pytest.raises(SystemExit, match="exactly 12 digits"):
        guard_account(replace(load("prod"), account="12345"), None)
    with pytest.raises(SystemExit, match="exactly 12 digits"):
        guard_account(replace(load("prod"), account="arn:aws:iam::000000000000:root"), None)


def test_unknown_env_is_refused() -> None:
    with pytest.raises(SystemExit, match="unknown env 'staging'"):
        load("staging")
