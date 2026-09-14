"""The admin console (HANDOFF §11.6, §4.10, §12.9, §15.2): scope, sentences, the
people/grants/unowned/audit/delete pages, and the POSTs behind them.

Three viewers share one subject (``SUBJECT``): a root owner, an owner of ``/racing``
and an owner of nothing. The world around them: Josh owns ``/`` (bootstrap), Dana
writes ``/racing/setup``, Pat reads ``/private``. A ``/racing`` owner must never see
the word ``/private`` on any page — nor Pat's name, email or subject, nor be told
whether an email they type belongs to anyone.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.auth.admin import GrantAdmin
from app.auth.types import Grant, Permission
from app.web import session as sess
from app.web import workos as workos_mod
from app.web.audit import AuditResult, AuditRow
from app.web.views import admin as admin_view
from tests.unit.web.conftest import SUBJECT, call, csrf_for, event

JOSH = "user_josh"
DANA = "user_dana"
PAT = "user_pat"
NOW = "2026-09-13T12:00:00+00:00"
REVOCATION = admin_view.REVOCATION_SENTENCE


# --- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def world(admin: GrantAdmin, profile: Any) -> GrantAdmin:
    """Josh owns ``/``; Dana writes ``/racing/setup``; Pat reads ``/private``."""
    admin.create_profile(JOSH, "josh@example.com", "Josh", "active")
    admin.create_profile(DANA, "dana@example.com", "Dana", "active")
    admin.create_profile(PAT, "pat@example.com", "Pat", "active")
    admin.store.put_grant(Grant(JOSH, "/", Permission.OWN, "process:bootstrap", NOW))
    admin.store.put_grant(Grant(DANA, "/racing/setup", Permission.WRITE, JOSH, NOW))
    admin.store.put_grant(Grant(PAT, "/private", Permission.READ, JOSH, NOW))
    return admin


@pytest.fixture
def as_root(world: GrantAdmin) -> GrantAdmin:
    world.store.put_grant(Grant(SUBJECT, "/", Permission.OWN, JOSH, NOW))
    return world


@pytest.fixture
def as_racing(world: GrantAdmin) -> GrantAdmin:
    world.store.put_grant(Grant(SUBJECT, "/racing", Permission.OWN, JOSH, NOW))
    return world


@pytest.fixture
def as_nobody(world: GrantAdmin) -> GrantAdmin:
    return world


@pytest.fixture
def cookie(session_cookie: str) -> str:
    return session_cookie


def get(path: str, cookie: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    return call(event("GET", path, query=query, cookies={sess.SESSION_COOKIE: cookie}))


def post(path: str, cookie: str, form: dict[str, str], *, csrf: bool = True) -> dict[str, Any]:
    data = {**form, "csrf": csrf_for(cookie)} if csrf else dict(form)
    return call(event("POST", path, cookies={sess.SESSION_COOKIE: cookie}, form=data))


class FakeWorkOS:
    """Stands in for ``WorkOSClient``; records calls, never touches the network."""

    instances: list[FakeWorkOS] = []

    def __init__(self, api_key: str, *, existing: dict[str, str] | None = None, fail_invite=False):
        if api_key in ("", "REPLACE-ME"):
            raise RuntimeError(workos_mod.NOT_CONFIGURED)
        self.existing = existing or {}
        self.fail_invite = fail_invite
        self.created: list[tuple[str, str, str]] = []
        self.invited: list[str] = []
        FakeWorkOS.instances.append(self)

    def find_user_by_email(self, email: str) -> str | None:
        return self.existing.get(email)

    def create_user(self, email: str, first_name: str, last_name: str) -> str:
        self.created.append((email, first_name, last_name))
        return "user_new01"

    def send_invitation(self, email: str) -> None:
        if self.fail_invite:
            raise workos_mod.WorkOSError(400, "invalid_request", "nope")
        self.invited.append(email)


@pytest.fixture
def fake_workos(monkeypatch: pytest.MonkeyPatch) -> type[FakeWorkOS]:
    FakeWorkOS.instances = []
    monkeypatch.setattr(admin_view, "WorkOSClient", FakeWorkOS)
    return FakeWorkOS


# --- sentences ------------------------------------------------------------------------------


def test_sentences_read_as_prose() -> None:
    names = {DANA: "Dana", JOSH: "Josh"}
    assert (
        admin_view.sentence(Grant(DANA, "/racing/setup", Permission.WRITE, JOSH, NOW), names)
        == "Dana can write everything under /racing/setup (granted by Josh, 2026-09-13)"
    )
    assert admin_view.sentence(
        Grant(DANA, "/racing/x.md", Permission.READ, JOSH, NOW), names
    ).startswith("Dana can read the article /racing/x.md")
    own = admin_view.sentence(Grant(JOSH, "/", Permission.OWN, "process:bootstrap", ""), names)
    assert own == (
        "Josh owns everything and can grant access to anything beneath it "
        "(granted by the install, date unknown)"
    )


def test_scope_covers_by_ancestor_not_prefix_string() -> None:
    s = admin_view.Scope(("/racing",))
    assert s.covers("/racing") and s.covers("/racing/setup/x.md")
    assert not s.covers("/racingcars") and not s.covers("/") and not s.covers("nope")
    assert admin_view.Scope(("/",)).is_root and admin_view.Scope(("/",)).covers("/anything")
    assert not admin_view.Scope(()).owns_anything


# --- overview and scoping across every GET ---------------------------------------------


ALL_GETS = [
    "/app/admin",
    "/app/admin/people",
    "/app/admin/grants",
    "/app/admin/audit",
    f"/app/admin/people/{DANA}",
]


@pytest.mark.parametrize("path", ALL_GETS)
def test_owner_of_nothing_sees_the_sentence_and_no_data(
    as_nobody: GrantAdmin, cookie: str, path: str
) -> None:
    out = get(path, cookie)
    assert out["statusCode"] == 200
    assert "You don't own any part of the tree" in out["body"]
    for word in ("/private", "/racing/setup", "Dana", "Pat", "Revoke", "Add a person"):
        assert word not in out["body"]


@pytest.mark.parametrize("path", ["/app/admin/unowned"])
def test_owner_of_nothing_is_refused_root_pages(
    as_nobody: GrantAdmin, cookie: str, path: str
) -> None:
    assert get(path, cookie)["statusCode"] == 403


def test_index_root_owner(as_root: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin", cookie)["body"]
    assert "You own <strong>/</strong>" in body
    assert "/app/admin/unowned" in body and "/app/admin/people" in body
    assert "4 members" in body  # Josh, Dana, Pat, the viewer
    assert "4 in your part of the tree" in body


def test_index_racing_owner_scopes_counts_and_links(as_racing: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin", cookie)["body"]
    assert "<strong>/racing</strong>" in body
    assert "/app/admin/unowned" not in body
    assert "/app/admin/people" in body
    assert "2 with access here" in body  # Dana and the viewer's own grant
    assert "/private" not in body


# --- people ---------------------------------------------------------------------------------


def test_people_root_sees_everyone_with_sentences(as_root: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin/people", cookie)["body"]
    assert "Dana can write everything under /racing/setup (granted by Josh, 2026-09-13)" in body
    assert "Pat can read everything under /private (granted by Josh, 2026-09-13)" in body
    assert "Josh owns everything and can grant access" in body
    assert "Add a person" in body and REVOCATION in body
    assert "sign-in email from WorkOS" in body


def test_people_racing_owner_sees_only_people_with_access_there(
    as_racing: GrantAdmin, cookie: str
) -> None:
    body = get("/app/admin/people", cookie)["body"]
    assert "Dana can write everything under /racing/setup" in body
    assert "Pat" not in body and "/private" not in body
    assert "Josh owns everything" not in body  # Josh's own grant is on /, above the scope
    assert "Add a person" in body and REVOCATION in body


def test_person_page_root(as_root: GrantAdmin, cookie: str, settings: Any) -> None:
    out = get(f"/app/admin/people/{PAT}", cookie)
    body = out["body"]
    assert out["statusCode"] == 200
    assert "Pat can read everything under /private" in body
    assert "Revoke" in body and "Disable" in body
    assert settings.canonical_mcp_url in body
    assert "Add custom connector" in body and "cannot start on a phone" in body
    assert "one custom connector" in body
    assert REVOCATION in body


def test_person_page_racing_owner_scope(as_racing: GrantAdmin, cookie: str) -> None:
    assert get(f"/app/admin/people/{PAT}", cookie)["statusCode"] == 404
    body = get(f"/app/admin/people/{DANA}", cookie)["body"]
    assert "Dana can write everything under /racing/setup" in body
    assert "Revoke" in body
    assert "Disable" not in body  # root owners only
    assert "/private" not in body


def test_person_page_unknown_is_404(as_root: GrantAdmin, cookie: str) -> None:
    assert get("/app/admin/people/user_nobody", cookie)["statusCode"] == 404


def test_person_page_for_self_offers_no_disable(as_root: GrantAdmin, cookie: str) -> None:
    body = get(f"/app/admin/people/{SUBJECT}", cookie)["body"]
    assert "this is you" in body and "Disable" not in body


# --- adding a person (§15.2) -------------------------------------------------------------


def test_add_person_creates_invites_grants_and_shows_connector_block(
    as_racing: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS], settings: Any
) -> None:
    out = post(
        "/app/admin/people",
        cookie,
        {
            "email": "new@example.com",
            "display_name": "New Person",
            "node": "/racing/notes",
            "permission": "read",
        },
    )
    assert out["statusCode"] == 303
    assert out["headers"]["Location"] == "/app/admin/people/user_new01?added=1&invite=sent"
    wk = fake_workos.instances[-1]
    assert wk.created == [("new@example.com", "New", "Person")]
    assert wk.invited == ["new@example.com"]
    prof = as_racing.get_profile("user_new01")
    assert prof is not None and prof.status == "invited" and prof.email == "new@example.com"
    grants = as_racing.grants_of("user_new01")
    assert len(grants) == 1
    assert grants[0].node == "/racing/notes" and grants[0].permission is Permission.READ
    assert grants[0].granted_by == SUBJECT
    body = get(out["headers"]["Location"].split("?")[0], cookie, {"added": "1", "invite": "sent"})[
        "body"
    ]
    assert settings.canonical_mcp_url in body
    assert "emailed them a sign-in link" in body
    assert "New Person can read everything under /racing/notes" in body


def test_add_person_reuses_existing_workos_user(
    as_root: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS], monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        admin_view,
        "WorkOSClient",
        lambda key: FakeWorkOS(key, existing={"old@example.com": "user_old01"}),
    )
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "old@example.com", "display_name": "Old", "node": "/", "permission": "write"},
    )
    assert out["statusCode"] == 303 and "user_old01" in out["headers"]["Location"]
    assert fake_workos.instances[-1].created == []
    assert as_root.get_profile("user_old01") is not None


def test_add_person_outside_scope_is_403_before_workos(
    as_racing: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS]
) -> None:
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "x@example.com", "display_name": "X", "node": "/private", "permission": "read"},
    )
    assert out["statusCode"] == 403
    assert fake_workos.instances == []


def test_add_person_owner_of_nothing_is_403(
    as_nobody: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS]
) -> None:
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "x@example.com", "display_name": "X", "node": "/racing", "permission": "read"},
    )
    assert out["statusCode"] == 403 and fake_workos.instances == []


LOCATION_SHAPE = re.compile(r"^/app/admin/people/user_[^/?]+\?added=1&invite=(sent|none)$")


def test_add_person_existing_member_is_granted_like_a_new_one(
    as_racing: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS]
) -> None:
    """Pat exists but holds nothing under /racing. Adding her there is a grant and a
    303 to her page — the same shape as a brand-new account — so the owner is never
    told whether the email was known."""
    form = {"display_name": "Someone", "node": "/racing/notes", "permission": "read"}
    existing = post("/app/admin/people", cookie, {**form, "email": "PAT@example.com"})
    assert existing["statusCode"] == 303
    assert existing["headers"]["Location"] == f"/app/admin/people/{PAT}?added=1&invite=none"
    assert fake_workos.instances == []  # no WorkOS user, no invitation
    rows = {g.node: g for g in as_racing.grants_of(PAT)}
    assert rows["/racing/notes"].permission is Permission.READ
    assert rows["/racing/notes"].granted_by == SUBJECT
    assert as_racing.get_profile(PAT).status == "active"  # untouched
    page = get(f"/app/admin/people/{PAT}", cookie, {"added": "1", "invite": "none"})
    assert page["statusCode"] == 200  # legitimately in scope now
    assert "Pat can read everything under /racing/notes" in page["body"]
    assert "emailed them a sign-in link" not in page["body"]

    new = post("/app/admin/people", cookie, {**form, "email": "new@example.com"})
    assert new["statusCode"] == 303
    assert new["headers"]["Location"] == "/app/admin/people/user_new01?added=1&invite=sent"
    assert fake_workos.instances[-1].created == [("new@example.com", "Someone", "")]
    assert fake_workos.instances[-1].invited == ["new@example.com"]
    assert existing["statusCode"] == new["statusCode"]
    assert LOCATION_SHAPE.match(existing["headers"]["Location"])
    assert LOCATION_SHAPE.match(new["headers"]["Location"])


def test_add_person_existing_member_root_gets_the_same_grant(
    as_root: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS]
) -> None:
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "DANA@example.com", "display_name": "D", "node": "/", "permission": "write"},
    )
    assert out["statusCode"] == 303
    assert out["headers"]["Location"] == f"/app/admin/people/{DANA}?added=1&invite=none"
    assert "already a member" not in out.get("body", "")
    assert fake_workos.instances == []
    rows = {g.node: g for g in as_root.grants_of(DANA)}
    assert rows["/"].permission is Permission.WRITE and rows["/"].granted_by == SUBJECT


def test_add_person_disabled_member_is_the_one_refusal_for_every_owner(
    as_racing: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS]
) -> None:
    as_racing.set_status(PAT, "disabled")
    form = {"email": "pat@example.com", "node": "/racing/notes", "permission": "read"}
    subtree = post("/app/admin/people", cookie, form)
    as_racing.store.put_grant(Grant(SUBJECT, "/", Permission.OWN, JOSH, NOW))  # now root
    root = post("/app/admin/people", cookie, form)
    assert subtree["statusCode"] == root["statusCode"] == 400
    assert admin_view.EMAIL_NOT_ADDABLE in subtree["body"]
    assert subtree["body"] == root["body"]
    for word in ("Pat", PAT, "pat@example.com", "disabled", "already a member"):
        assert word not in subtree["body"], word
    assert fake_workos.instances == []
    assert [g.node for g in as_racing.grants_of(PAT)] == ["/private"]


def test_add_person_existing_workos_user_with_profile_is_granted_too(
    as_racing: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS], monkeypatch: Any
) -> None:
    """The typed email is new to us but WorkOS maps it to a subject that already has
    a profile (their email changed upstream). Same answer as an email we knew."""
    monkeypatch.setattr(
        admin_view,
        "WorkOSClient",
        lambda key: FakeWorkOS(key, existing={"pat.new@example.com": PAT}),
    )
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "pat.new@example.com", "node": "/racing/notes", "permission": "read"},
    )
    assert out["statusCode"] == 303
    assert out["headers"]["Location"] == f"/app/admin/people/{PAT}?added=1&invite=none"
    assert fake_workos.instances[-1].created == [] and fake_workos.instances[-1].invited == []
    assert {g.node for g in as_racing.grants_of(PAT)} == {"/private", "/racing/notes"}
    assert as_racing.get_profile(PAT).email == "pat@example.com"  # profile untouched


@pytest.mark.parametrize(
    "form",
    [
        {"email": "not-an-email", "node": "/racing", "permission": "read"},
        {"email": "a@b.co", "node": "racing", "permission": "read"},
        {"email": "a@b.co", "node": "/Racing", "permission": "read"},
        {"email": "a@b.co", "node": "/racing", "permission": "admin"},
    ],
)
def test_add_person_bad_input_is_400(
    as_root: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS], form: dict[str, str]
) -> None:
    assert post("/app/admin/people", cookie, form)["statusCode"] == 400
    assert fake_workos.instances == []


def test_add_person_without_workos_key_is_friendly(
    as_root: GrantAdmin, cookie: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web import app as web_app
    from app.web import secrets as secrets_mod

    real = secrets_mod.load("")
    monkeypatch.setattr(
        web_app.secrets_mod,
        "load",
        lambda arn, client=None: secrets_mod.WebSecrets(real.client_secret, "", real.session_key),
    )
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "x@example.com", "display_name": "X", "node": "/", "permission": "read"},
    )
    assert out["statusCode"] == 503
    assert "WorkOS API key not configured" in out["body"] and "docs/DEPLOY.md" in out["body"]
    assert as_root.list_profiles() == [p for p in as_root.list_profiles()]  # nothing added
    assert all(p.email != "x@example.com" for p in as_root.list_profiles())


def test_add_person_invitation_failure_still_shows_block_with_warning(
    as_root: GrantAdmin, cookie: str, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    monkeypatch.setattr(admin_view, "WorkOSClient", lambda key: FakeWorkOS(key, fail_invite=True))
    out = post(
        "/app/admin/people",
        cookie,
        {"email": "x@example.com", "display_name": "X", "node": "/", "permission": "read"},
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"].endswith("invite=failed")
    body = get("/app/admin/people/user_new01", cookie, {"added": "1", "invite": "failed"})["body"]
    assert "could not be sent" in body and "WorkOS dashboard" in body
    assert settings.canonical_mcp_url in body
    assert as_root.get_profile("user_new01") is not None


# --- status -----------------------------------------------------------------------------------


def test_status_change_by_non_root_is_403(as_racing: GrantAdmin, cookie: str) -> None:
    out = post(f"/app/admin/people/{DANA}/status", cookie, {"status": "disabled"})
    assert out["statusCode"] == 403
    assert as_racing.get_profile(DANA).status == "active"


def test_status_change_by_root_disables_and_keeps_grants(as_root: GrantAdmin, cookie: str) -> None:
    out = post(f"/app/admin/people/{DANA}/status", cookie, {"status": "disabled"})
    assert out["statusCode"] == 303
    assert as_root.get_profile(DANA).status == "disabled"
    assert len(as_root.grants_of(DANA)) == 1  # grants outlive (§4.10)
    body = get(f"/app/admin/people/{DANA}", cookie)["body"]
    assert "Disabled." in body and "Enable" in body and "grants below are kept" in body
    out = post(f"/app/admin/people/{DANA}/status", cookie, {"status": "active"})
    assert out["statusCode"] == 303 and as_root.get_profile(DANA).status == "active"


def test_status_bad_value_self_and_unknown(as_root: GrantAdmin, cookie: str) -> None:
    assert post(f"/app/admin/people/{DANA}/status", cookie, {"status": "gone"})["statusCode"] == 400
    assert (
        post(f"/app/admin/people/{SUBJECT}/status", cookie, {"status": "disabled"})["statusCode"]
        == 400
    )
    assert as_root.get_profile(SUBJECT).status == "active"
    assert (
        post("/app/admin/people/user_x/status", cookie, {"status": "disabled"})["statusCode"] == 404
    )


# --- grants ---------------------------------------------------------------------------------


def test_grants_page_root(as_root: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin/grants", cookie)["body"]
    assert "<code>/racing/setup</code>" in body and "<code>/private</code>" in body
    assert "Dana can write everything under /racing/setup" in body
    assert "Pat can read everything under /private" in body
    assert "Grant access" in body and REVOCATION in body
    assert "<strong>own</strong>" in body and "grant any of these" in body
    # The full directory, by display name only.
    for sub, name in ((JOSH, "Josh"), (DANA, "Dana"), (PAT, "Pat"), (SUBJECT, "Test Person")):
        assert f'<option value="{sub}">{name}</option>' in body
    assert "@example.com" not in body
    assert 'name="email"' in body


def test_grants_page_racing_owner_never_sees_private(as_racing: GrantAdmin, cookie: str) -> None:
    """The dropdown holds only people with a grant under /racing — Dana and the viewer.
    Pat (reads /private) and Josh (owns /) are absent by name, email and subject."""
    body = get("/app/admin/grants", cookie)["body"]
    assert "Dana can write everything under /racing/setup" in body
    assert f'<option value="{DANA}">Dana</option>' in body
    assert f'<option value="{SUBJECT}">Test Person</option>' in body
    assert body.count('<option value="user_') == 2
    for word in ("/private", "Pat", PAT, "pat@example.com", JOSH, "josh@example.com"):
        assert word not in body, word
    assert "@example.com" not in body
    assert 'name="email"' in body and "give their email" in body
    assert REVOCATION in body


def test_grant_by_email_brings_a_member_into_scope(as_racing: GrantAdmin, cookie: str) -> None:
    out = post(
        "/app/admin/grants",
        cookie,
        {"subject": "", "email": "PAT@example.com", "node": "/racing/notes", "permission": "read"},
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"] == "/app/admin/grants"
    rows = {g.node: g for g in as_racing.grants_of(PAT)}
    assert rows["/racing/notes"].permission is Permission.READ
    assert rows["/racing/notes"].granted_by == SUBJECT
    body = get("/app/admin/grants", cookie)["body"]
    assert "Pat can read everything under /racing/notes (granted by Test Person" in body
    assert f'<option value="{PAT}">Pat</option>' in body  # in scope now, so listed
    assert "/private" not in body and "pat@example.com" not in body


def test_grant_by_email_failure_is_one_sentence_whichever_the_reason(
    as_racing: GrantAdmin, cookie: str
) -> None:
    form = {"subject": "", "node": "/racing/notes", "permission": "read"}
    unknown = post("/app/admin/grants", cookie, {**form, "email": "nobody@example.com"})
    as_racing.set_status(PAT, "disabled")
    disabled = post("/app/admin/grants", cookie, {**form, "email": "pat@example.com"})
    assert unknown["statusCode"] == disabled["statusCode"] == 400
    assert admin_view.EMAIL_NOT_GRANTABLE in unknown["body"]
    assert unknown["body"] == disabled["body"]
    for word in ("Pat", PAT, "pat@example.com", "nobody@example.com", "disabled"):
        assert word not in disabled["body"], word
    assert [g.node for g in as_racing.grants_of(PAT)] == ["/private"]


def test_grant_by_email_outside_scope_is_403_before_any_lookup(
    as_racing: GrantAdmin, cookie: str
) -> None:
    form = {"subject": "", "node": "/private", "permission": "read"}
    known = post("/app/admin/grants", cookie, {**form, "email": "pat@example.com"})
    unknown = post("/app/admin/grants", cookie, {**form, "email": "nobody@example.com"})
    assert known["statusCode"] == unknown["statusCode"] == 403
    assert known["body"] == unknown["body"]
    assert [g.node for g in as_racing.grants_of(PAT)] == ["/private"]


def test_grant_needs_a_person_or_an_email(as_root: GrantAdmin, cookie: str) -> None:
    out = post("/app/admin/grants", cookie, {"subject": "", "node": "/", "permission": "read"})
    assert out["statusCode"] == 400 and "give their email" in out["body"]
    out = post(
        "/app/admin/grants",
        cookie,
        {"subject": "", "email": "not-an-email", "node": "/", "permission": "read"},
    )
    assert out["statusCode"] == 400 and "Give one email address" in out["body"]


def test_grant_post_inside_scope(as_racing: GrantAdmin, cookie: str) -> None:
    out = post(
        "/app/admin/grants",
        cookie,
        {"subject": PAT, "node": "/racing/notes/", "permission": "write"},
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"] == "/app/admin/grants"
    rows = {g.node: g for g in as_racing.grants_of(PAT)}
    assert rows["/racing/notes"].permission is Permission.WRITE
    assert rows["/racing/notes"].granted_by == SUBJECT
    body = get("/app/admin/grants", cookie)["body"]
    assert "Pat can write everything under /racing/notes (granted by Test Person" in body
    assert "/private" not in body


def test_grant_post_outside_scope_is_403(as_racing: GrantAdmin, cookie: str) -> None:
    out = post(
        "/app/admin/grants", cookie, {"subject": DANA, "node": "/private", "permission": "read"}
    )
    assert out["statusCode"] == 403
    assert all(g.node != "/private" for g in as_racing.grants_of(DANA))


@pytest.mark.parametrize("node", ["racing", "/racing/../x", "/Racing", "/racing/_hidden", ""])
def test_grant_post_bad_path_is_400_with_message(
    as_root: GrantAdmin, cookie: str, node: str
) -> None:
    out = post("/app/admin/grants", cookie, {"subject": DANA, "node": node, "permission": "read"})
    assert out["statusCode"] == 400
    assert "path" in out["body"]


def test_grant_post_unknown_person_is_400(as_root: GrantAdmin, cookie: str) -> None:
    out = post(
        "/app/admin/grants", cookie, {"subject": "user_x", "node": "/", "permission": "read"}
    )
    assert out["statusCode"] == 400


def test_grant_back_param_never_leaves_admin(as_root: GrantAdmin, cookie: str) -> None:
    out = post(
        "/app/admin/grants",
        cookie,
        {"subject": DANA, "node": "/x", "permission": "read", "back": "https://evil.example/"},
    )
    assert out["headers"]["Location"] == "/app/admin/grants"
    out = post(
        "/app/admin/grants/revoke",
        cookie,
        {"subject": DANA, "node": "/x", "back": f"/app/admin/people/{DANA}"},
    )
    assert out["headers"]["Location"] == f"/app/admin/people/{DANA}"


def test_revoke_inside_and_outside_scope(as_racing: GrantAdmin, cookie: str) -> None:
    out = post("/app/admin/grants/revoke", cookie, {"subject": PAT, "node": "/private"})
    assert out["statusCode"] == 403
    assert len(as_racing.grants_of(PAT)) == 1
    out = post("/app/admin/grants/revoke", cookie, {"subject": DANA, "node": "/racing/setup"})
    assert out["statusCode"] == 303
    assert as_racing.grants_of(DANA) == []


# --- unowned (§4.10) --------------------------------------------------------------------


def test_unowned_lists_grants_by_disabled_and_former_owners(
    as_root: GrantAdmin, cookie: str
) -> None:
    # Dana (write on /racing/setup) "granted" something she never owned: unowned.
    as_root.store.put_grant(Grant(PAT, "/racing/setup/x.md", Permission.READ, DANA, NOW))
    # Josh granted Dana; disable Josh → his grants surface.
    as_root.set_status(JOSH, "disabled")
    body = get("/app/admin/unowned", cookie)["body"]
    assert "Pat can read the article /racing/setup/x.md (granted by Dana" in body
    assert "Dana no longer owns /racing/setup/x.md" in body
    assert "Dana can write everything under /racing/setup (granted by Josh" in body
    assert "Josh is disabled" in body
    assert "Revoke" in body and REVOCATION in body
    # The viewer's own grant on / was made by Josh too — listed, not hidden.
    assert "Test Person owns everything" in body


def test_unowned_is_empty_when_every_granter_still_owns(as_root: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin/unowned", cookie)["body"]
    assert "Nothing to review" in body and "Revoke" not in body


def test_unowned_is_root_only(as_racing: GrantAdmin, cookie: str) -> None:
    assert get("/app/admin/unowned", cookie)["statusCode"] == 403


# --- audit (§12.7) -----------------------------------------------------------------------


class FakeAudit:
    calls: list[tuple[str, str, int]] = []
    result = AuditResult()

    def __init__(self, log_group: str) -> None:
        self.log_group = log_group

    def query(self, path: str, since_days: int = 30) -> AuditResult:
        FakeAudit.calls.append((self.log_group, path, since_days))
        return FakeAudit.result


@pytest.fixture
def fake_audit(monkeypatch: pytest.MonkeyPatch) -> type[FakeAudit]:
    FakeAudit.calls = []
    FakeAudit.result = AuditResult(
        rows=[
            AuditRow("2026-09-13 10:00:00.000", DANA, "update_article", "allow", "200", "r1"),
            AuditRow("2026-09-13 09:00:00.000", "user_stranger", "read_article", "deny", "403"),
        ]
    )
    monkeypatch.setattr(admin_view, "AuditQuery", FakeAudit)
    monkeypatch.setenv("MCP_LOG_GROUP", "/aws/lambda/wiki-dev-mcp")
    return FakeAudit


def test_audit_rows_with_display_names(
    as_racing: GrantAdmin, cookie: str, fake_audit: type[FakeAudit]
) -> None:
    body = get("/app/admin/audit", cookie, {"path": "/racing/setup/x.md"})["body"]
    assert fake_audit.calls == [("/aws/lambda/wiki-dev-mcp", "/racing/setup/x.md", 30)]
    assert "update_article" in body and "Dana" in body
    assert "user_stranger" in body and "deny" in body and "403" in body
    assert "tool-call log" in body and "reads and writes" in body


def test_audit_outside_scope_is_404_and_never_queried(
    as_racing: GrantAdmin, cookie: str, fake_audit: type[FakeAudit]
) -> None:
    assert get("/app/admin/audit", cookie, {"path": "/private/x.md"})["statusCode"] == 404
    assert fake_audit.calls == []


def test_audit_partial_and_empty_and_form(
    as_root: GrantAdmin, cookie: str, fake_audit: type[FakeAudit]
) -> None:
    fake_audit.result = AuditResult(rows=[], partial=True)
    body = get("/app/admin/audit", cookie, {"path": "/racing"})["body"]
    assert "had not finished" in body and "No tool calls touched" in body
    body = get("/app/admin/audit", cookie)["body"]
    assert "Look up" in body and fake_audit.calls == [("/aws/lambda/wiki-dev-mcp", "/racing", 30)]


def test_audit_unconfigured_says_so(
    as_root: GrantAdmin, cookie: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_LOG_GROUP", raising=False)
    body = get("/app/admin/audit", cookie, {"path": "/racing"})["body"]
    assert "not configured" in body and "MCP_LOG_GROUP" in body


def test_audit_query_failure_is_a_message_not_a_500(
    as_root: GrantAdmin, cookie: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError

    class Boom:
        def __init__(self, log_group: str) -> None:
            pass

        def query(self, path: str, since_days: int = 30) -> AuditResult:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "StartQuery")

    monkeypatch.setattr(admin_view, "AuditQuery", Boom)
    monkeypatch.setenv("MCP_LOG_GROUP", "g")
    out = get("/app/admin/audit", cookie, {"path": "/racing"})
    assert out["statusCode"] == 200 and "could not be queried" in out["body"]


# --- hard delete runbook (§5.2, §8.9) ------------------------------------------------------


def _seed_versions(bucket: Any, settings: Any, key: str) -> list[str]:
    ids = []
    for i in range(3):
        r = bucket.put_object(
            Bucket=settings.bucket,
            Key=key,
            Body=f"---\ntitle: x\nseq: {i + 1}\n---\nbody {i}\n".encode(),
            Metadata={"actor": f"human:{DANA}", "kind": "write"},
        )
        ids.append(r["VersionId"])
    return ids


def test_delete_runbook_lists_versions_and_deletes_nothing(
    as_root: GrantAdmin, cookie: str, bucket: Any, settings: Any
) -> None:
    from app.web import app as web_app

    ids = _seed_versions(bucket, settings, "a/racing/setup/x.md")
    out = get("/app/admin/delete/racing/setup/x.md", cookie)
    assert out["statusCode"] == 200
    body = out["body"]
    assert "deletes nothing" in body and "break-glass" in body
    assert "Object Lock" in body and "--bypass-governance-retention" in body
    assert "assume-role" in body and "BreakGlassRoleArn" in body
    for vid in ids:
        assert vid in body
    assert body.count("aws s3api delete-object") == 3
    assert f"--bucket {settings.bucket}" in body and "a/racing/setup/x.md" in body
    assert f"human:{DANA}" in body
    # every version is still there
    listed = bucket.list_object_versions(Bucket=settings.bucket, Prefix="a/racing/setup/x.md")
    assert len(listed["Versions"]) == 3
    # minted a READ credential for exactly the path
    minter = web_app._minter
    assert (SUBJECT, "read", "/racing/setup/x.md") in minter.minted


def test_delete_runbook_scope_and_missing(
    as_racing: GrantAdmin, cookie: str, bucket: Any, settings: Any
) -> None:
    _seed_versions(bucket, settings, "a/private/x.md")
    assert get("/app/admin/delete/private/x.md", cookie)["statusCode"] == 404
    assert get("/app/admin/delete/racing/nothing.md", cookie)["statusCode"] == 404
    assert get("/app/admin/delete/racing/folder", cookie)["statusCode"] == 400


def test_delete_runbook_owner_of_nothing(as_nobody: GrantAdmin, cookie: str) -> None:
    body = get("/app/admin/delete/racing/x.md", cookie)["body"]
    assert "You don't own any part of the tree" in body


# --- csrf on every POST -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "form"),
    [
        ("/app/admin/people", {"email": "a@b.co", "node": "/", "permission": "read"}),
        (f"/app/admin/people/{DANA}/status", {"status": "disabled"}),
        ("/app/admin/grants", {"subject": DANA, "node": "/", "permission": "read"}),
        ("/app/admin/grants/revoke", {"subject": DANA, "node": "/racing/setup"}),
    ],
)
def test_post_without_csrf_is_403(
    as_root: GrantAdmin, cookie: str, fake_workos: type[FakeWorkOS], path: str, form: dict[str, str]
) -> None:
    assert post(path, cookie, form, csrf=False)["statusCode"] == 403
    assert as_root.get_profile(DANA).status == "active"
    assert len(as_root.grants_of(DANA)) == 1
    assert fake_workos.instances == []


def test_admin_requires_login() -> None:
    out = call(event("GET", "/app/admin/grants"))
    assert out["statusCode"] == 303 and "/app/login" in out["headers"]["Location"]
