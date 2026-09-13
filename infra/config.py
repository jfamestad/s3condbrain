"""Per-environment infrastructure configuration — HANDOFF §9.6.

Separate AWS accounts per environment, never separate stacks in one account.
``object_lock`` is prod-only (§8.2). Domain and hosted zone are optional so the
prototype can synth and deploy before DNS exists; when absent the API's default
execute-api URL is used and CANONICAL_MCP_URL must be set explicitly at deploy.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvConfig:
    """One deployable environment.

    Attributes:
        name: ``dev`` or ``prod``.
        account: AWS account id, or ``None`` to use the CLI's current account.
        region: AWS region.
        domain: Public hostname, e.g. ``wiki-dev.famestad.com``; ``None`` for no custom domain.
        hosted_zone_name: Route53 zone that owns ``domain``; ``None`` to skip DNS records.
        authkit_domain: Token issuer for this environment.
        object_lock: Enable Object Lock (governance, 1-year default retention).
        retain_data: RETAIN removal policy on bucket/table/key.
        allowed_origins: CORS + Origin-validation allowlist.
        credential_cache_seconds: Passed through to the function.
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

    @property
    def canonical_mcp_url(self) -> str | None:
        return f"https://{self.domain}/mcp" if self.domain else None


ENVIRONMENTS: dict[str, EnvConfig] = {
    "dev": EnvConfig(
        name="dev",
        account=None,
        region="us-west-2",
        domain="wiki-dev.famestad.com",
        hosted_zone_name=None,
        authkit_domain="https://REPLACE-ME.authkit.app",
        object_lock=False,
        retain_data=False,
    ),
    "prod": EnvConfig(
        name="prod",
        account=None,
        region="us-west-2",
        domain="wiki.famestad.com",
        hosted_zone_name=None,
        authkit_domain="https://REPLACE-ME.authkit.app",
        object_lock=True,
        retain_data=True,
    ),
}


def load(name: str) -> EnvConfig:
    try:
        return ENVIRONMENTS[name]
    except KeyError as e:
        raise SystemExit(f"unknown env {name!r}; expected one of {sorted(ENVIRONMENTS)}") from e
