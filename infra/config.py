"""Per-environment infrastructure configuration — HANDOFF §9.6.

Separate AWS accounts per environment, never separate stacks in one account.
``object_lock`` is prod-only (§8.2). Domain and hosted zone are optional so the
prototype can synth and deploy before DNS exists; when absent the API's default
execute-api URL is used and CANONICAL_MCP_URL must be set explicitly at deploy.

``account`` is the guard against deploying to the wrong account (§9.6). Production
must pin it; ``guard_account`` refuses to synth prod otherwise, and refuses any env
whose pinned account differs from the one the CDK CLI's credentials resolve to.
Development may float (``None`` — the CLI's current account is used).

The values in ``ENVIRONMENTS`` are placeholders (``example.com``, a zero account).
An operator's real account ids, hostnames and AuthKit client ids live in
``infra/environments.toml``, which is gitignored and merged over the placeholders by
``load`` — copy ``infra/environments.example.toml`` to start one. ``WIKI_ENV_FILE``
points ``load`` at a different file; set it empty to use the placeholders alone.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

ACCOUNT_ID_RE = re.compile(r"\d{12}")
LOCAL_CONFIG = Path(__file__).with_name("environments.toml")


@dataclass(frozen=True)
class EnvConfig:
    """One deployable environment.

    Attributes:
        name: ``dev`` or ``prod``.
        account: AWS account id, or ``None`` to use the CLI's current account.
            Never ``None`` for prod — ``guard_account`` refuses it.
        region: AWS region.
        domain: Public hostname, e.g. ``wiki-dev.example.com``; ``None`` for no custom domain.
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
        # Placeholder pin: synth against real credentials is refused until
        # infra/environments.toml supplies the real account id.
        account="000000000000",
        region="us-west-2",
        domain="wiki-dev.example.com",
        hosted_zone_name=None,
        authkit_domain="https://REPLACE-ME-staging.authkit.app",
        object_lock=False,
        retain_data=False,
        # The web application's own Connect OAuth client (confidential); its secret
        # lives in the WebSecret, never here. docs/DEPLOY.md §5.
        workos_web_client_id="client_REPLACE_ME",
        strict_mcp_headers=False,
    ),
    "prod": EnvConfig(
        name="prod",
        # The production account id — first-deploy checklist step 1 (docs/DEPLOY.md).
        # `None` here refuses to synth; CI passes `-c prodAccount=` to synth without it.
        account=None,
        region="us-west-2",
        domain="wiki.example.com",
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


def _local_overrides() -> dict[str, dict[str, object]]:
    """Read the operator's per-environment values, or ``{}`` when there are none.

    Returns:
        ``{env name: {field: value}}`` from ``WIKI_ENV_FILE`` if set (empty means
        none), else from ``infra/environments.toml`` when that file exists.

    Raises:
        SystemExit: ``WIKI_ENV_FILE`` names a missing file, or the file is not valid TOML.
    """
    configured = os.environ.get("WIKI_ENV_FILE")
    if configured == "":
        return {}
    path = Path(configured) if configured else LOCAL_CONFIG
    if not path.exists():
        if configured:
            raise SystemExit(f"WIKI_ENV_FILE={configured} does not exist")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: {e}") from e


def load(name: str, *, prod_account: str | None = None) -> EnvConfig:
    """Return the config for ``name``, with the operator's local values merged in.

    Args:
        name: Environment name, a key of ``ENVIRONMENTS``.
        prod_account: Overrides ``account`` for prod only — the CDK context key
            ``prodAccount``. Exists so CI can synth prod without a pinned account in
            this file; a deploy still needs the real id here or on the command line.

    Raises:
        SystemExit: Unknown environment name, or the local file sets a field
            ``EnvConfig`` does not have.
    """
    try:
        cfg = ENVIRONMENTS[name]
    except KeyError as e:
        raise SystemExit(f"unknown env {name!r}; expected one of {sorted(ENVIRONMENTS)}") from e
    local = dict(_local_overrides().get(name, {}))
    unknown = set(local) - ({f.name for f in fields(EnvConfig)} - {"name"})
    if unknown:
        raise SystemExit(f"environments.toml [{name}]: unknown or fixed keys {sorted(unknown)}")
    if "allowed_origins" in local:
        local["allowed_origins"] = tuple(local["allowed_origins"])  # type: ignore[arg-type]
    cfg = replace(cfg, **local)  # type: ignore[arg-type]
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
            "env 'prod' has account=None. Production must be pinned to its 12-digit AWS "
            "account id in infra/environments.toml — docs/DEPLOY.md, first-deploy "
            "checklist step 1 — "
            "or, for a credential-less synth in CI, passed as -c prodAccount=<id>."
        )
    if cfg.account is not None and not ACCOUNT_ID_RE.fullmatch(cfg.account):
        raise SystemExit(
            f"the config pins account={cfg.account!r} for env {cfg.name}; "
            "an AWS account id is exactly 12 digits."
        )
    if cfg.account is not None and resolved_account and resolved_account != cfg.account:
        raise SystemExit(
            f"credentials resolve to account {resolved_account} but the config pins "
            f"{cfg.account} for env {cfg.name} (infra/environments.toml)"
        )
