"""Per-environment infrastructure configuration — HANDOFF §9.6.

Separate AWS accounts per environment, never separate stacks in one account.
``object_lock`` is prod-only (§8.2). Domain and hosted zone are optional so the
prototype can synth and deploy before DNS exists; when absent the API's default
execute-api URL is used and CANONICAL_MCP_URL must be set explicitly at deploy.

``account`` is the guard against deploying to the wrong account (§9.6). Production
must pin it; ``guard_account`` refuses to synth prod otherwise, and refuses any env
whose pinned account differs from the one the CDK CLI's credentials resolve to.
Development may float (``None`` — the CLI's current account is used).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

ACCOUNT_ID_RE = re.compile(r"\d{12}")


@dataclass(frozen=True)
class EnvConfig:
    """One deployable environment.

    Attributes:
        name: ``dev`` or ``prod``.
        account: AWS account id, or ``None`` to use the CLI's current account.
            Never ``None`` for prod — ``guard_account`` refuses it.
        region: AWS region.
        domain: Public hostname, e.g. ``wiki-dev.famestad.com``; ``None`` for no custom domain.
        hosted_zone_name: Route53 zone that owns ``domain``; ``None`` to skip DNS records.
        authkit_domain: Token issuer for this environment.
        object_lock: Enable Object Lock (governance, 1-year default retention).
        retain_data: RETAIN removal policy on bucket/table/key.
        allowed_origins: CORS + Origin-validation allowlist.
        credential_cache_seconds: Passed through to the function.
        workos_web_client_id: The web app's own AuthKit client (increment F). Empty is
            a synth warning in dev and a synth error in prod.
        strict_mcp_headers: Reject POSTs that omit the ``Mcp-*`` headers §6.2 calls
            mandatory (``MCP_STRICT_HEADERS``). Off until a real client is observed
            sending them; see the first-deploy checklist in ``docs/DEPLOY.md``.
        log_retention_days: CloudTrail log group and log-bucket retention (§12.7: ninety).
        backup_retention_days: How long each daily grant-table snapshot is kept (§12.5).
    """

    name: str
    account: str | None
    region: str
    domain: str | None
    hosted_zone_name: str | None
    authkit_domain: str
    object_lock: bool
    retain_data: bool
    allowed_origins: tuple[str, ...] = ("https://claude.ai",)
    credential_cache_seconds: int = 900
    workos_web_client_id: str = ""
    strict_mcp_headers: bool = False
    log_retention_days: int = 90
    backup_retention_days: int = 35

    @property
    def canonical_mcp_url(self) -> str | None:
        return f"https://{self.domain}/mcp" if self.domain else None


ENVIRONMENTS: dict[str, EnvConfig] = {
    "dev": EnvConfig(
        name="dev",
        account="588747760390",  # pinned; synth refuses credentials that resolve elsewhere
        region="us-west-2",
        domain="wiki-dev.famestad.com",
        hosted_zone_name=None,
        authkit_domain="https://spirited-smile-45-staging.authkit.app",
        object_lock=False,
        retain_data=False,
        strict_mcp_headers=False,
    ),
    "prod": EnvConfig(
        name="prod",
        # The production account id — first-deploy checklist step 1 (docs/DEPLOY.md).
        # `None` here refuses to synth; CI passes `-c prodAccount=` to synth without it.
        account=None,
        region="us-west-2",
        domain="wiki.famestad.com",
        hosted_zone_name=None,
        authkit_domain="https://REPLACE-ME.authkit.app",
        object_lock=True,
        retain_data=True,
        # Flip to True once a real Claude client has been observed sending the Mcp-*
        # headers — HANDOFF §6.2 calls them mandatory; the safety valve stays off until
        # then. First-deploy checklist, post-launch item.
        strict_mcp_headers=False,
    ),
}


def load(name: str, *, prod_account: str | None = None) -> EnvConfig:
    """Return the config for ``name``.

    Args:
        name: Environment name, a key of ``ENVIRONMENTS``.
        prod_account: Overrides ``account`` for prod only — the CDK context key
            ``prodAccount``. Exists so CI can synth prod without a pinned account in
            this file; a deploy still needs the real id here or on the command line.

    Raises:
        SystemExit: Unknown environment name.
    """
    try:
        cfg = ENVIRONMENTS[name]
    except KeyError as e:
        raise SystemExit(f"unknown env {name!r}; expected one of {sorted(ENVIRONMENTS)}") from e
    if name == "prod" and prod_account:
        cfg = replace(cfg, account=prod_account)
    return cfg


def guard_account(cfg: EnvConfig, resolved_account: str | None) -> None:
    """Refuse to synth against the wrong account (§9.6).

    Args:
        cfg: The environment about to be synthesized.
        resolved_account: The account the CDK CLI's credentials resolve to
            (``CDK_DEFAULT_ACCOUNT``), or ``None`` when the CLI had no credentials —
            a credential-less synth in CI, where only the pin itself can be checked.

    Raises:
        SystemExit: Prod with no pinned account; a pinned account that is not a
            12-digit id; or credentials that resolve to a different account than the
            one pinned for this env.
    """
    if cfg.name == "prod" and cfg.account is None:
        raise SystemExit(
            "env 'prod' has account=None in infra/config.py. Production must be pinned to "
            "its 12-digit AWS account id — docs/DEPLOY.md, first-deploy checklist step 1 — "
            "or, for a credential-less synth in CI, passed as -c prodAccount=<id>."
        )
    if cfg.account is not None and not ACCOUNT_ID_RE.fullmatch(cfg.account):
        raise SystemExit(
            f"infra/config.py pins account={cfg.account!r} for env {cfg.name}; "
            "an AWS account id is exactly 12 digits."
        )
    if cfg.account is not None and resolved_account and resolved_account != cfg.account:
        raise SystemExit(
            f"credentials resolve to account {resolved_account} but infra/config.py pins "
            f"{cfg.account} for env {cfg.name}"
        )
