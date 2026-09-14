"""The read view (HANDOFF §11.6): browse, read, history, versions, search.

Articles are seeded straight into the moto bucket — frontmatter via ``serialize``,
attribution as object metadata — so these tests depend on the storage layer and
the web app only, never on the MCP tools. Grants go in through ``GrantStore``.

The property every route is held to (§4.8): a reader granted ``/racing`` and nothing
else never causes a credential to be minted for a path outside ``/racing``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.auth.admin import GrantAdmin
from app.auth.grants import GrantStore
from app.auth.types import Grant, Permission
from app.config import Settings
from app.storage.articles import key_for
from app.storage.markdown import serialize
from app.web import app as web_app
from app.web import session as sess
from app.web.views.read import HISTORY_NOTE
from tests.unit.web.conftest import SUBJECT, call, event

OTHER = "user_02OTHER"


# --- fixtures ----------------------------------------------------------------------------


@pytest.fixture
def put_raw(bucket: Any, settings: Settings) -> Callable[..., str]:
    """Write one object version directly; returns its S3 version id."""

    def _put(path: str, frontmatter: dict[str, Any], body: str = "", **metadata: str) -> str:
        meta = {"actor": f"human:{SUBJECT}", "kind": "write", **metadata}
        response = bucket.put_object(
            Bucket=settings.bucket,
            Key=key_for(path),
            Body=serialize(frontmatter, body),
            Metadata=meta,
        )
        return str(response["VersionId"])

    return _put


@pytest.fixture
def grant(admin: GrantAdmin) -> Callable[[str, str], None]:
    store: GrantStore = admin.store

    def _grant(node: str, permission: str = "read", subject: str = SUBJECT) -> None:
        store.put_grant(
            Grant(
                subject=subject,
                node=node,
                permission=Permission(permission),
                granted_by="bootstrap",
                granted_at="2026-01-01T00:00:00Z",
            )
        )

    return _grant


@pytest.fixture
def tree(put_raw: Callable[..., str]) -> dict[str, str]:
    """A small tree with something inside and outside ``/racing``."""
    ids: dict[str, str] = {}
    ids["/racing/setup/rear-bar.md"] = put_raw(
        "/racing/setup/rear-bar.md",
        {
            "type": "doc",
            "title": "Rear bar",
            "description": "Rear anti-roll bar settings.",
            "tags": ["setup", "handling"],
            "status": "stable",
            "verified": [{"by": f"human:{SUBJECT}", "at": "2026-02-01T00:00:00Z"}],
            "sources": [{"resource": "https://example.com/bars", "title": "Bar catalogue"}],
            "seq": 1,
        },
        "# Rear bar\n\nStiffer is faster.\n\n## Settings\n\n<script>alert(1)</script>\n\n"
        "See [the wing](/racing/aero/wing.md).\n",
    )
    ids["/racing/aero/wing.md"] = put_raw(
        "/racing/aero/wing.md",
        {"type": "doc", "title": "Rear wing", "tags": ["aero"], "seq": 1},
        "Downforce.\n",
    )
    ids["/finance/budget.md"] = put_raw(
        "/finance/budget.md",
        {"type": "doc", "title": "Rear-of-season budget", "description": "Money.", "seq": 1},
        "Secret numbers.\n",
    )
    ids["/top.md"] = put_raw("/top.md", {"type": "doc", "title": "Top page", "seq": 1}, "Hi.\n")
    return ids


def get(path: str, cookie: str, **query: str) -> dict[str, Any]:
    return call(event("GET", path, cookies={sess.SESSION_COOKIE: cookie}, query=query or None))


def mints() -> list[tuple[str, str, str]]:
    """Every ``(subject, shape, path)`` the fake minter was asked for in this test."""
    minter = web_app._minter
    return list(getattr(minter, "minted", [])) if minter is not None else []


def assert_mints_within(folder: str) -> None:
    for _subject, _shape, path in mints():
        assert path == folder or path.startswith(folder + "/"), f"minted outside {folder}: {path}"


# --- session gate ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/app/",
        "/app/a",
        "/app/a/racing",
        "/app/a/racing/x.md",
        "/app/history/racing/x.md",
        "/app/v/racing/x.md",
        "/app/search",
    ],
)
def test_every_read_route_requires_a_session(path: str) -> None:
    out = call(event("GET", path))
    assert out["statusCode"] == 303
    assert out["headers"]["Location"].startswith("/app/login?next=")
    assert mints() == []


# --- home and listings ----------------------------------------------------------------


def test_home_for_root_reader_lists_root_and_explains(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/")
    out = get("/app/", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "What you can see" in body
    assert 'href="/app/a/racing"' in body and 'href="/app/a/finance"' in body
    assert 'href="/app/a/top.md"' in body


def test_home_for_subtree_reader_shows_entry_points_not_root(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    out = get("/app/", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert 'everything under <a href="/app/a/racing">/racing</a>' in body
    assert "finance" not in body and "top.md" not in body
    assert mints() == []  # no root listing was read


def test_home_with_no_grants_says_so(session_cookie: str, tree: dict[str, str]) -> None:
    out = get("/app/", session_cookie)
    assert out["statusCode"] == 200
    assert "not been granted anything" in out["body"]
    assert "racing" not in out["body"]
    assert mints() == []


def test_root_listing_shows_only_readable_things(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/")
    out = get("/app/a/", session_cookie)
    assert out["statusCode"] == 200
    assert 'href="/app/a/racing"' in out["body"] and 'href="/app/a/top.md"' in out["body"]
    assert any(shape == "list" and path == "/" for _s, shape, path in mints())


def test_folder_under_grant_renders_rows(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    out = get("/app/a/racing/setup", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "Rear bar" in body and "Rear anti-roll bar settings." in body
    assert 'href="/app/a/racing/setup/rear-bar.md"' in body
    assert "human-reviewed" in body and "setup" in body
    # Breadcrumbs link the ancestors.
    assert 'href="/app/a/racing">racing</a>' in body
    assert mints() == [(SUBJECT, "list", "/racing/setup")]


def test_folder_writer_gets_maintain_credential(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing", "write")
    out = get("/app/a/racing", session_cookie)
    assert out["statusCode"] == 200
    assert mints() == [(SUBJECT, "maintain", "/racing")]


def test_folder_outside_grants_is_404_without_a_mint(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    for path in ("/app/a/finance", "/app/a/", "/app/a", "/app/a/nowhere"):
        out = get(path, session_cookie)
        assert out["statusCode"] == 404, path
    assert mints() == []


def test_empty_folder_under_grant_is_404(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    assert get("/app/a/racing/nothing-here", session_cookie)["statusCode"] == 404


# --- articles ----------------------------------------------------------------------------


def test_article_renders_body_panel_and_history_note(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    out = get("/app/a/racing/setup/rear-bar.md", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "<h1>Rear bar</h1>" in body
    assert "Stiffer is faster." in body
    # Raw HTML in the body is escaped, never rendered.
    assert "<script>" not in body and "&lt;script&gt;" in body
    # Internal link rewritten to the app route.
    assert 'href="/app/a/racing/aero/wing.md"' in body
    # Frontmatter panel and trust badge with the verified entry.
    assert "Rear anti-roll bar settings." in body
    assert 'class="trust human-reviewed"' in body
    assert f"verified by human:{SUBJECT}" in body
    assert 'class="tag">setup</span>' in body and 'class="tag">handling</span>' in body
    assert 'href="https://example.com/bars"' in body and "Bar catalogue" in body
    # TOC anchors on the rendered headings.
    assert 'id="settings"' in body and 'href="#settings"' in body
    # §4.5, stated where people read.
    assert HISTORY_NOTE in body
    assert 'href="/app/history/racing/setup/rear-bar.md"' in body
    assert mints() == [(SUBJECT, "read", "/racing/setup/rear-bar.md")]


def test_article_outside_grants_is_404_without_a_mint(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    out = get("/app/a/finance/budget.md", session_cookie)
    assert out["statusCode"] == 404
    assert "Secret" not in out["body"]
    assert mints() == []


def test_missing_article_under_grant_is_404(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    assert get("/app/a/racing/ghost.md", session_cookie)["statusCode"] == 404


def test_pointer_renders_moved_page_without_reading_destination(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/")
    put_raw(
        "/racing/old.md",
        {
            "type": "pointer",
            "moved_to": "/archive/old.md",
            "moved_at": "2026-03-01T00:00:00Z",
            "seq": 4,
        },
        "This article moved to /archive/old.md.\n",
        kind="moved_out",
    )
    put_raw("/archive/old.md", {"type": "doc", "title": "Old", "seq": 4}, "Destination body.\n")
    out = get("/app/a/racing/old.md", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "This page moved" in body
    assert 'href="/app/a/archive/old.md"' in body
    assert "Destination body" not in body
    assert mints() == [(SUBJECT, "read", "/racing/old.md")]


def test_tombstone_is_404(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/")
    put_raw("/racing/gone.md", {"type": "archived", "title": "Gone", "seq": 2}, "", kind="archive")
    out = get("/app/a/racing/gone.md", session_cookie)
    assert out["statusCode"] == 404
    assert "Gone" not in out["body"]


# --- history and versions -----------------------------------------------------------------


def test_history_lists_versions_newest_first_with_display_names(
    session_cookie: str, admin: GrantAdmin, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    admin.create_profile(OTHER, "other@example.com", "Dana Other", "active")
    path = "/racing/setup/rear-bar.md"
    v1 = put_raw(path, {"type": "doc", "title": "Rear bar", "seq": 1}, "one\n")
    fm = {"type": "doc", "title": "Rear bar"}
    v2 = put_raw(path, {**fm, "seq": 2}, "two\n", actor=f"human:{OTHER}")
    v3 = put_raw(path, {**fm, "seq": 3}, "three\n", actor="agent/1.0")
    out = get(f"/app/history{path}", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert body.index(v3) < body.index(v2) < body.index(v1)
    assert "Dana Other" in body and "Test Person" in body and "agent/1.0" in body
    assert f'href="/app/v{path}?version={v3}"' in body
    assert HISTORY_NOTE in body
    assert "Earlier history" not in body
    assert mints() == [(SUBJECT, "read", path)]


def test_history_pages_with_cursor(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    path = "/racing/many.md"
    ids = [put_raw(path, {"type": "doc", "seq": i}, f"v{i}\n") for i in range(1, 24)]
    first = get(f"/app/history{path}", session_cookie)
    assert first["statusCode"] == 200
    assert ids[-1] in first["body"] and ids[0] not in first["body"]
    assert "Older versions" in first["body"]
    cursor = first["body"].split("?cursor=", 1)[1].split('"', 1)[0]
    second = get(f"/app/history{path}", session_cookie, cursor=cursor)
    assert second["statusCode"] == 200
    assert ids[0] in second["body"] and "Older versions" not in second["body"]
    bad = get(f"/app/history{path}", session_cookie, cursor="not-a-cursor")
    assert bad["statusCode"] == 400


def test_history_continues_at_links_old_path_without_reading_it(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    path = "/racing/arrived.md"
    put_raw(
        path,
        {"type": "doc", "title": "Arrived", "moved_from": "/private/origin.md", "seq": 5},
        "Now here.\n",
        kind="moved_in",
        **{"moved-from": "/private/origin.md"},
    )
    put_raw(path, {"type": "doc", "title": "Arrived", "seq": 6}, "Edited here.\n")
    put_raw("/private/origin.md", {"type": "pointer", "moved_to": path, "seq": 5}, kind="moved_out")
    out = get(f"/app/history{path}", session_cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "Earlier history lives at" in body and "(separate permission)" in body
    assert 'href="/app/history/private/origin.md"' in body
    assert mints() == [(SUBJECT, "read", path)]
    # And following it is a fresh, separately authorized request: denied here.
    assert get("/app/history/private/origin.md", session_cookie)["statusCode"] == 404
    assert_mints_within("/racing")


def test_history_outside_grants_or_absent_is_404(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    assert get("/app/history/finance/budget.md", session_cookie)["statusCode"] == 404
    assert get("/app/history/racing/ghost.md", session_cookie)["statusCode"] == 404
    assert get("/app/history/racing", session_cookie)["statusCode"] == 404
    assert mints() == [(SUBJECT, "read", "/racing/ghost.md")]


def test_version_page_renders_the_historical_body(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    path = "/racing/setup/rear-bar.md"
    v1 = put_raw(path, {"type": "doc", "title": "Rear bar", "seq": 1}, "The old text.\n")
    put_raw(path, {"type": "doc", "title": "Rear bar", "seq": 2}, "The new text.\n")
    out = get(f"/app/v{path}", session_cookie, version=v1)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "Version 1" in body and "Test Person" in body
    assert "The old text." in body and "The new text." not in body
    assert f'href="/app/a{path}"' in body and f'href="/app/history{path}"' in body
    assert HISTORY_NOTE in body
    assert mints() == [(SUBJECT, "read", path)]


def test_version_page_for_pointer_version_is_a_note_not_a_forward_reference(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    path = "/racing/old.md"
    put_raw(path, {"type": "doc", "title": "Old", "seq": 1}, "Body.\n")
    vp = put_raw(
        path, {"type": "pointer", "moved_to": "/elsewhere/old.md", "seq": 2}, "", kind="moved_out"
    )
    out = get(f"/app/v{path}", session_cookie, version=vp)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "move pointer" in body
    assert "This page moved" not in body
    assert 'href="/app/a/elsewhere/old.md"' not in body
    assert mints() == [(SUBJECT, "read", path)]


def test_version_missing_or_foreign_is_404(
    session_cookie: str, grant: Callable[..., None], put_raw: Callable[..., str]
) -> None:
    grant("/racing")
    put_raw("/racing/a.md", {"type": "doc", "seq": 1}, "a\n")
    vb = put_raw("/racing/b.md", {"type": "doc", "seq": 1}, "b\n")
    assert get("/app/v/racing/a.md", session_cookie)["statusCode"] == 404
    assert get("/app/v/racing/a.md", session_cookie, version="nope")["statusCode"] == 404
    assert get("/app/v/racing/a.md", session_cookie, version=vb)["statusCode"] == 404
    assert get("/app/v/finance/x.md", session_cookie, version=vb)["statusCode"] == 404
    assert_mints_within("/racing")


# --- search -----------------------------------------------------------------------------


def test_search_empty_query_is_just_the_form(session_cookie: str) -> None:
    out = get("/app/search", session_cookie)
    assert out["statusCode"] == 200
    assert 'name="q"' in out["body"] and "Results" not in out["body"]
    assert mints() == []


def test_search_finds_under_folder_grant_and_nothing_outside(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    out = get("/app/search", session_cookie, q="rear")
    assert out["statusCode"] == 200
    body = out["body"]
    assert 'href="/app/a/racing/setup/rear-bar.md"' in body
    assert 'href="/app/a/racing/aero/wing.md"' in body
    assert "budget" not in body and "finance" not in body
    assert mints() == [(SUBJECT, "list", "/racing")]


def test_search_prefix_narrows_and_never_widens(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/racing")
    narrowed = get("/app/search", session_cookie, q="rear", prefix="/racing/aero")
    assert 'href="/app/a/racing/aero/wing.md"' in narrowed["body"]
    assert "rear-bar" not in narrowed["body"]
    outside = get("/app/search", session_cookie, q="rear", prefix="/finance")
    assert outside["statusCode"] == 200 and "budget" not in outside["body"]
    assert "Nothing matched" in outside["body"]
    malformed = get("/app/search", session_cookie, q="rear", prefix="/Finance/")
    assert malformed["statusCode"] == 200 and "absolute, lowercase" in malformed["body"]
    assert_mints_within("/racing")


def test_article_grant_is_searchable_but_parent_folder_is_404(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/finance/budget.md")
    found = get("/app/search", session_cookie, q="budget")
    assert found["statusCode"] == 200
    assert 'href="/app/a/finance/budget.md"' in found["body"]
    assert mints() == [(SUBJECT, "read", "/finance/budget.md")]
    assert get("/app/a/finance", session_cookie)["statusCode"] == 404
    assert get("/app/a/finance/budget.md", session_cookie)["statusCode"] == 200
    assert_mints_within("/finance/budget.md")


def test_search_with_no_grants_mints_nothing(session_cookie: str, tree: dict[str, str]) -> None:
    out = get("/app/search", session_cookie, q="rear")
    assert out["statusCode"] == 200 and "Nothing matched" in out["body"]
    assert mints() == []


# --- path safety ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "../x",
        "racing/../finance/budget.md",
        "Racing/X.md",
        "_secret/x.md",
        "racing/_listing.json",
        "racing//setup",
        "racing/setup//",
    ],
)
def test_malformed_paths_are_404_without_a_mint(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str], raw: str
) -> None:
    grant("/")
    for route in ("a", "history", "v"):
        out = get(f"/app/{route}/{raw}", session_cookie)
        assert out["statusCode"] == 404, (route, raw)
    assert mints() == []


def test_one_trailing_slash_on_a_folder_is_tolerated(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/")
    assert get("/app/a/racing/setup/", session_cookie)["statusCode"] == 200
    assert get("/app/a/racing/", session_cookie)["statusCode"] == 200


def test_url_encoded_traversal_is_404(
    session_cookie: str, grant: Callable[..., None], tree: dict[str, str]
) -> None:
    grant("/")
    out = get("/app/a/racing/%2e%2e/finance/budget.md", session_cookie)
    assert out["statusCode"] == 404
    assert mints() == []


# --- the §4.8 property, across every route ----------------------------------------------


def test_racing_reader_never_mints_outside_racing(
    session_cookie: str,
    grant: Callable[..., None],
    put_raw: Callable[..., str],
    tree: dict[str, str],
) -> None:
    grant("/racing")
    v = put_raw("/racing/setup/rear-bar.md", {"type": "doc", "title": "Rear bar", "seq": 2}, "x\n")
    put_raw(
        "/racing/moved.md",
        {"type": "pointer", "moved_to": "/finance/moved.md", "seq": 2},
        "",
        kind="moved_out",
    )
    requests = [
        ("/app/", {}),
        ("/app/a", {}),
        ("/app/a/", {}),
        ("/app/a/racing", {}),
        ("/app/a/racing/setup", {}),
        ("/app/a/finance", {}),
        ("/app/a/racing/setup/rear-bar.md", {}),
        ("/app/a/racing/moved.md", {}),
        ("/app/a/finance/budget.md", {}),
        ("/app/a/top.md", {}),
        ("/app/history/racing/setup/rear-bar.md", {}),
        ("/app/history/finance/budget.md", {}),
        ("/app/v/racing/setup/rear-bar.md", {"version": v}),
        ("/app/v/finance/budget.md", {"version": v}),
        ("/app/search", {"q": "rear budget top"}),
        ("/app/search", {"q": "rear", "prefix": "/finance"}),
        ("/app/search", {"q": "rear", "prefix": "/"}),
    ]
    for path, query in requests:
        out = get(path, session_cookie, **query)
        assert out["statusCode"] in (200, 404), (path, out["statusCode"])
        assert "Secret numbers" not in out["body"] and "Top page" not in out["body"], path
    assert mints(), "the racing routes did read something"
    assert_mints_within("/racing")
