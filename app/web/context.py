"""Per-request context for the web views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.auth.admin import GrantAdmin
from app.auth.credentials import CredentialMinter
from app.auth.grants import GrantStore
from app.config import Settings
from app.storage.articles import ArticleStore
from app.storage.listings import ListingIndex
from app.web.secrets import WebSecrets
from app.web.session import Principal


@dataclass
class WebContext:
    """What a view handler gets. Same data path as the MCP tools — grants resolved
    per request, credentials minted per operation (HANDOFF §4.8) — plus the admin
    writer, which only this function's role can use (§8.8).

    ``principal`` is ``None`` on public routes (login, callback, error pages).
    """

    settings: Settings
    secrets: WebSecrets
    grants: GrantStore
    admin: GrantAdmin
    minter: CredentialMinter
    store: ArticleStore
    listings: ListingIndex
    principal: Principal | None = None
    request_id: str = ""
    log: Any = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        if self.principal is None:
            raise RuntimeError("no principal on a public route")
        return self.principal.subject

    @property
    def app_prefix(self) -> str:
        return "/app"


__all__ = ["WebContext"]
