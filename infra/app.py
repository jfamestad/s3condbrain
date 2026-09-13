"""CDK app. ``cdk synth -c env=dev``."""

from __future__ import annotations

import aws_cdk as cdk

from infra.config import load
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.storage import StorageStack

app = cdk.App()
env_name = app.node.try_get_context("env") or "dev"
cfg = load(env_name)
aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
prefix = f"wiki-{cfg.name}"

storage = StorageStack(app, f"{prefix}-storage", cfg=cfg, env=aws_env)
compute = ComputeStack(app, f"{prefix}-compute", cfg=cfg, storage=storage, env=aws_env)
api = ApiStack(app, f"{prefix}-api", cfg=cfg, compute=compute, env=aws_env)

app.synth()
