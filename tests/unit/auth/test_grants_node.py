"""GSI1 — who can reach a node (HANDOFF §4.6, §8.5, increment B).

``grants_for_node`` answers for exactly one node; ``subjects_reaching`` unions it over
the ancestor chain and resolves per subject, which is what the move-impact report
consumes. The empty case is tested first, as with the rest of the grant layer.
"""

from __future__ import annotations

import os

import boto3
import pytest

from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission, Resolution

ALICE = "user_alice"
BOB = "user_bob"
CARA = "user_cara"
DAN = "user_dan"

ARTICLE = "/racing/setup/rear-bar.md"


def _grant(subject: str, node: str, permission: Permission) -> Grant:
    return Grant(
        subject=subject,
        node=node,
        permission=permission,
        granted_by="user_owner",
        granted_at="2026-09-13T00:00:00Z",
    )


@pytest.fixture
def store(grant_table, settings) -> GrantStore:
    ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    return GrantStore(settings.grant_table, dynamodb_resource=ddb)


@pytest.fixture
def seeded(store: GrantStore) -> dict[str, Grant]:
    grants = {
        "alice_root": _grant(ALICE, "/", Permission.OWN),
        "bob_racing": _grant(BOB, "/racing", Permission.READ),
        "bob_article": _grant(BOB, ARTICLE, Permission.WRITE),
        "cara_article": _grant(CARA, ARTICLE, Permission.READ),
        "dan_cooking": _grant(DAN, "/cooking", Permission.OWN),
    }
    for grant in grants.values():
        store.put_grant(grant)
    # A profile row on the same partition as a grant must never surface as one.
    store.table.put_item(
        Item={"pk": f"U#{BOB}", "sk": "PROFILE", "display_name": "Bob", "status": "active"}
    )
    return grants


# --- grants_for_node ------------------------------------------------------------------


class TestGrantsForNode:
    def test_empty_table(self, store: GrantStore) -> None:
        assert store.grants_for_node("/") == []
        assert store.grants_for_node(ARTICLE) == []

    def test_exact_node_only(self, store: GrantStore, seeded: dict[str, Grant]) -> None:
        assert store.grants_for_node("/racing") == [seeded["bob_racing"]]
        # Neither the ancestor's grant nor the descendant's is attached to /racing.
        assert seeded["alice_root"] not in store.grants_for_node("/racing")
        assert seeded["bob_article"] not in store.grants_for_node("/racing")

    def test_root(self, store: GrantStore, seeded: dict[str, Grant]) -> None:
        assert store.grants_for_node("/") == [seeded["alice_root"]]

    def test_article_node_lists_every_subject(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        found = store.grants_for_node(ARTICLE)
        assert sorted(found, key=lambda g: g.subject) == [
            seeded["bob_article"],
            seeded["cara_article"],
        ]

    def test_node_nobody_holds(self, store: GrantStore, seeded: dict[str, Grant]) -> None:
        assert store.grants_for_node("/racing/setup") == []

    def test_prefix_string_is_not_a_match(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        store.put_grant(_grant(DAN, "/racing-old", Permission.OWN))
        assert store.grants_for_node("/racing") == [seeded["bob_racing"]]

    def test_pagination_is_followed(self, store: GrantStore) -> None:
        subjects = [f"user_{i:03d}" for i in range(120)]
        for subject in subjects:
            store.put_grant(_grant(subject, "/shared", Permission.READ))
        found = store.grants_for_node("/shared")
        assert sorted(g.subject for g in found) == subjects


# --- subjects_reaching -----------------------------------------------------------------


class TestSubjectsReaching:
    def test_empty_table(self, store: GrantStore) -> None:
        assert store.subjects_reaching(ARTICLE) == {}
        assert store.subjects_reaching("/") == {}

    def test_article_unions_ancestors_and_resolves_per_subject(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        reaching = store.subjects_reaching(ARTICLE)
        assert set(reaching) == {ALICE, BOB, CARA}
        assert reaching[ALICE] == Resolution(Permission.OWN, (seeded["alice_root"],))
        # Bob: read on the folder plus write on the article → write, both grants used.
        assert reaching[BOB].permission is Permission.WRITE
        assert reaching[BOB].grants_used == (seeded["bob_racing"], seeded["bob_article"])
        assert reaching[CARA] == Resolution(Permission.READ, (seeded["cara_article"],))

    def test_unrelated_subtree_does_not_reach(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        assert DAN not in store.subjects_reaching(ARTICLE)
        assert set(store.subjects_reaching("/cooking/soup.md")) == {ALICE, DAN}

    def test_article_grant_does_not_reach_its_folder(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        reaching = store.subjects_reaching("/racing/setup")
        assert set(reaching) == {ALICE, BOB}
        assert reaching[BOB] == Resolution(Permission.READ, (seeded["bob_racing"],))

    def test_root_reaches_root_grantees_only(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        assert set(store.subjects_reaching("/")) == {ALICE}

    def test_sibling_article_sees_folder_grants_only(
        self, store: GrantStore, seeded: dict[str, Grant]
    ) -> None:
        reaching = store.subjects_reaching("/racing/setup/front-bar.md")
        assert set(reaching) == {ALICE, BOB}
        assert reaching[BOB].permission is Permission.READ

    def test_profile_rows_are_ignored(self, store: GrantStore, seeded: dict[str, Grant]) -> None:
        # Bob's PROFILE row shares his partition; it has no gs1 keys and no path sk.
        for resolution in store.subjects_reaching(ARTICLE).values():
            assert all(g.node.startswith("/") for g in resolution.grants_used)

    def test_invalid_path_raises(self, store: GrantStore) -> None:
        with pytest.raises(ValueError):
            store.subjects_reaching("racing/x.md")
