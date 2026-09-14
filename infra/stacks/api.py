"""API stack — HANDOFF §9, build step 6.

REST API (regional), Lambda REQUEST authorizer with **results cache TTL 0**, identity
source ``method.request.header.Authorization``.

Routes (§9.2):
    POST    /mcp                                        authorizer → mcp_fn proxy
    GET     /mcp, DELETE /mcp                           mock → 405
    OPTIONS /mcp                                        mock → 204 + CORS
    GET     /.well-known/oauth-protected-resource       mock, static JSON
    GET     /.well-known/oauth-protected-resource/mcp   mock, static JSON

Gateway responses (§9.3):
    UNAUTHORIZED                 401  WWW-Authenticate + Access-Control-Allow-Origin
                                      + Access-Control-Expose-Headers: WWW-Authenticate
    MISSING_AUTHENTICATION_TOKEN 404  empty body
    DEFAULT_4XX / DEFAULT_5XX    generic body, no request echo

CORS on /mcp (§9.4): allowOrigins = cfg.allowed_origins; allowMethods POST, OPTIONS;
allowHeaders authorization, content-type, mcp-protocol-version, mcp-method, mcp-name,
mcp-param-*; exposeHeaders WWW-Authenticate.

Throttle (§12.6): 20 rps steady, 50 burst on the stage. Monthly cost budget of 20 USD;
an email subscriber is attached when context ``alertEmail`` is given.

Custom domain (§9.5) when ``cfg.domain``: ACM cert (DNS-validated in the hosted zone
if given, else imported from context ``certificateArn`` — an error annotation when
absent), regional base path mapping ``(none)``, alias record when a zone is given.
Without a domain the stage URL is output and CANONICAL_MCP_URL must be provided.

Protected resource metadata body (AS-2):
    {"resource": "<canonical>", "authorization_servers": ["<authkit>"],
     "scopes_supported": ["wiki.read", "wiki.write"], "bearer_methods_supported": ["header"]}

Outputs: ApiUrl, CanonicalMcpUrl, ResourceMetadataUrl, DomainTarget (custom domain only).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.compute import ComputeStack

STAGE_NAME = "v1"
THROTTLE_RATE = 20
THROTTLE_BURST = 50
MONTHLY_BUDGET_USD = 20
CORS_ALLOW_HEADERS = [
    "authorization",
    "content-type",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "mcp-param-*",
]
SCOPES_SUPPORTED = ["wiki.read", "wiki.write"]


def _quoted(value: str) -> str:
    """API Gateway response-parameter literal: single-quoted string."""
    return f"'{value}'"


class ApiStack(cdk.Stack):
    """REST API, routes, gateway responses, CORS, domain, throttle, budget."""

    def __init__(
        self, scope: Construct, id: str, *, cfg: EnvConfig, compute: ComputeStack, **kwargs
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        self.compute = compute
        self.canonical_mcp_url = compute.canonical_mcp_url
        self.resource_metadata_url = compute.resource_metadata_url
        # Import the functions by ARN so invoke permissions are created in THIS stack;
        # otherwise CDK puts them next to the function and compute -> api -> compute cycles.
        self._authorizer_fn = self._import_fn("AuthorizerFn", compute.authorizer_fn)
        self._mcp_fn = self._import_fn("McpFn", compute.mcp_fn)
        self._web_fn = self._import_fn("WebFn", compute.web_fn)

        self.api = self._create_api()
        authorizer = self._create_authorizer()
        self._add_gateway_responses()
        self._add_mcp_routes(authorizer)
        self._add_metadata_routes()
        self._add_web_routes()
        self._add_custom_domain()
        self._add_budget()

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url)
        cdk.CfnOutput(self, "CanonicalMcpUrl", value=self.canonical_mcp_url)
        cdk.CfnOutput(self, "ResourceMetadataUrl", value=self.resource_metadata_url)

    # ------------------------------------------------------------------- api

    def _import_fn(self, id: str, fn: lambda_.IFunction) -> lambda_.IFunction:
        return lambda_.Function.from_function_attributes(
            self, id, function_arn=fn.function_arn, same_environment=True
        )

    def _create_api(self) -> apigateway.RestApi:
        return apigateway.RestApi(
            self,
            "Api",
            rest_api_name=f"wiki-{self.cfg.name}",
            description="wiki MCP server (HANDOFF §9)",
            endpoint_types=[apigateway.EndpointType.REGIONAL],
            cloud_watch_role=True,
            deploy_options=apigateway.StageOptions(
                stage_name=STAGE_NAME,
                throttling_rate_limit=THROTTLE_RATE,
                throttling_burst_limit=THROTTLE_BURST,
                logging_level=apigateway.MethodLoggingLevel.ERROR,
                metrics_enabled=True,
                tracing_enabled=True,
            ),
        )

    def _create_authorizer(self) -> apigateway.RequestAuthorizer:
        # AS-6: results cache TTL zero. The synth test asserts the template carries an
        # explicit 0 — API Gateway defaults an absent TTL to 300 s.
        return apigateway.RequestAuthorizer(
            self,
            "JwtAuthorizer",
            handler=self._authorizer_fn,
            identity_sources=[apigateway.IdentitySource.header("Authorization")],
            results_cache_ttl=Duration.seconds(0),
        )

    # ----------------------------------------------------- gateway responses

    def _add_gateway_responses(self) -> None:
        challenge = f'Bearer resource_metadata="{self.resource_metadata_url}", scope="wiki.read"'
        self.api.add_gateway_response(
            "Unauthorized",
            type=apigateway.ResponseType.UNAUTHORIZED,
            status_code="401",
            response_headers={
                "WWW-Authenticate": _quoted(challenge),
                "Access-Control-Allow-Origin": _quoted(self.cfg.allowed_origins[0]),
                "Access-Control-Expose-Headers": _quoted("WWW-Authenticate"),
            },
            templates={"application/json": '{"error":"unauthorized"}'},
        )
        self.api.add_gateway_response(
            "UnknownRoute",
            type=apigateway.ResponseType.MISSING_AUTHENTICATION_TOKEN,
            status_code="404",
            templates={"application/json": "{}"},
        )
        self.api.add_gateway_response(
            "Default4xx",
            type=apigateway.ResponseType.DEFAULT_4_XX,
            templates={"application/json": '{"error":"request refused"}'},
        )
        self.api.add_gateway_response(
            "Default5xx",
            type=apigateway.ResponseType.DEFAULT_5_XX,
            status_code="500",
            templates={"application/json": '{"error":"server error"}'},
        )

    # ---------------------------------------------------------------- routes

    def _add_mcp_routes(self, authorizer: apigateway.RequestAuthorizer) -> None:
        mcp = self.api.root.add_resource("mcp")
        mcp.add_method(
            "POST",
            apigateway.LambdaIntegration(self._mcp_fn, proxy=True),
            authorizer=authorizer,
            authorization_type=apigateway.AuthorizationType.CUSTOM,
        )

        not_allowed = apigateway.MockIntegration(
            request_templates={"application/json": '{"statusCode": 405}'},
            passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
            integration_responses=[
                apigateway.IntegrationResponse(
                    status_code="405",
                    response_parameters={"method.response.header.Allow": _quoted("POST, OPTIONS")},
                    response_templates={"application/json": '{"error":"method not allowed"}'},
                )
            ],
        )
        not_allowed_response = apigateway.MethodResponse(
            status_code="405",
            response_parameters={"method.response.header.Allow": True},
        )
        for verb in ("GET", "DELETE"):
            mcp.add_method(
                verb,
                not_allowed,
                authorization_type=apigateway.AuthorizationType.NONE,
                method_responses=[not_allowed_response],
            )

        mcp.add_cors_preflight(
            allow_origins=list(self.cfg.allowed_origins),
            allow_methods=["POST", "OPTIONS"],
            allow_headers=CORS_ALLOW_HEADERS,
            expose_headers=["WWW-Authenticate"],
            max_age=Duration.hours(1),
        )

    def _add_web_routes(self) -> None:
        """``/app`` and ``/app/{proxy+}``: the web application (§11.6). No gateway
        authorizer — it authenticates with its own session cookie; the function
        answers every path under the prefix, including its own 404 page."""
        integration = apigateway.LambdaIntegration(self._web_fn, proxy=True)
        app = self.api.root.add_resource("app")
        app.add_method("ANY", integration, authorization_type=apigateway.AuthorizationType.NONE)
        app.add_resource("{proxy+}").add_method(
            "ANY", integration, authorization_type=apigateway.AuthorizationType.NONE
        )

    def _add_metadata_routes(self) -> None:
        body = {
            "resource": self.canonical_mcp_url,
            "authorization_servers": [self.cfg.authkit_domain.rstrip("/")],
            "scopes_supported": SCOPES_SUPPORTED,
            "bearer_methods_supported": ["header"],
        }
        integration = apigateway.MockIntegration(
            request_templates={"application/json": '{"statusCode": 200}'},
            passthrough_behavior=apigateway.PassthroughBehavior.NEVER,
            integration_responses=[
                apigateway.IntegrationResponse(
                    status_code="200",
                    response_parameters={
                        "method.response.header.Content-Type": _quoted("application/json"),
                        "method.response.header.Access-Control-Allow-Origin": _quoted("*"),
                        "method.response.header.Cache-Control": _quoted("public, max-age=3600"),
                    },
                    response_templates={
                        "application/json": json.dumps(body, separators=(",", ":"))
                    },
                )
            ],
        )
        method_response = apigateway.MethodResponse(
            status_code="200",
            response_parameters={
                "method.response.header.Content-Type": True,
                "method.response.header.Access-Control-Allow-Origin": True,
                "method.response.header.Cache-Control": True,
            },
        )
        well_known = self.api.root.add_resource(".well-known")
        metadata = well_known.add_resource("oauth-protected-resource")
        for resource in (metadata, metadata.add_resource("mcp")):
            resource.add_method(
                "GET",
                integration,
                authorization_type=apigateway.AuthorizationType.NONE,
                method_responses=[method_response],
            )

    # ---------------------------------------------------------------- domain

    def _add_custom_domain(self) -> None:
        if not self.cfg.domain:
            return

        zone: route53.IHostedZone | None = None
        if self.cfg.hosted_zone_name:
            zone = route53.HostedZone.from_lookup(
                self, "Zone", domain_name=self.cfg.hosted_zone_name
            )
            certificate: acm.ICertificate = acm.Certificate(
                self,
                "Certificate",
                domain_name=self.cfg.domain,
                validation=acm.CertificateValidation.from_dns(zone),
            )
        else:
            cert_arn = self.node.try_get_context("certificateArn")
            if not cert_arn:
                cdk.Annotations.of(self).add_error(
                    f"cfg.domain={self.cfg.domain!r} has no hosted zone; pass "
                    "-c certificateArn=arn:aws:acm:... (a regional certificate for that "
                    "name) or set hosted_zone_name to have one issued."
                )
                return
            certificate = acm.Certificate.from_certificate_arn(self, "Certificate", cert_arn)

        domain = apigateway.DomainName(
            self,
            "Domain",
            domain_name=self.cfg.domain,
            certificate=certificate,
            endpoint_type=apigateway.EndpointType.REGIONAL,
            security_policy=apigateway.SecurityPolicy.TLS_1_2,
        )
        apigateway.BasePathMapping(
            self,
            "RootMapping",
            domain_name=domain,
            rest_api=self.api,
            stage=self.api.deployment_stage,
        )
        if zone is not None:
            route53.ARecord(
                self,
                "Alias",
                zone=zone,
                record_name=self.cfg.domain,
                target=route53.RecordTarget.from_alias(targets.ApiGatewayDomain(domain)),
            )
        cdk.CfnOutput(self, "DomainTarget", value=domain.domain_name_alias_domain_name)

    # ---------------------------------------------------------------- budget

    def _add_budget(self) -> None:
        alert_email = self.node.try_get_context("alertEmail")
        subscribers = None
        if alert_email:
            subscribers = [
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL", address=alert_email
                        )
                    ],
                )
            ]
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"wiki-{self.cfg.name}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=MONTHLY_BUDGET_USD, unit="USD"),
            ),
            notifications_with_subscribers=subscribers,
        )
