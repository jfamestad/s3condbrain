"""Grant administration — HANDOFF §4.7, §4.10, §11.6, increment D.

Everything here changes who can see what. **Nothing in this module is reachable from
the MCP tool surface**, and the MCP function's role cannot perform these writes
(§8.8). It is called by the web application (increment F) and by ``scripts/``.

Owner guard (§4.10): a granter may grant or revoke on ``node`` only when they hold
``own`` on ``node`` or on one of its ancestors. That rule is enforced *here*, in
one audited code path, not in the console's UI.

Grants outlive their granter (§4.10): revoking someone's ``own`` does not cascade.
``grants_by_granter`` exists so the console can surface what an ex-owner granted.

Every write emits one structured line (service ``wiki-admin``, message
``admin_write``) naming the action, the granter, the subject, the node and the
permission — the grant-change half of the AS-10 audit record. The tool-call half
lives in ``app/mcp/server.py``.

Table rows this module touches (§8.5):

    pk = "U#<subject>"  sk = "<node>"   permission, granted_by, granted_at,
                                        gs1pk = "N#<node>",  gs1sk = "<subject>"
    pk = "U#<subject>"  sk = "PROFILE"  email, display_name, status, created_at,
                                        gs1pk = "PROFILE",   gs1sk = "<email>"

The second ``gs1pk`` shape lets ``list_profiles`` be a query on the same index the
grant rows use for "who can reach this node", so nothing here scans except
``grants_by_granter``, which says so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from app.auth.grants import GrantStore, ancestors
from app.auth.types import Grant, Permission

logger = Logger(service="wiki-admin")

_SUBJECT_PREFIX = "U#"
_NODE_PREFIX = "N#"
_PROFILE_SK = "PROFILE"
_PROFILE_GSI_PK = "PROFILE"
_GSI_NAME = "gs1"
_CONDITION_FAILED = "ConditionalCheckFailedException"

VALID_STATUSES = frozenset({"active", "invited", "disabled"})


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


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _check_status(status: str) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}: {status!r}")


def _normalise_node(node: str) -> str:
    """Validate an absolute path and drop a trailing slash. Raises ``ValueError``."""
    return ancestors(node)[-1]


def _grant_from_item(item: dict[str, Any]) -> Grant:
    return Grant(
        subject=item["pk"][len(_SUBJECT_PREFIX) :],
        node=item["sk"],
        permission=Permission(item["permission"]),
        granted_by=item.get("granted_by", ""),
        granted_at=item.get("granted_at", ""),
    )


def _is_grant_row(item: dict[str, Any]) -> bool:
    sk = item.get("sk", "")
    return sk != _PROFILE_SK and sk.startswith("/")


def _profile_from_item(item: dict[str, Any]) -> Profile:
    return Profile(
        subject=item["pk"][len(_SUBJECT_PREFIX) :],
        email=item.get("email", ""),
        display_name=item.get("display_name", ""),
        status=item.get("status", "active"),
        created_at=item.get("created_at", ""),
    )


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


class GrantAdmin:
    """Writes to the grant table. Constructed with a resource whose credentials
    can write; the MCP role's cannot.

    Args:
        table_name: The grant table.
        dynamodb_resource: Injected for tests.
        clock: ISO-8601 timestamp source. Defaults to ``datetime.now(UTC)``.
    """

    def __init__(
        self,
        table_name: str,
        dynamodb_resource: Any | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._table_name = table_name
        self._ddb = dynamodb_resource
        self._clock: Callable[[], str] = clock or _iso_now
        self._store: GrantStore | None = None

    @property
    def _resource(self) -> Any:
        if self._ddb is None:
            self._ddb = boto3.resource("dynamodb")
        return self._ddb

    @property
    def store(self) -> GrantStore:
        """A read-side store over the same table (for the owner guard)."""
        if self._store is None:
            self._store = GrantStore(self._table_name, self._resource)
        return self._store

    @property
    def table(self) -> Any:
        return self.store.table

    # --- owner guard ---------------------------------------------------------------

    def assert_owner(self, granter: str, node: str) -> None:
        """Raise ``NotAnOwner`` unless ``granter`` has ``own`` on ``node`` or an ancestor.

        ``ancestors(node)`` includes ``node`` itself, so ``own`` attached directly to
        the node counts; so does ``own`` on ``/`` (the admin, §4.5).
        """
        if not self.store.resolve(granter, node).allows(Permission.OWN):
            raise NotAnOwner(f"{granter} holds no own on {node} or any ancestor")

    def owned_roots(self, subject: str) -> list[str]:
        """The minimal set of nodes on which ``subject`` holds ``own`` — the console
        scopes itself to these (§11.6).

        Minimal: a node whose ancestor is also owned by ``subject`` is dropped, since
        the ancestor's ``own`` already covers it. Sorted, so output is stable.
        """
        owned = {g.node for g in self.store.all_grants(subject) if g.permission is Permission.OWN}
        return sorted(n for n in owned if not any(a in owned for a in ancestors(n)[:-1]))

    # --- grants ------------------------------------------------------------------------

    def grant(self, granter: str, subject: str, node: str, permission: Permission) -> Grant:
        """Owner-guarded ``put_grant``. Idempotent on the same tuple; a different
        permission replaces the row.

        Raises:
            ValueError: ``node`` is not an absolute, well-formed path.
            NotAnOwner: ``granter`` holds no ``own`` on ``node`` or an ancestor.
        """
        node = _normalise_node(node)
        permission = Permission(permission)
        self.assert_owner(granter, node)
        grant = Grant(
            subject=subject,
            node=node,
            permission=permission,
            granted_by=granter,
            granted_at=self._clock(),
        )
        self.store.put_grant(grant)
        logger.info(
            "admin_write",
            action="grant",
            granter=granter,
            subject=subject,
            node=node,
            permission=permission.value,
        )
        return grant

    def revoke(self, granter: str, subject: str, node: str) -> None:
        """Owner-guarded delete. Missing row is not an error.

        An owner may revoke their own ``own`` — that is how ownership is resigned.
        Nothing cascades: what they granted stays (§4.10).

        Raises:
            ValueError: ``node`` is not an absolute, well-formed path.
            NotAnOwner: ``granter`` holds no ``own`` on ``node`` or an ancestor.
        """
        node = _normalise_node(node)
        self.assert_owner(granter, node)
        response = self.table.delete_item(
            Key={"pk": f"{_SUBJECT_PREFIX}{subject}", "sk": node}, ReturnValues="ALL_OLD"
        )
        old = response.get("Attributes") or {}
        logger.info(
            "admin_write",
            action="revoke",
            granter=granter,
            subject=subject,
            node=node,
            permission=old.get("permission"),
        )

    def grants_of(self, subject: str) -> list[Grant]:
        """Every grant row for ``subject`` (query on ``pk``), a disabled person's
        included — grants outlive disablement and are shown for review (§4.10)."""
        return self.store.grant_rows(subject)

    def grants_on(self, node: str) -> list[Grant]:
        """Every grant attached to exactly ``node`` — a GSI1 query on ``N#<node>``.

        Ancestor grants are not included; that union is ``GrantStore.resolve``'s job.
        """
        node = _normalise_node(node)
        kwargs: dict[str, Any] = {
            "IndexName": _GSI_NAME,
            "KeyConditionExpression": Key("gs1pk").eq(f"{_NODE_PREFIX}{node}"),
        }
        return [_grant_from_item(i) for i in self._paginate("query", kwargs) if _is_grant_row(i)]

    def grants_by_granter(self, granter: str) -> list[Grant]:
        """Every grant ``granter`` made — the "unowned grants" review list (§4.10).

        **This is a Scan.** The table has no index on ``granted_by``; adding one for
        a table that holds tens of rows (§1.3) buys nothing. The filter is
        ``granted_by = :g AND begins_with(sk, "/")`` so profile rows never match.
        Paginated to completion; the bound is the table size.
        """
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("granted_by").eq(granter) & Attr("sk").begins_with("/")
        }
        return [_grant_from_item(i) for i in self._paginate("scan", kwargs)]

    # --- profiles --------------------------------------------------------------------

    def create_profile(self, subject: str, email: str, display_name: str, status: str) -> Profile:
        """Write the ``PROFILE`` row for a new subject.

        Raises:
            ValueError: the profile already exists, or ``status`` is not one of
                ``active``, ``invited``, ``disabled``.
        """
        _check_status(status)
        profile = Profile(
            subject=subject,
            email=email,
            display_name=display_name,
            status=status,
            created_at=self._clock(),
        )
        try:
            self.table.put_item(
                Item={
                    "pk": f"{_SUBJECT_PREFIX}{subject}",
                    "sk": _PROFILE_SK,
                    "email": email,
                    "display_name": display_name,
                    "status": status,
                    "created_at": profile.created_at,
                    "gs1pk": _PROFILE_GSI_PK,
                    "gs1sk": email,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as exc:
            if _error_code(exc) == _CONDITION_FAILED:
                raise ValueError(f"profile already exists for {subject}") from exc
            raise
        logger.info("admin_write", action="create_profile", subject=subject, status=status)
        return profile

    def get_profile(self, subject: str) -> Profile | None:
        response = self.table.get_item(
            Key={"pk": f"{_SUBJECT_PREFIX}{subject}", "sk": _PROFILE_SK}, ConsistentRead=True
        )
        item = response.get("Item")
        return _profile_from_item(item) if item else None

    def list_profiles(self) -> list[Profile]:
        """All PROFILE rows — a GSI1 query on ``gs1pk = "PROFILE"``, ordered by email."""
        kwargs: dict[str, Any] = {
            "IndexName": _GSI_NAME,
            "KeyConditionExpression": Key("gs1pk").eq(_PROFILE_GSI_PK),
        }
        return [_profile_from_item(i) for i in self._paginate("query", kwargs)]

    def set_status(self, subject: str, status: str) -> None:
        """Change a profile's ``status``.

        Raises:
            ValueError: ``status`` is not valid, or no profile exists for ``subject``.
        """
        _check_status(status)
        try:
            self.table.update_item(
                Key={"pk": f"{_SUBJECT_PREFIX}{subject}", "sk": _PROFILE_SK},
                UpdateExpression="SET #s = :s",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": status},
            )
        except ClientError as exc:
            if _error_code(exc) == _CONDITION_FAILED:
                raise ValueError(f"no profile for {subject}") from exc
            raise
        logger.info("admin_write", action="set_status", subject=subject, status=status)

    # --- helpers ---------------------------------------------------------------------

    def _paginate(self, op: str, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        """Run ``table.query`` or ``table.scan`` to the last page."""
        method = getattr(self.table, op)
        items: list[dict[str, Any]] = []
        while True:
            response = method(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                return items
            kwargs = {**kwargs, "ExclusiveStartKey": last}


__all__ = ["VALID_STATUSES", "GrantAdmin", "NotAnOwner", "Profile"]
