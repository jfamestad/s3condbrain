"""Router, session, CSRF, OAuth exchange, login gate, headers, error boundary."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.web import app as web_app
from app.web import session as sess
from app.web.http import (
    HttpError,
    Request,
    Response,
    Router,
    content_security_policy,
    html,
    set_cookie,
)
from app.web.oauth import AuthKitClient
from app.web.render import render, sections
from tests.unit.web.conftest import SESSION_KEY, SUBJECT, call, csrf_for, event

# --- http ---------------------------------------------------------------------------


def _req(method: str, path: str) -> Request:
    return Request(method, path, {}, {}, {}, b"")


def test_router_captures_segments_and_paths() -> None:
    r = Router("/app")
    r.add("GET", "/a/{path:path}", lambda rq: html(rq.params["path"]))
    r.add("GET", "/people/{sub}", lambda rq: html(rq.params["sub"]))
    assert (
        r.dispatch(_req("GET", "/app/a/racing/setup/rear-bar.md")).body
        == "racing/setup/rear-bar.md"
    )
    assert r.dispatch(_req("GET", "/app/people/user_1")).body == "user_1"


def test_router_404_and_405() -> None:
    r = Router("/app")
    r.add("GET", "/x", lambda rq: html("x"))
    with pytest.raises(HttpError) as e404:
        r.dispatch(_req("GET", "/app/nope"))
    assert e404.value.status == 404
    with pytest.raises(HttpError) as e405:
        r.dispatch(_req("POST", "/app/x"))
    assert e405.value.status == 405


def test_host_cookie_forces_root_path_and_secure() -> None:
    c = set_cookie("__Host-wiki_session", "v", max_age=10, path="/app")
    assert "Path=/;" in c or c.endswith("Path=/") or "Path=/; " in c
    assert "Secure" in c and "HttpOnly" in c and "SameSite=Lax" in c


def test_response_carries_security_headers() -> None:
    out = Response(200, "x").to_event()
    h = out["headers"]
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h["X-Frame-Options"] == "DENY"
    assert h["Cache-Control"] == "no-store"


def test_csp_form_action_names_the_authorization_server() -> None:
    """The sign-in POST is answered with a redirect to AuthKit, and `form-action` is
    enforced against the redirect target. Omitting the authorization server makes the
    browser drop the submission with no request and no console error — the login
    button simply does nothing."""
    authkit = "https://example-staging.authkit.app"
    policy = content_security_policy(authkit)
    assert f"form-action 'self' {authkit};" in policy
    # Everything else stays exactly as strict as before.
    assert "default-src 'self';" in policy
    assert "frame-ancestors 'none';" in policy
    assert "base-uri 'none'" in policy
    # The bare policy is still self-only, so nothing widens by accident.
    assert "form-action 'self';" in content_security_policy()


# --- session ----------------------------------------------------------------------------


def test_session_round_trip_and_tamper() -> None:
    tok = sess.issue_session(SESSION_KEY, SUBJECT, "e@x", "Name", 1)
    p = sess.load_session(SESSION_KEY, tok)
    assert p is not None and p.subject == SUBJECT
    payload, sig = tok.rsplit(".", 1)
    assert sess.load_session(SESSION_KEY, payload + "." + sig[::-1]) is None
    assert sess.load_session(b"other-key-" * 4, tok) is None
    assert sess.load_session(SESSION_KEY, "") is None


def test_session_expiry() -> None:
    tok = sess.encode(SESSION_KEY, {"sub": "s", "exp": int(time.time()) - 1})
    assert sess.load_session(SESSION_KEY, tok) is None


def test_session_carries_the_epoch_and_older_cookies_read_as_zero() -> None:
    tok = sess.issue_session(SESSION_KEY, SUBJECT, "e@x", "Name", 1, epoch=3)
    p = sess.load_session(SESSION_KEY, tok)
    assert p is not None and p.epoch == 3
    payload = sess.decode(SESSION_KEY, tok)
    assert payload is not None and payload["epoch"] == 3
    # Issued before epochs existed, or with a mangled one: epoch 0, which only a
    # profile that has never been bumped accepts.
    exp = int(time.time()) + 60
    legacy = sess.load_session(SESSION_KEY, sess.encode(SESSION_KEY, {"sub": SUBJECT, "exp": exp}))
    assert legacy is not None and legacy.epoch == 0
    for odd in ("7", 7.5, True, None):
        mangled = sess.encode(SESSION_KEY, {"sub": SUBJECT, "exp": exp, "epoch": odd})
        loaded = sess.load_session(SESSION_KEY, mangled)
        assert loaded is not None and loaded.epoch == 0, odd


def test_csrf_bound_to_session() -> None:
    a = sess.issue_session(SESSION_KEY, SUBJECT, "e", "n", 1)
    b = sess.issue_session(SESSION_KEY, "user_other", "e", "n", 1)
    pa = sess.load_session(SESSION_KEY, a)
    pb = sess.load_session(SESSION_KEY, b)
    assert pa and pb
    assert sess.csrf_valid(SESSION_KEY, pa, sess.csrf_token(SESSION_KEY, pa))
    assert not sess.csrf_valid(SESSION_KEY, pa, sess.csrf_token(SESSION_KEY, pb))
    assert not sess.csrf_valid(SESSION_KEY, pa, None)


# --- oauth -------------------------------------------------------------------------------


@pytest.fixture
def rsa_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Jwks:
    def __init__(self, key: Any) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        class K:
            key = self._key.public_key()

        return K()


def _id_token(key: Any, settings: Settings, nonce: str | None = None, **over: Any) -> str:
    """An ID token as AuthKit would mint it. ``nonce=None`` leaves the claim out."""
    now = int(time.time())
    claims = {
        "iss": settings.authkit_domain,
        "aud": settings.workos_client_id,
        "sub": SUBJECT,
        "email": "test@example.com",
        "name": "Test Person",
        "iat": now,
        "exp": now + 300,
        **({"nonce": nonce} if nonce is not None else {}),
        **over,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def _client(
    settings: Settings, key: Any, token_body: dict[str, Any], status: int = 200
) -> AuthKitClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth2/token"
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert form["client_secret"] == "shh"
        assert "code_verifier" in form
        return httpx.Response(status, json=token_body)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return AuthKitClient(settings, "shh", http=http, jwks_client=_Jwks(key))


def test_oauth_start_and_finish(settings: Settings, rsa_key: Any) -> None:
    s = Settings.from_env()
    body: dict[str, Any] = {}  # the token is minted once the nonce is known
    c = _client(s, rsa_key, body)
    url, payload = c.start()
    assert "code_challenge_method=S256" in url and "resource=" not in url
    assert f"nonce={payload['nonce']}" in url and f"state={payload['state']}" in url
    assert payload["nonce"] != payload["state"]
    assert c.redirect_uri == "https://wiki-dev.famestad.com/app/callback"
    body["id_token"] = _id_token(rsa_key, s, nonce=str(payload["nonce"]))
    ident = c.finish("code123", str(payload["state"]), payload)
    assert ident.subject == SUBJECT and ident.email == "test@example.com"


def test_oauth_rejects_wrong_missing_or_unrecorded_nonce(rsa_key: Any) -> None:
    s = Settings.from_env()
    body: dict[str, Any] = {}
    c = _client(s, rsa_key, body)
    _, payload = c.start()
    state = str(payload["state"])
    body["id_token"] = _id_token(rsa_key, s, nonce="minted-for-some-other-login")
    with pytest.raises(ValueError, match="nonce"):
        c.finish("c", state, payload)
    body["id_token"] = _id_token(rsa_key, s)  # no nonce claim at all
    with pytest.raises(ValueError, match="nonce"):
        c.finish("c", state, payload)
    # A cookie from before nonces existed is refused before the code is exchanged.
    stale = {k: v for k, v in payload.items() if k != "nonce"}
    body["id_token"] = _id_token(rsa_key, s, nonce=str(payload["nonce"]))
    with pytest.raises(ValueError, match="incomplete"):
        _client(s, rsa_key, body, status=500).finish("c", state, stale)


def test_oauth_rejects_state_mismatch_bad_aud_and_none_alg(rsa_key: Any) -> None:
    s = Settings.from_env()
    ok = _client(s, rsa_key, {})
    _, payload = ok.start()
    nonce = str(payload["nonce"])
    with pytest.raises(ValueError):
        ok.finish("c", "wrong-state", payload)
    with pytest.raises(ValueError):  # non-ASCII is a mismatch, not a TypeError
        ok.finish("c", "wrong-\u00e9tat", payload)
    bad_aud = _client(
        s, rsa_key, {"id_token": _id_token(rsa_key, s, nonce=nonce, aud="someone-else")}
    )
    with pytest.raises(ValueError):
        bad_aud.finish("c", str(payload["state"]), payload)
    none_tok = jwt.encode(
        {
            "sub": SUBJECT,
            "iss": s.authkit_domain,
            "aud": s.workos_client_id,
            "nonce": nonce,
            "exp": int(time.time()) + 60,
        },
        None,
        algorithm="none",
    )  # type: ignore[arg-type]
    none_alg = _client(s, rsa_key, {"id_token": none_tok})
    with pytest.raises(ValueError):
        none_alg.finish("c", str(payload["state"]), payload)
    refused = _client(s, rsa_key, {"error": "invalid_grant"}, status=400)
    with pytest.raises(ValueError):
        refused.finish("c", str(payload["state"]), payload)


# --- app: login gate, callback, csrf, errors ---------------------------------------------


def test_unauthenticated_view_redirects_to_login() -> None:
    out = call(event("GET", "/app/"))
    assert out["statusCode"] == 303
    assert out["headers"]["Location"].startswith("/app/login?next=/app/")


def test_login_page_and_start_sets_oauth_cookie() -> None:
    page = call(event("GET", "/app/login"))
    assert page["statusCode"] == 200 and "Sign in" in page["body"]
    start = call(event("POST", "/app/login", form={}))
    assert start["statusCode"] == 303
    assert (
        start["headers"]["Location"].startswith("https://spirited")
        or "/oauth2/authorize?" in start["headers"]["Location"]
    )
    assert "nonce=" in start["headers"]["Location"]
    cookie = start["multiValueHeaders"]["Set-Cookie"][0]
    assert cookie.startswith("__Host-wiki_oauth=") and "Max-Age=600" in cookie


def test_next_param_never_open_redirect() -> None:
    start = call(event("GET", "/app/login/start", query={"next": "https://evil.example/"}))
    raw = start["multiValueHeaders"]["Set-Cookie"][0].split(";")[0].split("=", 1)[1]
    payload = sess.decode(SESSION_KEY, raw)
    assert payload is not None and "next" not in payload


def test_callback_unknown_subject_is_403(monkeypatch: pytest.MonkeyPatch, rsa_key: Any) -> None:
    s = Settings.from_env()
    body: dict[str, Any] = {}
    fake = _client(s, rsa_key, body)
    monkeypatch.setattr(web_app, "authkit", lambda ctx: fake)
    _, payload = fake.start()
    body["id_token"] = _id_token(rsa_key, s, nonce=str(payload["nonce"]), sub="user_stranger")
    cookie = sess.encode(SESSION_KEY, payload)
    out = call(
        event(
            "GET",
            "/app/callback",
            query={"code": "c", "state": str(payload["state"])},
            cookies={sess.OAUTH_COOKIE: cookie},
        )
    )
    assert out["statusCode"] == 403 and "not a member" in out["body"]


def test_callback_success_sets_session_and_activates_invited(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any, admin: Any
) -> None:
    admin.create_profile(SUBJECT, "test@example.com", "Test Person", "invited")
    s = Settings.from_env()
    body: dict[str, Any] = {}
    fake = _client(s, rsa_key, body)
    monkeypatch.setattr(web_app, "authkit", lambda ctx: fake)
    _, payload = fake.start()
    body["id_token"] = _id_token(rsa_key, s, nonce=str(payload["nonce"]))
    payload["next"] = "/app/a/racing"
    cookie = sess.encode(SESSION_KEY, payload)
    out = call(
        event(
            "GET",
            "/app/callback",
            query={"code": "c", "state": str(payload["state"])},
            cookies={sess.OAUTH_COOKIE: cookie},
        )
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"] == "/app/a/racing"
    set_cookies = out["multiValueHeaders"]["Set-Cookie"]
    assert any(c.startswith("__Host-wiki_session=") for c in set_cookies)
    assert any(c.startswith("__Host-wiki_oauth=;") and "Max-Age=0" in c for c in set_cookies)
    assert admin.get_profile(SUBJECT).status == "active"


def test_callback_issues_the_cookie_under_the_profile_epoch(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any, admin: Any
) -> None:
    """After a "Sign out everywhere", a fresh login must carry the new epoch, or it
    would be refused on its own next request."""
    admin.create_profile(SUBJECT, "test@example.com", "Test Person", "active")
    assert admin.bump_session_epoch(SUBJECT) == 1
    s = Settings.from_env()
    body: dict[str, Any] = {}
    fake = _client(s, rsa_key, body)
    monkeypatch.setattr(web_app, "authkit", lambda ctx: fake)
    _, payload = fake.start()
    body["id_token"] = _id_token(rsa_key, s, nonce=str(payload["nonce"]))
    out = call(
        event(
            "GET",
            "/app/callback",
            query={"code": "c", "state": str(payload["state"])},
            cookies={sess.OAUTH_COOKIE: sess.encode(SESSION_KEY, payload)},
        )
    )
    assert out["statusCode"] == 303
    raw = next(
        c for c in out["multiValueHeaders"]["Set-Cookie"] if c.startswith("__Host-wiki_session=")
    )
    issued = sess.load_session(SESSION_KEY, raw.split(";", 1)[0].split("=", 1)[1])
    assert issued is not None and issued.epoch == 1
    assert (
        call(
            event(
                "GET",
                "/app/admin",
                cookies={sess.SESSION_COOKIE: raw.split(";", 1)[0].split("=", 1)[1]},
            )
        )["statusCode"]
        == 200
    )


def test_callback_with_nonce_for_another_login_is_400(
    monkeypatch: pytest.MonkeyPatch, rsa_key: Any, admin: Any
) -> None:
    admin.create_profile(SUBJECT, "test@example.com", "Test Person", "active")
    s = Settings.from_env()
    fake = _client(s, rsa_key, {"id_token": _id_token(rsa_key, s, nonce="someone-elses")})
    monkeypatch.setattr(web_app, "authkit", lambda ctx: fake)
    _, payload = fake.start()
    cookie = sess.encode(SESSION_KEY, payload)
    out = call(
        event(
            "GET",
            "/app/callback",
            query={"code": "c", "state": str(payload["state"])},
            cookies={sess.OAUTH_COOKIE: cookie},
        )
    )
    assert out["statusCode"] == 400 and "could not be verified" in out["body"]
    assert not any(
        c.startswith("__Host-wiki_session=")
        for c in out.get("multiValueHeaders", {}).get("Set-Cookie", [])
    )


def test_post_without_csrf_is_403(session_cookie: str) -> None:
    out = call(event("POST", "/app/logout", cookies={sess.SESSION_COOKIE: session_cookie}, form={}))
    assert out["statusCode"] == 403


def test_logout_with_csrf_clears_cookie(session_cookie: str) -> None:
    out = call(
        event(
            "POST",
            "/app/logout",
            cookies={sess.SESSION_COOKIE: session_cookie},
            form={"csrf": csrf_for(session_cookie)},
        )
    )
    assert out["statusCode"] == 303
    assert any("Max-Age=0" in c for c in out["multiValueHeaders"]["Set-Cookie"])


def test_disabled_profile_is_logged_out(session_cookie: str, admin: Any) -> None:
    admin.set_status(SUBJECT, "disabled")
    out = call(event("GET", "/app/", cookies={sess.SESSION_COOKIE: session_cookie}))
    assert out["statusCode"] == 303 and "/app/login" in out["headers"]["Location"]


# --- sign out everywhere (§11.6 session epochs) ---------------------------------------------


def _get_admin(cookie: str) -> dict[str, Any]:
    return call(event("GET", "/app/admin", cookies={sess.SESSION_COOKIE: cookie}))


def test_cookie_behind_the_profile_epoch_is_logged_out(session_cookie: str, admin: Any) -> None:
    assert _get_admin(session_cookie)["statusCode"] == 200  # issued at epoch 0, profile at 0
    assert admin.bump_session_epoch(SUBJECT) == 1
    out = _get_admin(session_cookie)
    assert out["statusCode"] == 303 and out["headers"]["Location"].startswith("/app/login")
    # A POST with a valid CSRF token is no better: the session itself is gone.
    out = call(
        event(
            "POST",
            "/app/logout",
            cookies={sess.SESSION_COOKIE: session_cookie},
            form={"csrf": csrf_for(session_cookie)},
        )
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"].startswith("/app/login")


def test_logout_all_bumps_the_epoch_and_kills_every_older_cookie(
    session_cookie: str, admin: Any
) -> None:
    copied = session_cookie  # the same cookie in another browser
    out = call(
        event(
            "POST",
            "/app/logout-all",
            cookies={sess.SESSION_COOKIE: session_cookie},
            form={"csrf": csrf_for(session_cookie)},
        )
    )
    assert out["statusCode"] == 303 and out["headers"]["Location"] == "/app/login"
    assert any("Max-Age=0" in c for c in out["multiValueHeaders"]["Set-Cookie"])
    assert admin.get_profile(SUBJECT).session_epoch == 1
    assert _get_admin(copied)["statusCode"] == 303
    fresh = sess.issue_session(SESSION_KEY, SUBJECT, "test@example.com", "Test Person", 12, epoch=1)
    assert _get_admin(fresh)["statusCode"] == 200
    stale = sess.issue_session(SESSION_KEY, SUBJECT, "test@example.com", "Test Person", 12, epoch=2)
    assert _get_admin(stale)["statusCode"] == 303  # ahead is as wrong as behind


def test_logout_all_needs_csrf_and_bumps_nothing_without_it(
    session_cookie: str, admin: Any
) -> None:
    out = call(
        event("POST", "/app/logout-all", cookies={sess.SESSION_COOKIE: session_cookie}, form={})
    )
    assert out["statusCode"] == 403
    assert admin.get_profile(SUBJECT).session_epoch == 0
    assert _get_admin(session_cookie)["statusCode"] == 200


def test_ordinary_logout_does_not_bump_the_epoch(session_cookie: str, admin: Any) -> None:
    out = call(
        event(
            "POST",
            "/app/logout",
            cookies={sess.SESSION_COOKIE: session_cookie},
            form={"csrf": csrf_for(session_cookie)},
        )
    )
    assert out["statusCode"] == 303
    assert admin.get_profile(SUBJECT).session_epoch == 0
    # Server-side the cookie is still good; only this browser dropped it. That is the
    # difference "Sign out everywhere" exists to make.
    assert _get_admin(session_cookie)["statusCode"] == 200


def test_every_page_offers_both_sign_out_buttons(session_cookie: str) -> None:
    body = _get_admin(session_cookie)["body"]
    assert 'action="/app/logout"' in body and 'action="/app/logout-all"' in body
    assert "Sign out everywhere" in body
    assert body.count('name="csrf"') >= 2


def test_unknown_route_is_404_page_and_unhandled_is_500_without_details(
    monkeypatch: pytest.MonkeyPatch, session_cookie: str
) -> None:
    out = call(event("GET", "/app/definitely-not-a-route"))
    assert out["statusCode"] == 404
    r = web_app.router()

    def boom(rq: Request) -> Response:
        raise RuntimeError("secret detail 12345")

    r.add("GET", "/boom", boom)
    out = call(event("GET", "/app/boom", cookies={sess.SESSION_COOKIE: session_cookie}))
    assert out["statusCode"] == 500
    assert "12345" not in out["body"] and "Traceback" not in out["body"]


# --- render ------------------------------------------------------------------------------------


def test_render_disables_html_and_rewrites_internal_links() -> None:
    out = render(
        "<script>x</script>\n\nSee [spec](/a/racing/setup/rear-bar.md) and [ext](https://x.y/)."
    )
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert 'href="/app/a/racing/setup/rear-bar.md"' in out
    assert 'rel="noopener noreferrer"' in out
    assert sections("# A\n\ntext\n\n## B c\n") == [(1, "A"), (2, "B c")]


def test_sections_ignore_headings_inside_code_fences() -> None:
    md = "# Real\n\n```\n# not a heading\n```\n\n~~~\n## nor this\n~~~\n\n## Also real\n"
    assert sections(md) == [(1, "Real"), (2, "Also real")]


def test_cookie_without_profile_row_is_not_logged_in(session_cookie: str, admin: Any) -> None:
    """Default deny per request (§3.3): a valid cookie whose PROFILE row is gone is a
    logged-out cookie, exactly like a disabled one."""
    admin.table.delete_item(Key={"pk": f"U#{SUBJECT}", "sk": "PROFILE"})
    out = call(event("GET", "/app/", cookies={sess.SESSION_COOKIE: session_cookie}))
    assert out["statusCode"] == 303 and "/app/login" in out["headers"]["Location"]
