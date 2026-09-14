"""CDK app. ``cdk synth -c env=dev``.

Context keys:
    env              ``dev`` (default) or ``prod``.
    codeRoot         Directory holding ``authorizer/`` and ``mcp/`` (default ``build``).
    canonicalMcpUrl  Required when the environment has no ``domain``.
    certificateArn   Optional override; without it the API stack reads the ARN that
                     ``infra/cert_app.py`` (deployed by hand) published to SSM.
    alertEmail       Optional subscriber for the budget notification and the alerts topic.
    workosWebClientId
                     The web application's AuthKit client id when ``infra/config.py``
                     leaves ``workos_web_client_id`` empty.
    prodAccount      Overrides ``account`` for prod so CI can synth without pinning
                     the real id in ``infra/config.py``.

Before any stack is built, ``guard_account`` (``infra/config.py``) refuses an
unpinned prod and any env whose pinned account differs from the one the CLI's
credentials resolve to (``CDK_DEFAULT_ACCOUNT``) — HANDOFF §9.6.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

from infra.config import EnvConfig, guard_account, load
from infra.stacks.api import ApiStack
from infra.stacks.compute import ComputeStack
from infra.stacks.ops import OpsStack
from infra.stacks.storage import StorageStack


def build(app: cdk.App) -> EnvConfig:
    """Add the four stacks for the env named in context to ``app``.

    Args:
        app: The CDK app; context is read from it.

    Returns:
        The environment config the stacks were built from.

    Raises:
        SystemExit: Unknown env, or ``guard_account`` refused the account.
    """
    env_name = app.node.try_get_context("env") or "dev"
    cfg = load(env_name, prod_account=app.node.try_get_context("prodAccount"))
    guard_account(cfg, os.environ.get("CDK_DEFAULT_ACCOUNT"))
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    prefix = f"wiki-{cfg.name}"
    code_root = Path(app.node.try_get_context("codeRoot") or "build")

    storage = StorageStack(app, f"{prefix}-storage", cfg=cfg, env=aws_env)
    compute = ComputeStack(
        app, f"{prefix}-compute", cfg=cfg, storage=storage, code_root=code_root, env=aws_env
    )
    api = ApiStack(app, f"{prefix}-api", cfg=cfg, compute=compute, env=aws_env)
    OpsStack(app, f"{prefix}-ops", cfg=cfg, storage=storage, compute=compute, api=api, env=aws_env)
    return cfg


if __name__ == "__main__":
    app = cdk.App()
    cfg = build(app)
    # One line the operator can check against `aws sts get-caller-identity`
    # (docs/DEPLOY.md, first-deploy checklist step 5). stderr: `cdk synth` owns stdout.
    print(
        f"wiki: env={cfg.name} account={cfg.account or '<floating: CLI credentials>'} "
        f"region={cfg.region}",
        file=sys.stderr,
    )
    app.synth()
