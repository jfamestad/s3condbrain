"""Build step 2 — grant resolution, offline (HANDOFF §4.6, §8.5, §11.3).

The empty case is tested hardest: a subject with no matching grant must resolve to
nothing, whatever else is in the table.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
import pytest

from app.auth.grants import GrantStore, ancestors, effective
from app.auth.types import Grant, Permission, Resolution
from app.errors import ToolError

SUBJECT = "user_01ABC"


def _grant(node: str, permission: Permission, subject: str = SUBJECT) -> Grant:
    return Grant(
        subject=subject,
        node=node,
        permission=permission,
        granted_by="user_owner",
        granted_at="2026-09-13T00:00:00Z",
    )


# --- ancestors -----------------------------------------------------------------


class TestAncestors:
    def test_root(self) -> None:
        assert ancestors("/") == ["/"]

    def test_top_level_folder(self) -> None:
        assert ancestors("/racing") == ["/", "/racing"]

    def test_deep_article(self) -> None:
        assert ancestors("/racing/setup/rear-bar.md") == [
            "/",
            "/racing",
            "/racing/setup",
            "/racing/setup/rear-bar.md",
        ]

    def test_root_article(self) -> None:
        assert ancestors("/readme.md") == ["/", "/readme.md"]

    def test_folder_nodes_carry_no_trailing_slash(self) -> None:
        for node in ancestors("/a/b/c/d.md")[:-1]:
            assert not node.endswith("/") or node == "/"

    def test_trailing_slash_on_folder_is_tolerated(self) -> None:
        assert ancestors("/racing/") == ["/", "/racing"]

    @pytest.mark.parametrize("bad", ["", "racing", "racing/x.md", "./x.md", "../x.md"])
    def test_relative_path_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            ancestors(bad)

    @pytest.mark.parametrize("bad", ["/racing/../x.md", "/racing/./x.md", "/racing//x.md"])
    def test_dot_segments_and_empty_segments_raise(self, bad: str) -> None:
        with pytest.raises(ValueError):
            ancestors(bad)


# --- effective: the empty case -----------------------------------------------------


class TestEffectiveEmpty:
    """§11.3 step 2: test the empty case hardest."""

    def test_no_grants_at_all(self) -> None:
        assert effective([], "/racing/setup/rear-bar.md") == Resolution(None, ())

    def test_no_grants_at_root(self) -> None:
        assert effective([], "/") == Resolution(None, ())

    def test_grant_on_sibling_folder(self) -> None:
        grants = [_grant("/cooking", Permission.OWN)]
        assert effective(grants, "/racing/setup/rear-bar.md") == Resolution(None, ())

    def test_grant_on_sibling_folder_sharing_a_prefix_string(self) -> None:
        # "/racing-old" is not an ancestor of "/racing/..." even though it shares a prefix.
        grants = [_grant("/racing-old", Permission.OWN), _grant("/rac", Permission.OWN)]
        assert effective(grants, "/racing/setup/rear-bar.md") == Resolution(None, ())

    def test_grant_deeper_than_requested_path(self) -> None:
        grants = [_grant("/racing/setup", Permission.OWN)]
        assert effective(grants, "/racing") == Resolution(None, ())

    def test_article_grant_does_not_reach_its_folder(self) -> None:
        grants = [_grant("/racing/setup/rear-bar.md", Permission.OWN)]
        assert effective(grants, "/racing/setup") == Resolution(None, ())
        assert effective(grants, "/racing") == Resolution(None, ())
        assert effective(grants, "/") == Resolution(None, ())

    def test_article_grant_on_different_article_in_same_folder(self) -> None:
        grants = [_grant("/racing/setup/front-bar.md", Permission.OWN)]
        assert effective(grants, "/racing/setup/rear-bar.md") == Resolution(None, ())

    def test_grants_for_another_subject_are_still_matched_by_node_only(self) -> None:
        # ``effective`` is pure and trusts its caller to pass one subject's grants;
        # this pins that it does not silently filter by subject.
        grants = [_grant("/racing", Permission.READ, subject="someone_else")]
        assert effective(grants, "/racing/x.md").permission is Permission.READ

    def test_result_is_falsy_for_every_permission(self) -> None:
        res = effective([], "/racing/x.md")
        for needed in Permission:
            assert not res.allows(needed)


# --- effective: cascade, isolation, union ------------------------------------------


class TestEffectiveCascade:
    def test_folder_grant_cascades_to_article_beneath(self) -> None:
        g = _grant("/racing", Permission.READ)
        res = effective([g], "/racing/setup/rear-bar.md")
        assert res.permission is Permission.READ
        assert res.grants_used == (g,)

    def test_root_grant_cascades_everywhere(self) -> None:
        g = _grant("/", Permission.OWN)
        assert effective([g], "/anything/at/all.md").permission is Permission.OWN
        assert effective([g], "/").permission is Permission.OWN

    def test_article_grant_never_cascades_beneath_its_path_string(self) -> None:
        # A grant on the *article* /racing/x.md must not reach /racing/x.md/y.md even
        # though "/racing/x.md" is, as a string, an ancestor of it (review 2026-09-24).
        g = _grant("/racing/x.md", Permission.WRITE)
        assert effective([g], "/racing/x.md") == Resolution(Permission.WRITE, (g,))
        assert effective([g], "/racing/x.md/y.md") == Resolution(None, ())
        assert effective([g], "/racing/x.md/deeper/z.md") == Resolution(None, ())

    def test_folder_grant_matches_the_folder_itself(self) -> None:
        g = _grant("/racing", Permission.WRITE)
        assert effective([g], "/racing") == Resolution(Permission.WRITE, (g,))

    def test_article_grant_matches_that_article(self) -> None:
        g = _grant("/racing/setup/rear-bar.md", Permission.READ)
        res = effective([g], "/racing/setup/rear-bar.md")
        assert res == Resolution(Permission.READ, (g,))

    def test_article_grant_does_not_cascade_to_sibling(self) -> None:
        g = _grant("/racing/setup/rear-bar.md", Permission.OWN)
        assert effective([g], "/racing/setup/front-bar.md") == Resolution(None, ())

    def test_union_picks_strongest_across_ancestors(self) -> None:
        weak = _grant("/", Permission.READ)
        strong = _grant("/racing/setup", Permission.WRITE)
        res = effective([weak, strong], "/racing/setup/rear-bar.md")
        assert res.permission is Permission.WRITE

    def test_union_strongest_may_be_higher_in_the_tree(self) -> None:
        strong = _grant("/", Permission.OWN)
        weak = _grant("/racing/setup/rear-bar.md", Permission.READ)
        res = effective([weak, strong], "/racing/setup/rear-bar.md")
        assert res.permission is Permission.OWN

    def test_grants_used_lists_every_contributor_sorted_by_depth(self) -> None:
        root = _grant("/", Permission.READ)
        folder = _grant("/racing", Permission.WRITE)
        article = _grant("/racing/x.md", Permission.READ)
        unrelated = _grant("/cooking", Permission.OWN)
        res = effective([article, unrelated, root, folder], "/racing/x.md")
        assert res.permission is Permission.WRITE
        assert res.grants_used == (root, folder, article)
        assert unrelated not in res.grants_used

    def test_accepts_any_iterable(self) -> None:
        g = _grant("/racing", Permission.READ)
        assert effective(iter([g]), "/racing/x.md").permission is Permission.READ

    def test_invalid_path_raises(self) -> None:
        with pytest.raises(ValueError):
            effective([_grant("/", Permission.OWN)], "racing/x.md")


# --- GrantStore against moto ----------------------------------------------------------


@pytest.fixture
def store(grant_table, settings) -> GrantStore:
    ddb = boto3.resource("dynamodb", region_name=os.environ["AWS_DEFAULT_REGION"])
    return GrantStore(settings.grant_table, dynamodb_resource=ddb)


class TestGrantStore:
    def test_empty_table_resolves_to_nothing(self, store: GrantStore) -> None:
        assert store.grants_for(SUBJECT, "/racing/setup/rear-bar.md") == []
        assert store.resolve(SUBJECT, "/racing/setup/rear-bar.md") == Resolution(None, ())

    def test_put_then_resolve_direct(self, store: GrantStore) -> None:
        g = _grant("/racing/setup/rear-bar.md", Permission.READ)
        store.put_grant(g)
        assert store.resolve(SUBJECT, "/racing/setup/rear-bar.md") == Resolution(
            Permission.READ, (g,)
        )

    def test_put_then_resolve_cascade(self, store: GrantStore) -> None:
        g = _grant("/racing", Permission.WRITE)
        store.put_grant(g)
        res = store.resolve(SUBJECT, "/racing/setup/rear-bar.md")
        assert res.permission is Permission.WRITE
        assert res.grants_used == (g,)

    def test_put_then_resolve_union(self, store: GrantStore) -> None:
        root = _grant("/", Permission.READ)
        folder = _grant("/racing", Permission.OWN)
        store.put_grant(root)
        store.put_grant(folder)
        res = store.resolve(SUBJECT, "/racing/x.md")
        assert res.permission is Permission.OWN
        assert res.grants_used == (root, folder)

    def test_other_subjects_grants_are_invisible(self, store: GrantStore) -> None:
        store.put_grant(_grant("/", Permission.OWN, subject="someone_else"))
        assert store.resolve(SUBJECT, "/racing/x.md") == Resolution(None, ())
        assert store.grants_for(SUBJECT, "/") == []

    def test_sibling_grant_is_not_fetched(self, store: GrantStore) -> None:
        store.put_grant(_grant("/cooking", Permission.OWN))
        store.put_grant(_grant("/racing/other.md", Permission.OWN))
        assert store.grants_for(SUBJECT, "/racing/x.md") == []

    def test_grants_for_returns_grant_rows(self, store: GrantStore) -> None:
        g = _grant("/racing", Permission.READ)
        store.put_grant(g)
        rows = store.grants_for(SUBJECT, "/racing/x.md")
        assert rows == [g]
        assert rows[0].granted_by == "user_owner"
        assert rows[0].granted_at == "2026-09-13T00:00:00Z"

    def test_require_raises_forbidden_with_no_grant(self, store: GrantStore) -> None:
        with pytest.raises(ToolError) as exc:
            store.require(SUBJECT, "/racing/x.md", Permission.READ)
        assert exc.value.status == 403
        assert exc.value.code == "forbidden"
        assert "wiki." not in exc.value.message
        assert "scope" not in exc.value.message.lower()

    def test_require_raises_forbidden_when_too_weak(self, store: GrantStore) -> None:
        store.put_grant(_grant("/racing", Permission.READ))
        with pytest.raises(ToolError) as exc:
            store.require(SUBJECT, "/racing/x.md", Permission.WRITE)
        assert exc.value.status == 403
        assert exc.value.code == "forbidden"

    def test_require_returns_resolution_when_satisfied(self, store: GrantStore) -> None:
        g = _grant("/racing", Permission.WRITE)
        store.put_grant(g)
        res = store.require(SUBJECT, "/racing/x.md", Permission.READ)
        assert res.permission is Permission.WRITE
        assert res.grants_used == (g,)

    def test_all_grants_excludes_profile(self, store: GrantStore, grant_table) -> None:
        grant_table.put_item(
            Item={
                "pk": f"U#{SUBJECT}",
                "sk": "PROFILE",
                "email": "someone@example.com",
                "display_name": "Someone",
                "status": "active",
            }
        )
        a = _grant("/racing", Permission.READ)
        b = _grant("/cooking/soup.md", Permission.WRITE)
        store.put_grant(a)
        store.put_grant(b)
        store.put_grant(_grant("/", Permission.OWN, subject="someone_else"))
        rows = store.all_grants(SUBJECT)
        assert sorted(rows, key=lambda g: g.node) == sorted([a, b], key=lambda g: g.node)
        assert all(g.node.startswith("/") for g in rows)

    def test_all_grants_empty_for_unknown_subject(self, store: GrantStore) -> None:
        assert store.all_grants("nobody") == []

    def test_put_grant_writes_gsi_keys(self, store: GrantStore, grant_table) -> None:
        g = _grant("/racing", Permission.READ)
        store.put_grant(g)
        item = grant_table.get_item(Key={"pk": f"U#{SUBJECT}", "sk": "/racing"})["Item"]
        assert item["permission"] == "read"
        assert item["granted_by"] == "user_owner"
        assert item["granted_at"] == "2026-09-13T00:00:00Z"
        assert item["gs1pk"] == "N#/racing"
        assert item["gs1sk"] == SUBJECT

    def test_gsi_answers_who_can_reach_a_node(self, store: GrantStore, grant_table) -> None:
        store.put_grant(_grant("/racing", Permission.READ, subject="alice"))
        store.put_grant(_grant("/racing", Permission.WRITE, subject="bob"))
        store.put_grant(_grant("/cooking", Permission.OWN, subject="carol"))
        from boto3.dynamodb.conditions import Key

        out = grant_table.query(
            IndexName="gs1", KeyConditionExpression=Key("gs1pk").eq("N#/racing")
        )
        assert sorted(i["gs1sk"] for i in out["Items"]) == ["alice", "bob"]

    def test_default_resource_is_constructed_lazily(self, aws, settings) -> None:
        # No injected resource: the store builds boto3.resource("dynamodb") itself.
        s = GrantStore(settings.grant_table)
        assert s is not None


class TestDisabledProfile:
    """§12.9: disabling a person takes effect on their next request, with no extra
    round trip — the PROFILE row rides in the ancestor batch."""

    def test_disabled_profile_resolves_to_nothing(self, grant_table: Any, settings: Any) -> None:
        store = GrantStore(settings.grant_table)
        store.put_grant(Grant("user_d", "/", Permission.OWN))
        grant_table.put_item(
            Item={"pk": "U#user_d", "sk": "PROFILE", "email": "d@x", "status": "disabled"}
        )
        assert store.resolve("user_d", "/racing/x.md").permission is None
        with pytest.raises(ToolError) as e:
            store.require("user_d", "/", Permission.READ)
        assert e.value.status == 403

    def test_active_and_missing_profiles_keep_grants(self, grant_table: Any, settings: Any) -> None:
        store = GrantStore(settings.grant_table)
        store.put_grant(Grant("user_a", "/", Permission.OWN))
        assert store.resolve("user_a", "/x.md").permission is Permission.OWN  # no profile row
        grant_table.put_item(
            Item={"pk": "U#user_a", "sk": "PROFILE", "email": "a@x", "status": "active"}
        )
        assert store.resolve("user_a", "/x.md").permission is Permission.OWN

    def test_all_grants_is_empty_for_disabled_profile(
        self, grant_table: Any, settings: Any
    ) -> None:
        """Search builds its area from ``all_grants``; a disabled person must get
        nothing there too — detected from the same Query, no extra round trip."""
        store = GrantStore(settings.grant_table)
        store.put_grant(Grant("user_d", "/racing", Permission.READ))
        store.put_grant(Grant("user_d", "/kitchen/bread.md", Permission.WRITE))
        grant_table.put_item(
            Item={"pk": "U#user_d", "sk": "PROFILE", "email": "d@x", "status": "disabled"}
        )
        assert store.all_grants("user_d") == []

    def test_all_grants_keeps_grants_for_active_and_missing_profiles(
        self, grant_table: Any, settings: Any
    ) -> None:
        store = GrantStore(settings.grant_table)
        g = Grant("user_a", "/racing", Permission.READ)
        store.put_grant(g)
        assert store.all_grants("user_a") == [g]  # no profile row
        grant_table.put_item(
            Item={"pk": "U#user_a", "sk": "PROFILE", "email": "a@x", "status": "active"}
        )
        assert store.all_grants("user_a") == [g]

    def test_grant_rows_is_the_inventory_and_ignores_status(
        self, grant_table: Any, settings: Any
    ) -> None:
        """The admin console reviews a disabled person's grants; only the
        authorizing readers (``all_grants``, ``grants_for``) hide them."""
        store = GrantStore(settings.grant_table)
        g = Grant("user_d", "/racing", Permission.READ)
        store.put_grant(g)
        grant_table.put_item(
            Item={"pk": "U#user_d", "sk": "PROFILE", "email": "d@x", "status": "disabled"}
        )
        assert store.grant_rows("user_d") == [g]
        assert store.all_grants("user_d") == []
        assert store.grants_for("user_d", "/racing/x.md") == []
