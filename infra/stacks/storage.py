"""Storage stack — HANDOFF §8.2, §8.8, §8.9, build step 1.

Creates: KMS key · bucket (versioned, SSE-KMS, public access blocked, Object Lock
prod-only with 1-year governance default retention, lifecycle expiring non-current
``_listing.json`` versions after retention) · grant table (pk/sk, GSI1 gs1pk/gs1sk,
pay-per-request, SSE-KMS with the same key) · storage role (outer bound: the three
shapes over ``a/*`` + the key; never DeleteObjectVersion or BypassGovernanceRetention)
· CloudWatch alarm on bucket policy change.

Exposes ``bucket``, ``table``, ``key``, ``storage_role`` as attributes for ComputeStack.
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from infra.config import EnvConfig


class StorageStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)
        self.cfg = cfg
        raise NotImplementedError
