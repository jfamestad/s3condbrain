"""CDK app for the certificate stack only. Deployed by hand (``make cert-deploy``),
never by CI — see ``infra/stacks/certificate.py`` for why.

    npx cdk --app "uv run python -m infra.cert_app" deploy -c env=dev

Context keys: ``env`` (``dev`` default, or ``prod``), ``prodAccount`` (same override
as the main app). The same account guard applies: a pinned env whose account differs
from the CLI's credentials is refused before any stack exists.
"""

from __future__ import annotations

import os
import sys

import aws_cdk as cdk

from infra.config import EnvConfig, guard_account, load
from infra.stacks.certificate import CertificateStack


def build(app: cdk.App) -> EnvConfig:
    """Add the certificate stack for the env named in context.

    Raises:
        SystemExit: Unknown env, or ``guard_account`` refused the account.
        ValueError: The env has no domain.
    """
    env_name = app.node.try_get_context("env") or "dev"
    cfg = load(env_name, prod_account=app.node.try_get_context("prodAccount"))
    guard_account(cfg, os.environ.get("CDK_DEFAULT_ACCOUNT"))
    aws_env = cdk.Environment(account=cfg.account, region=cfg.region)
    CertificateStack(app, f"wiki-{cfg.name}-certificate", cfg=cfg, env=aws_env)
    return cfg


if __name__ == "__main__":
    app = cdk.App()
    cfg = build(app)
    print(
        f"wiki certificate: env={cfg.name} domain={cfg.domain} "
        f"account={cfg.account or '<floating: CLI credentials>'} region={cfg.region}",
        file=sys.stderr,
    )
    app.synth()
