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

Throttle (§12.6): 20 rps steady, 50 burst on the stage. Billing alarm.

Custom domain (§9.5) when ``cfg.domain``: ACM cert (DNS-validated in the hosted zone
if given, else imported by ARN from context), regional base path mapping ``(none)``.
Without a domain the stage URL is output and CANONICAL_MCP_URL must be provided.

Protected resource metadata body (AS-2):
    {"resource": "<canonical>", "authorization_servers": ["<authkit>"],
     "scopes_supported": ["wiki.read", "wiki.write"], "bearer_methods_supported": ["header"]}

Outputs: api url, canonical mcp url, metadata url.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.compute import ComputeStack


class ApiStack(cdk.Stack):
    def __init__(
        self, scope: Construct, id: str, *, cfg: EnvConfig, compute: ComputeStack, **kwargs
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        raise NotImplementedError
