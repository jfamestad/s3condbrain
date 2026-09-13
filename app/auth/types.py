"""Grant model shared by resolution, minting and the tools (HANDOFF §4.5, §4.6, §8.5)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Permission(str, Enum):
    """Three verbs. ``own`` implies ``write`` implies ``read`` (§4.5)."""

    READ = "read"
    WRITE = "write"
    OWN = "own"

    @property
    def rank(self) -> int:
        return {"read": 1, "write": 2, "own": 3}[self.value]

    def satisfies(self, needed: Permission) -> bool:
        """True when this permission confers ``needed``."""
        return self.rank >= needed.rank


@dataclass(frozen=True)
class Grant:
    """One row of the grant store: *(principal, node, permission)* (§3, §8.5).

    Attributes:
        subject: Token ``sub`` of the principal.
        node: Absolute path of a folder (no trailing slash; ``/`` is root) or an article.
        permission: The verb granted.
        granted_by: Subject of the owner who made the grant.
        granted_at: ISO-8601 timestamp.
    """

    subject: str
    node: str
    permission: Permission
    granted_by: str = ""
    granted_at: str = ""

    @property
    def is_article(self) -> bool:
        return self.node.endswith(".md")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a subject against a path.

    Attributes:
        permission: Effective permission, or ``None`` when no grant matches.
        grants_used: Every grant the decision depended on (AS-10 audit field).
    """

    permission: Permission | None
    grants_used: tuple[Grant, ...] = ()

    def allows(self, needed: Permission) -> bool:
        return self.permission is not None and self.permission.satisfies(needed)
