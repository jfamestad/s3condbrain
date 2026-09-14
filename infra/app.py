"""CDK app. ``cdk synth -c env=dev``.

Context keys:
    env              ``dev`` (default) or ``prod``.
    codeRoot         Directory holding ``authorizer/`` and ``mcp/`` (default ``build``).
    canonicalMcpUrl  Required when the environment has no ``domain``.
    certificateArn   Required when ``domain`` is set but ``hosted_zone_name`` is not.
    alertEmail       Optional subscriber for the budget notification and the alerts topic.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk

from infra.config import load
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.ops import OpsStack
from infra.stacks.storage import StorageStack

app = cdk.App()
env_name = app.node.try_get_context("env") or "dev"
cfg = load(env_name)
aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
prefix = f"wiki-{cfg.name}"
code_root = Path(app.node.try_get_context("codeRoot") or "build")

storage = StorageStack(app, f"{prefix}-storage", cfg=cfg, env=aws_env)
compute = ComputeStack(
    app, f"{prefix}-compute", cfg=cfg, storage=storage, code_root=code_root, env=aws_env
)
api = ApiStack(app, f"{prefix}-api", cfg=cfg, compute=compute, env=aws_env)
ops = OpsStack(
    app, f"{prefix}-ops", cfg=cfg, storage=storage, compute=compute, api=api, env=aws_env
)

app.synth()
