"""Compute stack — HANDOFF §8.1, §8.8, §11.2, build steps 5 and 8.

Two functions, Python 3.13, ARM64, code from ``<code_root>/authorizer`` and
``<code_root>/mcp`` (produced by ``make build``; ``code_root`` defaults to ``build``).
When a directory is missing the function gets an inline placeholder so ``cdk synth``
works before ``make build`` — a warning annotation says so.

* ``authorizer`` — handler ``app.authorizer.handler.handle``. Role: logs (+ X-Ray
  write, which ``Tracing.ACTIVE`` attaches). No S3, no DynamoDB, no STS, no KMS.
  Env: AUTHKIT_DOMAIN, JWKS_URL, CANONICAL_MCP_URL, RESOURCE_METADATA_URL, LOG_LEVEL.
* ``mcp`` — handler ``app.mcp.handler.handle``. Role: ``dynamodb:GetItem``,
  ``BatchGetItem``, ``Query`` on the grant table and its index (READ-ONLY);
  ``sts:AssumeRole`` on the storage role; ``kms:Decrypt`` for the table; **no S3**.
  Env: everything in ``app.config.Settings``.

The storage role lives here because its trust policy names the mcp role. Its inline
policy is the outer bound of §8.5's three shapes over the whole ``a/`` prefix plus the
one key; it never holds Delete*, BypassGovernanceRetention, or an unconditioned
ListBucket. Session policies narrow from here (§8.8).

Exposes ``authorizer_fn``, ``mcp_fn``, ``storage_role``, ``canonical_mcp_url``,
``resource_metadata_url``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import aws_cdk as cdk
from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.storage import StorageStack

PLACEHOLDER_CANONICAL_URL = "https://SET-CANONICAL-MCP-URL/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource"
MCP_PROTOCOL_VERSION = "2026-07-28"
PLACEHOLDER_CODE = "def handle(e, c): raise RuntimeError('not built')"


class ComputeStack(cdk.Stack):
    """Authorizer, data plane, and the storage role the data plane assumes."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        code_root: Path = Path("build"),
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        self.storage = storage
        self._code_root = Path(code_root)
        self._log_removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY

        self.canonical_mcp_url = self._resolve_canonical_url()
        self.resource_metadata_url = self._metadata_url(self.canonical_mcp_url)
        authkit = cfg.authkit_domain.rstrip("/")
        common_env = {
            "AUTHKIT_DOMAIN": authkit,
            "JWKS_URL": f"{authkit}/oauth2/jwks",
            "CANONICAL_MCP_URL": self.canonical_mcp_url,
            "RESOURCE_METADATA_URL": self.resource_metadata_url,
            "LOG_LEVEL": "INFO",
        }

        self.authorizer_fn = self._create_authorizer(common_env)

        mcp_role = self._create_mcp_role()
        web_role = self._create_web_role()
        self.storage_role = self._create_storage_role(mcp_role, web_role)
        self._grant_mcp_role(mcp_role)
        self.mcp_fn = self._create_mcp_fn(mcp_role, common_env)
        self.web_secret = self._create_web_secret()
        self._grant_web_role(web_role)
        self.web_fn = self._create_web_fn(web_role, common_env)

        cdk.CfnOutput(self, "StorageRoleArn", value=self.storage_role.role_arn)
        cdk.CfnOutput(self, "McpFunctionName", value=self.mcp_fn.function_name)
        cdk.CfnOutput(self, "WebFunctionName", value=self.web_fn.function_name)
        cdk.CfnOutput(
            self,
            "WebSecretArn",
            value=self.web_secret.secret_arn,
            description="Fill client_secret and api_key from the WorkOS dashboard (docs/DEPLOY.md)",
        )

    # ------------------------------------------------------------------ urls

    def _resolve_canonical_url(self) -> str:
        """AS-2: the exact ``resource`` value; also the ``aud`` every token carries."""
        url = self.cfg.canonical_mcp_url or self.node.try_get_context("canonicalMcpUrl")
        if not url:
            url = PLACEHOLDER_CANONICAL_URL
            cdk.Annotations.of(self).add_warning_v2(
                "wiki:canonical-url",
                "No domain configured and no `canonicalMcpUrl` context given; using "
                f"placeholder {url!r}. Pass -c canonicalMcpUrl=https://host/mcp.",
            )
        url = url.rstrip("/")
        if urlsplit(url).path != "/mcp":
            cdk.Annotations.of(self).add_warning_v2(
                "wiki:canonical-path",
                f"Canonical MCP URL {url!r} does not end in /mcp; the API only routes /mcp.",
            )
        return url

    @staticmethod
    def _metadata_url(canonical: str) -> str:
        """RFC 9728 path-suffixed metadata URL for ``canonical``."""
        parts = urlsplit(canonical)
        return f"{parts.scheme}://{parts.netloc}{METADATA_PATH}{parts.path}"

    # ------------------------------------------------------------------ code

    def _code(self, name: str) -> lambda_.Code:
        path = self._code_root / name
        if path.is_dir():
            return lambda_.Code.from_asset(str(path))
        cdk.Annotations.of(self).add_warning_v2(
            f"wiki:code-missing:{name}",
            f"{path} does not exist; using an inline placeholder. Run `make build` "
            "before deploying.",
        )
        return lambda_.Code.from_inline(PLACEHOLDER_CODE)

    def _log_group(self, id: str) -> logs.LogGroup:
        return logs.LogGroup(
            self,
            id,
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=self._log_removal,
        )

    # ------------------------------------------------------------- authorizer

    def _create_authorizer(self, env: dict[str, str]) -> lambda_.Function:
        role = iam.Role(
            self,
            "AuthorizerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="wiki authorizer: JWT validation only, no data permissions (§8.8)",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        return lambda_.Function(
            self,
            "Authorizer",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            code=self._code("authorizer"),
            handler="app.authorizer.handler.handle",
            role=role,
            memory_size=256,
            timeout=Duration.seconds(10),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self._log_group("AuthorizerLogs"),
            environment=dict(env),
            description="wiki REQUEST authorizer (AS-4)",
        )

    # ------------------------------------------------------------- data plane

    def _create_mcp_role(self) -> iam.Role:
        return iam.Role(
            self,
            "McpRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="wiki data plane: grant table read-only + AssumeRole; no S3 (§8.8)",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess"),
            ],
        )

    def _grant_mcp_role(self, role: iam.Role) -> None:
        table = self.storage.table
        role.add_to_policy(
            iam.PolicyStatement(
                sid="GrantTableReadOnly",
                actions=["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"],
                resources=[table.table_arn, f"{table.table_arn}/index/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeStorageRole",
                actions=["sts:AssumeRole"],
                resources=[self.storage_role.role_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="TableKeyDecrypt",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self.storage.key.key_arn],
            )
        )
        ratelimit = getattr(self.storage, "ratelimit_table", None)
        if ratelimit is not None:
            # Counters only (§12.6). This is the one write the MCP role holds, on a
            # table that carries no grants and no content.
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="RateLimitCounters",
                    actions=["dynamodb:UpdateItem"],
                    resources=[ratelimit.table_arn],
                )
            )

    def _create_mcp_fn(self, role: iam.Role, env: dict[str, str]) -> lambda_.Function:
        return lambda_.Function(
            self,
            "Mcp",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            code=self._code("mcp"),
            handler="app.mcp.handler.handle",
            role=role,
            memory_size=512,
            timeout=Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self._log_group("McpLogs"),
            environment={
                **env,
                "WIKI_BUCKET": self.storage.bucket.bucket_name,
                "GRANT_TABLE": self.storage.table.table_name,
                "STORAGE_ROLE_ARN": self.storage_role.role_arn,
                "KMS_KEY_ARN": self.storage.key.key_arn,
                "ALLOWED_ORIGINS": ",".join(self.cfg.allowed_origins),
                "CREDENTIAL_CACHE_SECONDS": str(self.cfg.credential_cache_seconds),
                "MCP_STRICT_HEADERS": "true" if self.cfg.strict_mcp_headers else "false",
                "MCP_PROTOCOL_VERSION": MCP_PROTOCOL_VERSION,
                "RATELIMIT_TABLE": self._ratelimit_table_name(),
            },
            description="wiki MCP data plane (§8.1)",
        )

    def _ratelimit_table_name(self) -> str:
        table = getattr(self.storage, "ratelimit_table", None)
        return table.table_name if table is not None else ""

    # -------------------------------------------------------------- web app

    def _create_web_role(self) -> iam.Role:
        return iam.Role(
            self,
            "WebRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="wiki web application: the only role that can write grants (§8.8)",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSXRayDaemonWriteAccess"),
            ],
        )

    def _create_web_secret(self) -> secretsmanager.Secret:
        """One JSON secret: client_secret + api_key (operator fills from WorkOS) and a
        generated session_key (48 alphanumerics → 36 bytes after base64url)."""
        return secretsmanager.Secret(
            self,
            "WebSecret",
            description="wiki web app: AuthKit client secret, WorkOS API key, session key",
            # AWS-managed key: a CMK here would put a key-policy grant for the web role
            # into the storage stack and cycle the dependency graph.
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {"client_secret": "REPLACE-ME", "api_key": "REPLACE-ME"}
                ),
                generate_string_key="session_key",
                password_length=48,
                exclude_punctuation=True,
                include_space=False,
            ),
            removal_policy=RemovalPolicy.RETAIN if self.cfg.retain_data else RemovalPolicy.DESTROY,
        )

    def _grant_web_role(self, role: iam.Role) -> None:
        table = self.storage.table
        role.add_to_policy(
            iam.PolicyStatement(
                sid="GrantTableReadWrite",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:BatchGetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[table.table_arn, f"{table.table_arn}/index/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeStorageRole",
                actions=["sts:AssumeRole"],
                resources=[self.storage_role.role_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="KeyUse",
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[self.storage.key.key_arn],
            )
        )
        self.web_secret.grant_read(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AuditLogQueries",
                actions=["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"],
                resources=[self.mcp_fn.log_group.log_group_arn],
            )
        )

    def _web_client_id(self) -> str:
        """``workos_web_client_id`` from config, else the ``workosWebClientId`` context.

        Empty means every browser login fails at the redirect, so it is never silent:
        a warning in dev (synth before WorkOS exists is a legitimate step), an error in
        prod (synth fails).
        """
        client_id = self.cfg.workos_web_client_id or str(
            self.node.try_get_context("workosWebClientId") or ""
        )
        if client_id:
            return client_id
        message = (
            "No WorkOS client id for the web application: set `workos_web_client_id` in "
            f"infra/config.py for env {self.cfg.name!r} or pass -c workosWebClientId=client_... "
            "(docs/DEPLOY.md §5 step 7). WORKOS_CLIENT_ID would deploy empty."
        )
        if self.cfg.name == "prod":
            cdk.Annotations.of(self).add_error(message)
        else:
            cdk.Annotations.of(self).add_warning_v2("wiki:web-client-id", message)
        return ""

    def _create_web_fn(self, role: iam.Role, env: dict[str, str]) -> lambda_.Function:
        root = self.canonical_mcp_url.removesuffix("/mcp")
        return lambda_.Function(
            self,
            "Web",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            code=self._code("mcp"),  # same package; different handler
            handler="app.web.handler.handle",
            role=role,
            memory_size=512,
            timeout=Duration.seconds(30),
            tracing=lambda_.Tracing.ACTIVE,
            log_group=self._log_group("WebLogs"),
            environment={
                **env,
                "WIKI_BUCKET": self.storage.bucket.bucket_name,
                "GRANT_TABLE": self.storage.table.table_name,
                "STORAGE_ROLE_ARN": self.storage_role.role_arn,
                "KMS_KEY_ARN": self.storage.key.key_arn,
                "CREDENTIAL_CACHE_SECONDS": str(self.cfg.credential_cache_seconds),
                "WEB_BASE_URL": f"{root}/app",
                "WORKOS_CLIENT_ID": self._web_client_id(),
                "WEB_SECRET_ARN": self.web_secret.secret_arn,
                "SESSION_HOURS": "12",
                "MCP_LOG_GROUP": self.mcp_fn.log_group.log_group_name,
            },
            description="wiki web application: read view and admin console (§11.6)",
        )

    # ----------------------------------------------------------- storage role

    def _create_storage_role(self, mcp_role: iam.Role, web_role: iam.Role) -> iam.Role:
        """The outer bound: union of the §8.5 shapes over ``a/*`` and the key.
        Assumed by the MCP function and the web application, never by a person."""
        bucket = self.storage.bucket
        outer_bound = iam.PolicyDocument(
            statements=[
                iam.PolicyStatement(
                    sid="ObjectReadWrite",
                    actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
                    resources=[bucket.arn_for_objects("a/*")],
                ),
                iam.PolicyStatement(
                    sid="TagListingsOnly",
                    actions=["s3:PutObjectTagging"],
                    resources=[bucket.arn_for_objects("a/*_listing.json")],
                ),
                iam.PolicyStatement(
                    sid="ListWithinArticles",
                    actions=["s3:ListBucket", "s3:ListBucketVersions"],
                    resources=[bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": ["a/*"]}},
                ),
                iam.PolicyStatement(
                    sid="KeyUse",
                    actions=["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                    resources=[self.storage.key.key_arn],
                ),
            ]
        )
        return iam.Role(
            self,
            "StorageRole",
            assumed_by=iam.CompositePrincipal(mcp_role, web_role),
            max_session_duration=Duration.hours(1),
            description="wiki storage role: assumed per operation with a session policy (§8.5)",
            inline_policies={"OuterBound": outer_bound},
        )
