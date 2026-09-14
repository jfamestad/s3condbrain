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

import time
from collections.abc import Iterable
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from app.auth.types import Grant, Permission, Resolution
from app.errors import ToolError, forbidden

_BATCH_GET_LIMIT = 100
_UNPROCESSED_RETRIES = 3
_SUBJECT_PREFIX = "U#"
_NODE_PREFIX = "N#"
_PROFILE_SK = "PROFILE"


def ancestors(path: str) -> list[str]:
    """Return ``/`` and every ancestor folder of ``path``, then ``path`` itself.

    Args:
        path: Absolute article or folder path, e.g. ``/racing/setup/rear-bar.md``.
            A trailing slash on a folder is tolerated and dropped.

    Returns:
        ``["/", "/racing", "/racing/setup", "/racing/setup/rear-bar.md"]``.
        For ``/`` itself, ``["/"]``.

    Raises:
        ValueError: when ``path`` is not absolute, or carries an empty, ``.`` or
            ``..`` segment.
    """
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute: {path!r}")
    trimmed = path.rstrip("/")
    if not trimmed:
        return ["/"]
    segments = trimmed[1:].split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise ValueError(f"path carries an invalid segment: {path!r}")
    out = ["/"]
    current = ""
    for seg in segments:
        current = f"{current}/{seg}"
        out.append(current)
    return out


def effective(grants: Iterable[Grant], path: str) -> Resolution:
    """Pure resolution: the strongest grant whose node is ``path`` or an ancestor of it.

    A grant matches only when its node string is exactly one of ``ancestors(path)``:
    a folder grant reaches the folder and everything beneath, an article grant reaches
    that one article. There are no deny rules (§4.6).

    Args:
        grants: Candidate grants for one subject (any node).
        path: Absolute path being accessed.

    Returns:
        The effective permission and the grants that contributed. With no matching
        grant, ``Resolution(None, ())``.
    """
    nodes = set(ancestors(path))
    used = sorted((g for g in grants if g.node in nodes), key=lambda g: len(g.node))
    if not used:
        return Resolution(None, ())
    strongest = max(used, key=lambda g: g.permission.rank).permission
    return Resolution(strongest, tuple(used))


def _grant_from_item(item: dict[str, Any]) -> Grant:
    return Grant(
        subject=item["pk"][len(_SUBJECT_PREFIX) :],
        node=item["sk"],
        permission=Permission(item["permission"]),
        granted_by=item.get("granted_by", ""),
        granted_at=item.get("granted_at", ""),
    )


def _is_grant_row(item: dict[str, Any]) -> bool:
    """Grant rows key on a path; ``PROFILE`` (and any future non-path row) is not one."""
    sk = item.get("sk", "")
    return sk != _PROFILE_SK and sk.startswith("/")


class GrantStore:
    """Reads grants from DynamoDB. Construct with an injected resource for tests."""

    def __init__(self, table_name: str, dynamodb_resource: Any | None = None) -> None:
        self._table_name = table_name
        self._ddb = dynamodb_resource
        self._table: Any | None = None

    @property
    def _resource(self) -> Any:
        if self._ddb is None:
            self._ddb = boto3.resource("dynamodb")
        return self._ddb

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = self._resource.Table(self._table_name)
        return self._table

    def grants_for(self, subject: str, path: str) -> list[Grant]:
        """BatchGetItem over ``ancestors(path)`` for one subject.

        Depth bounds the key count, but keys are chunked to the API's 100-key limit
        anyway. Unprocessed keys are retried with exponential backoff.
        """
        pk = f"{_SUBJECT_PREFIX}{subject}"
        keys = [{"pk": pk, "sk": node} for node in ancestors(path)]
        items: list[dict[str, Any]] = []
        for start in range(0, len(keys), _BATCH_GET_LIMIT):
            items.extend(self._batch_get(keys[start : start + _BATCH_GET_LIMIT]))
        return [_grant_from_item(i) for i in items if _is_grant_row(i)]

    def _batch_get(self, keys: list[dict[str, str]]) -> list[dict[str, Any]]:
        request: dict[str, Any] = {self._table_name: {"Keys": keys}}
        items: list[dict[str, Any]] = []
        for attempt in range(_UNPROCESSED_RETRIES + 1):
            response = self._resource.batch_get_item(RequestItems=request)
            items.extend(response.get("Responses", {}).get(self._table_name, []))
            request = response.get("UnprocessedKeys") or {}
            if not request.get(self._table_name, {}).get("Keys"):
                return items
            if attempt < _UNPROCESSED_RETRIES:
                time.sleep(0.05 * (2**attempt))
        raise RuntimeError("grant lookup left keys unprocessed after retries")

    def resolve(self, subject: str, path: str) -> Resolution:
        """``effective(self.grants_for(subject, path), path)``."""
        return effective(self.grants_for(subject, path), path)

    def require(self, subject: str, path: str, needed: Permission) -> Resolution:
        """Resolve and raise ``ToolError(403, "forbidden")`` unless ``needed`` is satisfied.

        Raises:
            ToolError: 403 when the subject lacks the permission. Message names no scope.
        """
        resolution = self.resolve(subject, path)
        if not resolution.allows(needed):
            raise forbidden()
        return resolution

    def all_grants(self, subject: str) -> list[Grant]:
        """Every grant row for a subject (Query on pk). Used by search to build the
        searchable area (§8.7). Excludes the PROFILE item."""
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"{_SUBJECT_PREFIX}{subject}")
        }
        out: list[Grant] = []
        while True:
            response = self.table.query(**kwargs)
            out.extend(_grant_from_item(i) for i in response.get("Items", []) if _is_grant_row(i))
            last = response.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    def grants_for_node(self, node: str) -> list[Grant]:
        """Every grant attached to exactly ``node`` (GSI1 query on ``N#<node>``).
        Increment B implements this for the move-impact report (§4.6, §8.5)."""
        raise NotImplementedError

    def subjects_reaching(self, path: str) -> dict[str, Resolution]:
        """Effective permission per subject for ``path``: union over
        ``grants_for_node`` on every ancestor. Bounded by depth × grantees."""
        raise NotImplementedError

    def put_grant(self, grant: Grant) -> None:
        """Write one grant row plus its GSI keys. Bootstrap and tests only."""
        self.table.put_item(
            Item={
                "pk": f"{_SUBJECT_PREFIX}{grant.subject}",
                "sk": grant.node,
                "permission": grant.permission.value,
                "granted_by": grant.granted_by,
                "granted_at": grant.granted_at,
                "gs1pk": f"{_NODE_PREFIX}{grant.node}",
                "gs1sk": grant.subject,
            }
        )


__all__ = ["GrantStore", "Permission", "Resolution", "ToolError", "ancestors", "effective"]
