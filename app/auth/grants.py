"""Grant resolution — HANDOFF §4.6, §8.5, build step 2.

Effective permission is the union of grants on the node and on every ancestor.
The ancestor set falls out of the path string, so resolution is one
``BatchGetItem`` bounded by depth, never a scan.

DynamoDB layout (single table, abstract key names):

    pk = "U#<subject>"   sk = "<node path>"   permission, granted_by, granted_at
    pk = "U#<subject>"   sk = "PROFILE"       email, display_name, status
    gs1pk = "N#<node>"   gs1sk = "<subject>"  (GSI1: who can reach this node)

The MCP function's role can only read this table (§4.7, §8.8). ``put_grant`` exists
for the bootstrap script and tests; it will fail with AccessDenied from the MCP role,
which is the intended outcome.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.auth.types import Grant, Permission, Resolution
from app.errors import ToolError


def ancestors(path: str) -> list[str]:
    """Return ``/`` and every ancestor folder of ``path``, then ``path`` itself.

    Args:
        path: Absolute article or folder path, e.g. ``/racing/setup/rear-bar.md``.

    Returns:
        ``["/", "/racing", "/racing/setup", "/racing/setup/rear-bar.md"]``.
        For ``/`` itself, ``["/"]``.
    """
    raise NotImplementedError


def effective(grants: Iterable[Grant], path: str) -> Resolution:
    """Pure resolution: the strongest grant whose node is ``path`` or an ancestor of it.

    Args:
        grants: Candidate grants for one subject (any node).
        path: Absolute path being accessed.

    Returns:
        The effective permission and the grants that contributed. With no matching
        grant, ``Resolution(None, ())``.
    """
    raise NotImplementedError


class GrantStore:
    """Reads grants from DynamoDB. Construct with an injected resource for tests."""

    def __init__(self, table_name: str, dynamodb_resource: Any | None = None) -> None:
        raise NotImplementedError

    def grants_for(self, subject: str, path: str) -> list[Grant]:
        """BatchGetItem over ``ancestors(path)`` for one subject."""
        raise NotImplementedError

    def resolve(self, subject: str, path: str) -> Resolution:
        """``effective(self.grants_for(subject, path), path)``."""
        raise NotImplementedError

    def require(self, subject: str, path: str, needed: Permission) -> Resolution:
        """Resolve and raise ``ToolError(403, "forbidden")`` unless ``needed`` is satisfied.

        Raises:
            ToolError: 403 when the subject lacks the permission. Message names no scope.
        """
        raise NotImplementedError

    def all_grants(self, subject: str) -> list[Grant]:
        """Every grant row for a subject (Query on pk). Used by search to build the
        searchable area (§8.7). Excludes the PROFILE item."""
        raise NotImplementedError

    def put_grant(self, grant: Grant) -> None:
        """Write one grant row plus its GSI keys. Bootstrap and tests only."""
        raise NotImplementedError


__all__ = ["GrantStore", "Permission", "Resolution", "ToolError", "ancestors", "effective"]
