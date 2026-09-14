"""Grant administration — HANDOFF §4.7, §4.10, §11.6, increment D.

Everything here changes who can see what. **Nothing in this module is reachable from
the MCP tool surface**, and the MCP function's role cannot perform these writes
(§8.8). It is called by the web application (increment F) and by ``scripts/``.

Owner guard (§4.10): a granter may grant or revoke on ``node`` only when they hold
``own`` on ``node`` or on one of its ancestors. That rule is enforced *here*, in
one audited code path, not in the console's UI.

Grants outlive their granter (§4.10): revoking someone's ``own`` does not cascade.
``grants_by_granter`` exists so the console can surface what an ex-owner granted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission


@dataclass(frozen=True)
class Profile:
    """The ``PROFILE`` row for one subject (§8.5)."""

    subject: str
    email: str
    display_name: str
    status: str = "active"  # active | invited | disabled
    created_at: str = ""


class NotAnOwner(Exception):
    """The granter holds no ``own`` on the node or any ancestor."""


class GrantAdmin:
    """Writes to the grant table. Constructed with a resource whose credentials
    can write; the MCP role's cannot.

    Args:
        table_name: The grant table.
        dynamodb_resource: Injected for tests.
        clock: ISO-8601 timestamp source.
    """

    def __init__(
        self, table_name: str, dynamodb_resource: Any | None = None, clock: Any = None
    ) -> None:
        raise NotImplementedError

    @property
    def store(self) -> GrantStore:
        """A read-side store over the same table (for the owner guard)."""
        raise NotImplementedError

    # --- owner guard ---------------------------------------------------------------

    def assert_owner(self, granter: str, node: str) -> None:
        """Raise ``NotAnOwner`` unless ``granter`` has ``own`` on ``node`` or an ancestor."""
        raise NotImplementedError

    def owned_roots(self, subject: str) -> list[str]:
        """The nodes on which ``subject`` holds ``own`` directly — the console scopes
        itself to these (§11.6)."""
        raise NotImplementedError

    # --- grants ------------------------------------------------------------------------

    def grant(self, granter: str, subject: str, node: str, permission: Permission) -> Grant:
        """Owner-guarded ``put_grant``. Idempotent on the same tuple; a different
        permission replaces the row."""
        raise NotImplementedError

    def revoke(self, granter: str, subject: str, node: str) -> None:
        """Owner-guarded delete. Missing row is not an error."""
        raise NotImplementedError

    def grants_of(self, subject: str) -> list[Grant]:
        raise NotImplementedError

    def grants_on(self, node: str) -> list[Grant]:
        raise NotImplementedError

    def grants_by_granter(self, granter: str) -> list[Grant]:
        """Scan-free if a GSI exists; otherwise a bounded query per subject — the
        table holds tens of rows. Document which."""
        raise NotImplementedError

    # --- profiles --------------------------------------------------------------------

    def create_profile(self, subject: str, email: str, display_name: str, status: str) -> Profile:
        raise NotImplementedError

    def get_profile(self, subject: str) -> Profile | None:
        raise NotImplementedError

    def list_profiles(self) -> list[Profile]:
        """All PROFILE rows (GSI or a bounded scan filtered on ``sk = PROFILE``)."""
        raise NotImplementedError

    def set_status(self, subject: str, status: str) -> None:
        raise NotImplementedError


__all__ = ["GrantAdmin", "NotAnOwner", "Profile"]
