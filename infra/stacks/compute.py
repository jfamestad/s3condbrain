"""Compute stack — HANDOFF §8.1, §8.8, §11.2, build steps 5 and 8.

Two functions, Python 3.13, ARM64, code from ``build/authorizer`` and ``build/mcp``
(produced by ``make build``):

* ``authorizer`` — handler ``app.authorizer.handler.handle``. Role: logs only.
  Env: AUTHKIT_DOMAIN, JWKS_URL, CANONICAL_MCP_URL, RESOURCE_METADATA_URL, LOG_LEVEL.
* ``mcp`` — handler ``app.mcp.handler.handle``. Role: ``dynamodb:GetItem``,
  ``BatchGetItem``, ``Query`` on the grant table and its index (READ-ONLY);
  ``sts:AssumeRole`` on the storage role; ``kms:Decrypt`` for the table; **no S3**.
  Env: everything in ``app.config.Settings``.

The storage role's trust policy must allow the mcp function's role to assume it
(cross-stack: pass the role in and add the trust in this stack, or grant here).

Exposes ``authorizer_fn`` and ``mcp_fn``.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.storage import StorageStack


class ComputeStack(cdk.Stack):
    def __init__(
        self, scope: Construct, id: str, *, cfg: EnvConfig, storage: StorageStack, **kwargs
    ) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        raise NotImplementedError
