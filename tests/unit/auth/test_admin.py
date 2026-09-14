"""Increment D — grant administration (HANDOFF §4.5, §4.7, §4.10, §8.5).

The owner guard is the thing under test: who may grant where. Everything runs
against a moto grant table via the shared ``grant_table`` fixture; the admin gets
its own resource over the same mocked endpoint.
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from aws_lambda_powertools import Logger

from app.auth import admin as admin_mod
from app.auth.admin import GrantAdmin, NotAnOwner, Profile
from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission
from scripts import grant_owner

ROOT = "user_root"
ALICE = "user_alice"
BOB = "user_bob"
CARA = "user_cara"
NOW = "2026-09-13T12:00:00+00:00"


@pytest.fixture
def ddb(grant_table: Any) -> Any:
    return boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])


@pytest.fixture
def admin(ddb: Any, settings: Any) -> GrantAdmin:
    return GrantAdmin(settings.grant_table, dynamodb_resource=ddb, clock=lambda: NOW)


@pytest.fixture
def seeded(admin: GrantAdmin) -> GrantAdmin:
    """ROOT owns ``/`` (bootstrap row), ALICE owns ``/racing``."""
    admin.store.put_grant(
        Grant(ROOT, "/", Permission.OWN, granted_by="process:bootstrap", granted_at=NOW)
    )
    admin.grant(ROOT, ALICE, "/racing", Permission.OWN)
    return admin


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    buf = io.StringIO()
    logger = Logger(
        service=f"wiki-admin-test-{uuid.uuid4().hex}", logger_handler=logging.StreamHandler(buf)
    )
    monkeypatch.setattr(admin_mod, "logger", logger)
    monkeypatch.setattr(grant_owner, "logger", logger)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

    yield read


# --- construction ------------------------------------------------------------------


def test_store_is_a_grant_store_over_the_same_table(admin: GrantAdmin, settings: Any) -> None:
    assert isinstance(admin.store, GrantStore)
    assert admin.store is admin.store
    assert admin.store.table.name == settings.grant_table
    assert admin.table is admin.store.table


def test_default_clock_is_iso_utc(ddb: Any, settings: Any) -> None:
    a = GrantAdmin(settings.grant_table, dynamodb_resource=ddb)
    a.store.put_grant(Grant(ROOT, "/", Permission.OWN))
    g = a.grant(ROOT, ALICE, "/x", Permission.READ)
    assert g.granted_at.endswith("+00:00")


# --- owner guard --------------------------------------------------------------------


class TestOwnerGuard:
    def test_non_owner_raises(self, seeded: GrantAdmin) -> None:
        with pytest.raises(NotAnOwner):
            seeded.assert_owner(BOB, "/racing")
        with pytest.raises(NotAnOwner):
            seeded.grant(BOB, CARA, "/racing", Permission.READ)
        with pytest.raises(NotAnOwner):
            seeded.revoke(BOB, ALICE, "/racing")

    def test_owner_of_ancestor_may_grant_beneath(self, seeded: GrantAdmin) -> None:
        seeded.assert_owner(ROOT, "/racing/setup/rear-bar.md")
        g = seeded.grant(ALICE, BOB, "/racing/setup/rear-bar.md", Permission.READ)
        assert g.granted_by == ALICE
        assert seeded.store.resolve(BOB, "/racing/setup/rear-bar.md").permission is Permission.READ

    def test_owner_of_sibling_may_not(self, seeded: GrantAdmin) -> None:
        with pytest.raises(NotAnOwner):
            seeded.assert_owner(ALICE, "/cooking")
        with pytest.raises(NotAnOwner):
            seeded.grant(ALICE, BOB, "/cooking/x.md", Permission.READ)

    def test_own_on_the_node_itself_counts(self, seeded: GrantAdmin) -> None:
        # ALICE's only own is on /racing; ancestors("/racing") includes "/racing".
        seeded.assert_owner(ALICE, "/racing")
        g = seeded.grant(ALICE, BOB, "/racing", Permission.WRITE)
        assert g.node == "/racing"

    def test_owner_of_root_may_grant_at_root(self, seeded: GrantAdmin) -> None:
        seeded.assert_owner(ROOT, "/")
        seeded.grant(ROOT, BOB, "/", Permission.READ)
        assert seeded.store.resolve(BOB, "/anything/at/all.md").permission is Permission.READ

    def test_write_on_node_is_not_ownership(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing", Permission.WRITE)
        with pytest.raises(NotAnOwner):
            seeded.assert_owner(BOB, "/racing/x.md")

    def test_granter_may_grant_own_beneath(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing/setup", Permission.OWN)
        seeded.assert_owner(BOB, "/racing/setup/rear-bar.md")
        with pytest.raises(NotAnOwner):
            seeded.assert_owner(BOB, "/racing")


# --- grant / revoke --------------------------------------------------------------------


class TestGrantRevoke:
    def test_round_trip(self, seeded: GrantAdmin) -> None:
        g = seeded.grant(ALICE, BOB, "/racing/setup", Permission.READ)
        assert g == Grant(BOB, "/racing/setup", Permission.READ, granted_by=ALICE, granted_at=NOW)
        assert seeded.grants_of(BOB) == [g]
        assert seeded.store.resolve(BOB, "/racing/setup/x.md").permission is Permission.READ

        seeded.revoke(ALICE, BOB, "/racing/setup")
        assert seeded.grants_of(BOB) == []
        assert seeded.store.resolve(BOB, "/racing/setup/x.md").permission is None

    def test_grant_with_different_permission_replaces_the_row(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing", Permission.READ)
        seeded.grant(ALICE, BOB, "/racing", Permission.WRITE)
        grants = seeded.grants_of(BOB)
        assert len(grants) == 1
        assert grants[0].permission is Permission.WRITE

    def test_grant_accepts_permission_as_string(self, seeded: GrantAdmin) -> None:
        g = seeded.grant(ALICE, BOB, "/racing", "read")  # type: ignore[arg-type]
        assert g.permission is Permission.READ

    def test_revoke_missing_row_is_not_an_error(self, seeded: GrantAdmin) -> None:
        seeded.revoke(ALICE, BOB, "/racing/nothing-here.md")

    @pytest.mark.parametrize("bad", ["racing", "", "/racing/../x.md", "/racing//x.md"])
    def test_bad_node_is_value_error_before_any_guard(self, seeded: GrantAdmin, bad: str) -> None:
        with pytest.raises(ValueError):
            seeded.grant(ROOT, BOB, bad, Permission.READ)
        with pytest.raises(ValueError):
            seeded.revoke(ROOT, BOB, bad)

    def test_trailing_slash_is_normalised(self, seeded: GrantAdmin) -> None:
        g = seeded.grant(ALICE, BOB, "/racing/setup/", Permission.READ)
        assert g.node == "/racing/setup"
        seeded.revoke(ALICE, BOB, "/racing/setup/")
        assert seeded.grants_of(BOB) == []

    def test_gsi_keys_are_written(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing", Permission.READ)
        item = seeded.table.get_item(Key={"pk": f"U#{BOB}", "sk": "/racing"})["Item"]
        assert item["gs1pk"] == "N#/racing"
        assert item["gs1sk"] == BOB
        assert item["granted_by"] == ALICE
        assert item["granted_at"] == NOW


class TestGrantsOutliveGranter:
    def test_revoking_the_granter_leaves_their_grants(self, seeded: GrantAdmin) -> None:
        cara = seeded.grant(ALICE, CARA, "/racing/setup", Permission.READ)
        seeded.revoke(ROOT, ALICE, "/racing")

        assert seeded.grants_of(ALICE) == []
        assert seeded.grants_of(CARA) == [cara]
        assert seeded.grants_by_granter(ALICE) == [cara]
        # and ALICE can no longer touch what she granted
        with pytest.raises(NotAnOwner):
            seeded.revoke(ALICE, CARA, "/racing/setup")
        # but the root owner can
        seeded.revoke(ROOT, CARA, "/racing/setup")
        assert seeded.grants_of(CARA) == []

    def test_owner_may_resign_by_revoking_their_own_grant(self, seeded: GrantAdmin) -> None:
        seeded.revoke(ALICE, ALICE, "/racing")
        assert seeded.grants_of(ALICE) == []
        with pytest.raises(NotAnOwner):
            seeded.grant(ALICE, BOB, "/racing", Permission.READ)


# --- owned_roots -----------------------------------------------------------------------


class TestOwnedRoots:
    def test_empty(self, seeded: GrantAdmin) -> None:
        assert seeded.owned_roots(BOB) == []

    def test_single(self, seeded: GrantAdmin) -> None:
        assert seeded.owned_roots(ALICE) == ["/racing"]

    def test_root_swallows_everything(self, seeded: GrantAdmin) -> None:
        seeded.grant(ROOT, ROOT, "/racing", Permission.OWN)
        seeded.grant(ROOT, ROOT, "/cooking/x.md", Permission.OWN)
        assert seeded.owned_roots(ROOT) == ["/"]

    def test_disjoint_subtrees_are_both_roots(self, seeded: GrantAdmin) -> None:
        seeded.grant(ROOT, ALICE, "/cooking", Permission.OWN)
        seeded.grant(ROOT, ALICE, "/racing/setup", Permission.OWN)  # beneath /racing → dropped
        assert seeded.owned_roots(ALICE) == ["/cooking", "/racing"]

    def test_non_own_grants_do_not_count(self, seeded: GrantAdmin) -> None:
        seeded.grant(ROOT, BOB, "/cooking", Permission.WRITE)
        seeded.grant(ROOT, BOB, "/racing", Permission.READ)
        assert seeded.owned_roots(BOB) == []

    def test_sibling_prefix_is_not_an_ancestor(self, seeded: GrantAdmin) -> None:
        seeded.grant(ROOT, ALICE, "/racing-archive", Permission.OWN)
        assert seeded.owned_roots(ALICE) == ["/racing", "/racing-archive"]


# --- listings ---------------------------------------------------------------------------


class TestListings:
    def test_grants_on_via_gsi(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing", Permission.READ)
        seeded.grant(ALICE, CARA, "/racing", Permission.WRITE)
        seeded.grant(ALICE, CARA, "/racing/setup", Permission.OWN)
        seeded.create_profile(BOB, "bob@example.com", "Bob", "active")

        on_racing = seeded.grants_on("/racing")
        assert {(g.subject, g.permission) for g in on_racing} == {
            (ALICE, Permission.OWN),
            (BOB, Permission.READ),
            (CARA, Permission.WRITE),
        }
        assert all(g.node == "/racing" for g in on_racing)
        assert [g.subject for g in seeded.grants_on("/racing/setup")] == [CARA]
        assert seeded.grants_on("/nowhere") == []
        assert [g.subject for g in seeded.grants_on("/racing/")] == [g.subject for g in on_racing]

    def test_grants_by_granter(self, seeded: GrantAdmin) -> None:
        seeded.grant(ALICE, BOB, "/racing", Permission.READ)
        seeded.grant(ALICE, CARA, "/racing/setup", Permission.READ)
        seeded.grant(ROOT, CARA, "/cooking", Permission.READ)
        seeded.create_profile(ALICE, "alice@example.com", "Alice", "active")

        by_alice = seeded.grants_by_granter(ALICE)
        assert {(g.subject, g.node) for g in by_alice} == {
            (BOB, "/racing"),
            (CARA, "/racing/setup"),
        }
        assert all(g.granted_by == ALICE for g in by_alice)
        assert {(g.subject, g.node) for g in seeded.grants_by_granter(ROOT)} == {
            (ALICE, "/racing"),
            (CARA, "/cooking"),
        }
        assert seeded.grants_by_granter("nobody") == []

    def test_grants_of_excludes_profile(self, seeded: GrantAdmin) -> None:
        seeded.create_profile(ALICE, "alice@example.com", "Alice", "active")
        assert [g.node for g in seeded.grants_of(ALICE)] == ["/racing"]


# --- profiles ----------------------------------------------------------------------------


class TestProfiles:
    def test_create_get(self, admin: GrantAdmin) -> None:
        p = admin.create_profile(ALICE, "alice@example.com", "Alice", "invited")
        assert p == Profile(ALICE, "alice@example.com", "Alice", "invited", NOW)
        assert admin.get_profile(ALICE) == p
        item = admin.table.get_item(Key={"pk": f"U#{ALICE}", "sk": "PROFILE"})["Item"]
        assert item["gs1pk"] == "PROFILE"
        assert item["gs1sk"] == "alice@example.com"

    def test_get_missing_is_none(self, admin: GrantAdmin) -> None:
        assert admin.get_profile("nobody") is None

    def test_create_twice_is_value_error(self, admin: GrantAdmin) -> None:
        admin.create_profile(ALICE, "alice@example.com", "Alice", "active")
        with pytest.raises(ValueError):
            admin.create_profile(ALICE, "other@example.com", "Alice 2", "active")
        assert admin.get_profile(ALICE).email == "alice@example.com"  # type: ignore[union-attr]

    def test_create_with_bad_status_is_value_error(self, admin: GrantAdmin) -> None:
        with pytest.raises(ValueError):
            admin.create_profile(ALICE, "alice@example.com", "Alice", "banned")
        assert admin.get_profile(ALICE) is None

    def test_list_profiles_via_gsi_excludes_grants(self, seeded: GrantAdmin) -> None:
        seeded.create_profile(BOB, "bob@example.com", "Bob", "active")
        seeded.create_profile(ALICE, "alice@example.com", "Alice", "invited")
        profiles = seeded.list_profiles()
        assert [p.subject for p in profiles] == [ALICE, BOB]  # ordered by email (GSI sort key)
        assert all(isinstance(p, Profile) for p in profiles)

    def test_list_profiles_empty(self, seeded: GrantAdmin) -> None:
        assert seeded.list_profiles() == []

    @pytest.mark.parametrize("status", ["active", "invited", "disabled"])
    def test_set_status(self, admin: GrantAdmin, status: str) -> None:
        admin.create_profile(ALICE, "alice@example.com", "Alice", "invited")
        admin.set_status(ALICE, status)
        assert admin.get_profile(ALICE).status == status  # type: ignore[union-attr]

    def test_set_status_rejects_unknown(self, admin: GrantAdmin) -> None:
        admin.create_profile(ALICE, "alice@example.com", "Alice", "active")
        with pytest.raises(ValueError):
            admin.set_status(ALICE, "deleted")
        assert admin.get_profile(ALICE).status == "active"  # type: ignore[union-attr]

    def test_set_status_on_missing_profile_is_value_error(self, admin: GrantAdmin) -> None:
        with pytest.raises(ValueError):
            admin.set_status("nobody", "disabled")
        assert admin.list_profiles() == []


# --- audit lines (AS-10) -------------------------------------------------------------------


class TestAudit:
    def test_grant_and_revoke_log_structured_lines(self, seeded: GrantAdmin, log_lines) -> None:
        seeded.grant(ALICE, BOB, "/racing/setup", Permission.READ)
        seeded.revoke(ALICE, BOB, "/racing/setup")
        lines = [ln for ln in log_lines() if ln["message"] == "admin_write"]
        assert len(lines) == 2
        assert (
            lines[0].items()
            >= {
                "action": "grant",
                "granter": ALICE,
                "subject": BOB,
                "node": "/racing/setup",
                "permission": "read",
            }.items()
        )
        assert (
            lines[1].items()
            >= {
                "action": "revoke",
                "granter": ALICE,
                "subject": BOB,
                "node": "/racing/setup",
                "permission": "read",
            }.items()
        )

    def test_refused_grant_logs_nothing(self, seeded: GrantAdmin, log_lines) -> None:
        with pytest.raises(NotAnOwner):
            seeded.grant(BOB, CARA, "/racing", Permission.READ)
        assert [ln for ln in log_lines() if ln["message"] == "admin_write"] == []

    def test_profile_writes_log(self, admin: GrantAdmin, log_lines) -> None:
        admin.create_profile(ALICE, "alice@example.com", "Alice", "invited")
        admin.set_status(ALICE, "active")
        actions = [ln["action"] for ln in log_lines() if ln["message"] == "admin_write"]
        assert actions == ["create_profile", "set_status"]


# --- scripts/grant_owner.py ----------------------------------------------------------------


class TestGrantOwnerScript:
    def _argv(self, *extra: str, settings: Any) -> list[str]:
        return ["--table", settings.grant_table, *extra]

    def test_bootstrap_on_empty_table(self, ddb: Any, settings: Any, log_lines, capsys) -> None:
        rc = grant_owner.main(
            self._argv(
                "--bootstrap",
                "--subject",
                ROOT,
                "--email",
                "root@x",
                "--name",
                "Root",
                settings=settings,
            ),
            dynamodb_resource=ddb,
        )
        assert rc == 0
        store = GrantStore(settings.grant_table, ddb)
        grants = store.all_grants(ROOT)
        assert len(grants) == 1
        # The first owner can sign in to the web app: a PROFILE row exists (§3.3).
        profile = GrantAdmin(settings.grant_table, dynamodb_resource=ddb).get_profile(ROOT)
        assert profile is not None and profile.email == "root@x" and profile.status == "active"
        assert grants[0].node == "/"
        assert grants[0].permission is Permission.OWN
        assert grants[0].granted_by == grant_owner.BOOTSTRAP_GRANTER
        warned = [ln for ln in log_lines() if ln["level"] == "WARNING"]
        assert warned and warned[0]["action"] == "bootstrap"
        assert warned[0]["subject"] == ROOT
        assert "BOOTSTRAP" in capsys.readouterr().err

    def test_bootstrap_refused_when_root_has_an_owner(
        self, seeded: GrantAdmin, ddb: Any, settings: Any, capsys
    ) -> None:
        rc = grant_owner.main(
            self._argv("--bootstrap", "--subject", BOB, "--email", "bob@x", settings=settings),
            dynamodb_resource=ddb,
        )
        assert rc == 2
        assert "refused" in capsys.readouterr().err
        assert seeded.grants_of(BOB) == []

    @pytest.mark.parametrize(
        "extra",
        [
            ["--bootstrap", "--email", "r@x", "--node", "/racing"],
            ["--bootstrap", "--email", "r@x", "--permission", "read"],
            ["--bootstrap", "--email", "r@x", "--granter", ROOT],
            ["--bootstrap"],  # no --email: the first owner could never sign in
            [],  # neither --bootstrap nor --granter
        ],
    )
    def test_argument_errors(self, ddb: Any, settings: Any, extra: list[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            grant_owner.main(
                self._argv(*extra, "--subject", BOB, settings=settings), dynamodb_resource=ddb
            )
        assert exc.value.code == 2

    def test_owner_grants_through_the_guard(
        self, seeded: GrantAdmin, ddb: Any, settings: Any
    ) -> None:
        rc = grant_owner.main(
            self._argv(
                "--granter",
                ALICE,
                "--subject",
                BOB,
                "--node",
                "/racing/setup",
                "--permission",
                "write",
                settings=settings,
            ),
            dynamodb_resource=ddb,
        )
        assert rc == 0
        assert seeded.store.resolve(BOB, "/racing/setup/x.md").permission is Permission.WRITE

    def test_non_owner_is_refused(
        self, seeded: GrantAdmin, ddb: Any, settings: Any, capsys
    ) -> None:
        rc = grant_owner.main(
            self._argv("--granter", BOB, "--subject", CARA, "--node", "/racing", settings=settings),
            dynamodb_resource=ddb,
        )
        assert rc == 2
        assert "refused" in capsys.readouterr().err
        assert seeded.grants_of(CARA) == []
