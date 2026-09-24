"""Runtime configuration for both Lambda functions.

Every value comes from the environment; nothing is hardcoded (HANDOFF §12.3).
The same ``Settings`` object serves the authorizer and the MCP/data-plane
function — each reads only the fields it needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    """Environment-derived configuration.

    Attributes:
        bucket: S3 bucket holding the tree (``a/`` prefix) — HANDOFF §8.2.
        grant_table: DynamoDB table holding grants and profiles — §8.5.
        storage_role_arn: Role assumed per operation with a session policy — §8.8.
        kms_key_arn: Customer-managed key for bucket and table — §8.8.
        authkit_domain: Token issuer, e.g. ``https://xyz.authkit.app`` — AS-4.
        jwks_url: JWKS endpoint used to verify signatures — AS-4.
        canonical_mcp_url: Exact ``aud`` every token must carry — §4.3.
        resource_metadata_url: Advertised in the 401 challenge — AS-3.
        allowed_origins: Origins accepted when an ``Origin`` header is present — §6.2.
        credential_cache_seconds: Lifetime of a minted credential in cache; must
            move together with token lifetime — AS-6 note, §8.5.
        strict_mcp_headers: When true, a POST missing the mandatory MCP headers is
            rejected. Default false so older clients can connect to the prototype;
            a present-but-disagreeing header is always rejected — §6.2.
        protocol_version: Protocol revision this server advertises — §6.1.
        log_level: Powertools log level.
    """

    bucket: str
    grant_table: str
    storage_role_arn: str
    kms_key_arn: str
    authkit_domain: str
    jwks_url: str
    canonical_mcp_url: str
    resource_metadata_url: str
    allowed_origins: tuple[str, ...] = field(default=("https://claude.ai",))
    credential_cache_seconds: int = 900
    strict_mcp_headers: bool = False
    # 2025-06-18, not 2026-07-28: Claude's client refuses the latter outright. The
    # deployed value comes from MCP_PROTOCOL_VERSION (infra/stacks/compute.py).
    protocol_version: str = "2025-06-18"
    log_level: str = "INFO"
    # Increment D — rate limits (§12.6). Empty table name → no-op limiter.
    ratelimit_table: str = ""
    calls_per_minute: int = 60
    writes_per_hour: int = 200
    # Increment F — web application (§11.6). The web function is its own OAuth
    # client of AuthKit; its secrets live in one Secrets Manager secret.
    web_base_url: str = ""
    workos_client_id: str = ""
    web_secret_arn: str = ""
    session_hours: int = 12

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment.

        Raises:
            RuntimeError: when a required variable is absent.
        """
        authkit = _env("AUTHKIT_DOMAIN").rstrip("/")
        mcp_url = _env("CANONICAL_MCP_URL")
        raw_origins = os.environ.get("ALLOWED_ORIGINS", "https://claude.ai")
        origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())
        return cls(
            bucket=_env("WIKI_BUCKET", ""),
            grant_table=_env("GRANT_TABLE", ""),
            storage_role_arn=_env("STORAGE_ROLE_ARN", ""),
            kms_key_arn=_env("KMS_KEY_ARN", ""),
            authkit_domain=authkit,
            jwks_url=os.environ.get("JWKS_URL") or f"{authkit}/oauth2/jwks",
            canonical_mcp_url=mcp_url,
            resource_metadata_url=os.environ.get("RESOURCE_METADATA_URL")
            or mcp_url.replace("/mcp", "/.well-known/oauth-protected-resource/mcp"),
            allowed_origins=origins,
            credential_cache_seconds=int(os.environ.get("CREDENTIAL_CACHE_SECONDS") or "900"),
            strict_mcp_headers=os.environ.get("MCP_STRICT_HEADERS", "false").lower() == "true",
            protocol_version=os.environ.get("MCP_PROTOCOL_VERSION", "2025-06-18"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            ratelimit_table=os.environ.get("RATELIMIT_TABLE", ""),
            calls_per_minute=int(os.environ.get("CALLS_PER_MINUTE") or "60"),
            writes_per_hour=int(os.environ.get("WRITES_PER_HOUR") or "200"),
            web_base_url=os.environ.get("WEB_BASE_URL", ""),
            workos_client_id=os.environ.get("WORKOS_CLIENT_ID", ""),
            web_secret_arn=os.environ.get("WEB_SECRET_ARN", ""),
            session_hours=int(os.environ.get("SESSION_HOURS") or "12"),
        )


SCOPE_READ = "wiki.read"
SCOPE_WRITE = "wiki.write"
ALL_SCOPES = (SCOPE_READ, SCOPE_WRITE)

ARTICLE_PREFIX = "a/"
MAX_ARTICLE_BYTES = 1_048_576
RESERVED_NAMES = frozenset({"index.md", "log.md"})
RESERVED_TYPES = frozenset({"pointer", "archived"})
LISTING_NAME = "_listing.json"
LISTING_TAG = "wiki:listing=true"
MAX_PATH_LENGTH = 512
