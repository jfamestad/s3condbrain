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
_NODE_INDEX = "gs1"
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
    # An article grant reaches that one article only. The path grammar no longer
    # admits ".md" folder segments, but the resolver enforces the invariant itself so
    # a grant row written by any other route cannot cascade.
    used = sorted(
        (g for g in grants if g.node in nodes and (g.node == path or not g.is_article)),
        key=lambda g: len(g.node),
    )
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


def _is_disabled_profile(item: dict[str, Any]) -> bool:
    """The PROFILE row with ``status == "disabled"`` — the person resolves to nothing."""
    return item.get("sk") == _PROFILE_SK and item.get("status") == "disabled"


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
        return self._grants_on(subject, ancestors(path))

    def _grants_on(self, subject: str, nodes: Iterable[str]) -> list[Grant]:
        """The subject's grant rows on exactly ``nodes`` — ``[]`` when disabled.

        One BatchGetItem per 100 keys; duplicate nodes are fetched once.
        """
        pk = f"{_SUBJECT_PREFIX}{subject}"
        # The PROFILE row shares the partition key, so it rides in the same batch:
        # a disabled person resolves to nothing, on every request, for free (§12.9).
        keys = [{"pk": pk, "sk": _PROFILE_SK}]
        keys += [{"pk": pk, "sk": node} for node in dict.fromkeys(nodes)]
        items: list[dict[str, Any]] = []
        for start in range(0, len(keys), _BATCH_GET_LIMIT):
            items.extend(self._batch_get(keys[start : start + _BATCH_GET_LIMIT]))
        if any(_is_disabled_profile(i) for i in items):
            return []
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

    def resolve_many(self, subject: str, paths: Iterable[str]) -> dict[str, Resolution]:
        """``resolve`` for several paths at once, from one batched lookup.

        The union of every path's ancestors is fetched together (they share the
        subject's partition), chunked to the 100-key limit, with the PROFILE row in
        the batch exactly as in ``grants_for``: a disabled person resolves every path
        to nothing.

        Args:
            subject: Token ``sub``.
            paths: Absolute paths to resolve.

        Returns:
            ``{path: Resolution}``, equal path by path to ``resolve(subject, path)``.
            No paths, no request.

        Raises:
            ValueError: when a path is not a valid absolute path (see ``ancestors``).
        """
        wanted = list(dict.fromkeys(paths))
        if not wanted:
            return {}
        grants = self._grants_on(subject, (node for p in wanted for node in ancestors(p)))
        return {p: effective(grants, p) for p in wanted}

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
        searchable area (§8.7). Excludes the PROFILE item.

        The PROFILE row shares the partition, so the same Query returns it: a
        disabled person gets ``[]`` here exactly as from ``grants_for`` (§12.9),
        with no extra round trip. Every page is read before deciding — the row
        sorts after the path keys and may land on the last one.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"{_SUBJECT_PREFIX}{subject}")
        }
        out: list[Grant] = []
        disabled = False
        while True:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                if _is_disabled_profile(item):
                    disabled = True
                elif _is_grant_row(item):
                    out.append(_grant_from_item(item))
            last = response.get("LastEvaluatedKey")
            if not last:
                return [] if disabled else out
            kwargs["ExclusiveStartKey"] = last

    def grant_rows(self, subject: str) -> list[Grant]:
        """Every grant row stored for a subject, whatever their status — the
        inventory the admin console reviews. Grants outlive disablement (§4.10)
        and must stay visible there; nothing that *authorizes* may call this —
        use ``all_grants`` or ``grants_for``."""
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

        Answers "who can reach this node" for the move-impact report (§4.6, §8.5).
        A grant on an ancestor is not returned here; ``subjects_reaching`` walks the
        ancestors.
        """
        kwargs: dict[str, Any] = {
            "IndexName": _NODE_INDEX,
            "KeyConditionExpression": Key("gs1pk").eq(f"{_NODE_PREFIX}{node}"),
        }
        out: list[Grant] = []
        while True:
            response = self.table.query(**kwargs)
            out.extend(_grant_from_item(i) for i in response.get("Items", []) if _is_grant_row(i))
            last = response.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    def subjects_reaching(self, path: str) -> dict[str, Resolution]:
        """Effective permission per subject for ``path``: union over
        ``grants_for_node`` on every ancestor. Bounded by depth × grantees.

        Returns:
            ``{subject: Resolution}`` for every subject whose effective permission
            on ``path`` is not ``None``. A subject with grants only elsewhere in the
            tree does not appear, and neither does a disabled one (§12.9): their
            grants outlive disablement but confer nothing, so a move-impact report
            that named them would report a boundary nobody crosses.
        """
        by_subject: dict[str, list[Grant]] = {}
        for node in ancestors(path):
            for grant in self.grants_for_node(node):
                by_subject.setdefault(grant.subject, []).append(grant)
        disabled = self._disabled_among(list(by_subject))
        reaching: dict[str, Resolution] = {}
        for subject, grants in by_subject.items():
            if subject in disabled:
                continue
            resolution = effective(grants, path)
            if resolution.permission is not None:
                reaching[subject] = resolution
        return reaching

    def _disabled_among(self, subjects: list[str]) -> set[str]:
        """The subjects among ``subjects`` whose PROFILE row says ``disabled``.

        One BatchGetItem over the PROFILE keys, chunked to the API limit. A subject
        with no PROFILE row is not disabled; an empty input makes no request.
        """
        keys = [{"pk": f"{_SUBJECT_PREFIX}{subject}", "sk": _PROFILE_SK} for subject in subjects]
        disabled: set[str] = set()
        for start in range(0, len(keys), _BATCH_GET_LIMIT):
            for item in self._batch_get(keys[start : start + _BATCH_GET_LIMIT]):
                if _is_disabled_profile(item):
                    disabled.add(str(item["pk"]).removeprefix(_SUBJECT_PREFIX))
        return disabled

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
