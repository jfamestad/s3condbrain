"""The web application: routing, authentication, templates (HANDOFF §11.6).

Three jobs, all human: sign in (AuthKit), read (browse and view articles with their
history), administer (people, grants, audit, the hard-delete runbook). Views live in
``app/web/views/`` and register themselves on the router; this module owns the
plumbing they share.

Route prefix is ``/app``. The MCP endpoint, the discovery documents and this app
share one API and one host, so the session cookie is ``__Host-`` scoped and the
authorizer on ``/mcp`` ignores it.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from typing import Any

from aws_lambda_powertools import Logger
from jinja2 import Environment, PackageLoader, select_autoescape

from app.auth.admin import GrantAdmin
from app.auth.credentials import CredentialMinter
from app.auth.grants import GrantStore
from app.config import Settings
from app.storage.articles import ArticleStore
from app.storage.listings import ListingIndex
from app.web import secrets as secrets_mod
from app.web import session as sess
from app.web.context import WebContext
from app.web.http import (
    HttpError,
    Request,
    Response,
    Router,
    clear_cookie,
    content_security_policy,
    html,
    redirect,
    set_cookie,
)
from app.web.oauth import AuthKitClient

logger = Logger(service="wiki-web")

PREFIX = "/app"
VIEW_MODULES = ("auth", "read", "admin", "move")

_env = Environment(
    loader=PackageLoader("app.web", "templates"),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Module singletons across warm invocations.
_settings: Settings | None = None
_router: Router | None = None
_grants: GrantStore | None = None
_admin: GrantAdmin | None = None
_minter: CredentialMinter | None = None


def _reset() -> None:
    global _settings, _router, _grants, _admin, _minter
    _settings = _router = _grants = _admin = _minter = None
    secrets_mod._reset()


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def build_context(request: Request, principal: sess.Principal | None) -> WebContext:
    global _grants, _admin, _minter
    s = settings()
    if _grants is None:
        _grants = GrantStore(s.grant_table)
    if _admin is None:
        _admin = GrantAdmin(s.grant_table)
    if _minter is None:
        _minter = CredentialMinter(
            s.storage_role_arn, s.bucket, s.kms_key_arn, s.credential_cache_seconds
        )
    return WebContext(
        settings=s,
        secrets=secrets_mod.load(s.web_secret_arn),
        grants=_grants,
        admin=_admin,
        minter=_minter,
        store=ArticleStore(s.bucket),
        listings=ListingIndex(s.bucket),
        principal=principal,
        request_id=request.request_id,
        log=logger,
    )


# --- templates ------------------------------------------------------------------


def render(template: str, ctx: WebContext, **vars: Any) -> str:
    tpl = _env.get_template(template)
    base = {
        "prefix": PREFIX,
        "principal": ctx.principal,
        "csrf": sess.csrf_token(ctx.secrets.session_key, ctx.principal) if ctx.principal else "",
        "request_id": ctx.request_id,
    }
    return tpl.render({**base, **vars})


def page(template: str, ctx: WebContext, status: int = 200, **vars: Any) -> Response:
    return html(render(template, ctx, **vars), status)


# --- authentication ---------------------------------------------------------------


ViewHandler = Callable[[Request, WebContext], Response]


def current_principal(request: Request) -> sess.Principal | None:
    s = settings()
    key = secrets_mod.load(s.web_secret_arn).session_key
    principal = sess.load_session(key, request.cookies.get(sess.SESSION_COOKIE))
    if principal is None:
        return None
    # Revocation is prospective (§12.9): a disabled profile is refused now, not at expiry.
    # The same read enforces "Sign out everywhere" (§11.6): a cookie issued under an
    # older session_epoch than the profile's is a logged-out cookie.
    profile = build_context(request, None).admin.get_profile(principal.subject)
    # Default deny (§3.3) holds per request, not only at login: no PROFILE row, a
    # disabled one, or a cookie from before the last "sign out everywhere" — all refused.
    if profile is None or profile.status == "disabled":
        return None
    if principal.epoch != profile.session_epoch:
        return None
    return principal


def view(fn: ViewHandler) -> Callable[[Request], Response]:
    """Wrap a view: build the context; require a session; on POST require CSRF."""

    @functools.wraps(fn)
    def wrapper(request: Request) -> Response:
        principal = current_principal(request)
        if principal is None:
            nxt = request.path if request.method == "GET" else PREFIX + "/"
            return redirect(f"{PREFIX}/login?next={nxt}")
        ctx = build_context(request, principal)
        if request.method == "POST":
            token = request.form().get("csrf")
            if not sess.csrf_valid(ctx.secrets.session_key, principal, token):
                raise HttpError(
                    403, "The form expired or was tampered with. Go back and try again."
                )
        return fn(request, ctx)

    return wrapper


def public(fn: ViewHandler) -> Callable[[Request], Response]:
    """A route that needs no session (login, callback, logout)."""

    @functools.wraps(fn)
    def wrapper(request: Request) -> Response:
        return fn(request, build_context(request, current_principal(request)))

    return wrapper


def authkit(ctx: WebContext) -> AuthKitClient:
    return AuthKitClient(ctx.settings, ctx.secrets.client_secret)


def login_start(request: Request, ctx: WebContext) -> Response:
    if ctx.principal is not None:
        return redirect(PREFIX + "/")
    url, payload = authkit(ctx).start()
    nxt = request.query.get("next", "")
    if nxt.startswith(PREFIX + "/"):  # same-app paths only; never an open redirect
        payload["next"] = nxt
    cookie = set_cookie(
        sess.OAUTH_COOKIE, sess.encode(ctx.secrets.session_key, payload), max_age=600
    )
    return redirect(url, cookies=[cookie])


def login_callback(request: Request, ctx: WebContext) -> Response:
    raw = request.cookies.get(sess.OAUTH_COOKIE)
    payload = sess.decode(ctx.secrets.session_key, raw or "")
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    if not payload or not code or not state:
        raise HttpError(400, "The login did not complete. Start again.")
    try:
        identity = authkit(ctx).finish(code, state, payload)
    except ValueError as e:
        logger.warning("login_failed", reason=str(e), request_id=request.request_id)
        raise HttpError(400, "The login could not be verified. Start again.") from None
    profile = ctx.admin.get_profile(identity.subject)
    if profile is None:
        # Default deny (§3.3): a WorkOS identity with no profile is not a member.
        logger.warning("login_unknown_subject", request_id=request.request_id)
        raise HttpError(403, "This account is not a member of this wiki. Ask an owner to add you.")
    if profile.status == "disabled":
        raise HttpError(403, "This account has been disabled.")
    if profile.status == "invited":
        ctx.admin.set_status(identity.subject, "active")
    cookie = set_cookie(
        sess.SESSION_COOKIE,
        sess.issue_session(
            ctx.secrets.session_key,
            identity.subject,
            identity.email or profile.email,
            profile.display_name or identity.display_name,
            ctx.settings.session_hours,
            epoch=profile.session_epoch,
        ),
        max_age=ctx.settings.session_hours * 3600,
    )
    nxt = payload.get("next")
    target = nxt if isinstance(nxt, str) and nxt.startswith(PREFIX + "/") else PREFIX + "/"
    return redirect(target, cookies=[cookie, clear_cookie(sess.OAUTH_COOKIE)])


def logout(request: Request, ctx: WebContext) -> Response:
    return redirect(PREFIX + "/login", cookies=[clear_cookie(sess.SESSION_COOKIE)])


def logout_all(request: Request, ctx: WebContext) -> Response:
    """ "Sign out everywhere" (§11.6): bump the profile's session_epoch so every cookie
    issued so far — this one and any copied one — is refused from the next request."""
    try:
        epoch = ctx.admin.bump_session_epoch(ctx.subject)
    except ValueError:
        epoch = None  # the profile is gone; there is nothing left to sign out of
    logger.info("logout_all", request_id=request.request_id, subject=ctx.subject, epoch=epoch)
    return redirect(PREFIX + "/login", cookies=[clear_cookie(sess.SESSION_COOKIE)])


def login_page(request: Request, ctx: WebContext) -> Response:
    if ctx.principal is not None:
        return redirect(PREFIX + "/")
    return page("login.html", ctx)


# --- router ---------------------------------------------------------------------------


def router() -> Router:
    """Build (once) the router with auth routes plus every view module."""
    global _router
    if _router is None:
        r = Router(PREFIX)
        r.add("GET", "/login", public(login_page))
        r.add("POST", "/login", public(login_start))
        r.add("GET", "/login/start", public(login_start))
        r.add("GET", "/callback", public(login_callback))
        r.add("POST", "/logout", view(logout))
        r.add("POST", "/logout-all", view(logout_all))
        for name in VIEW_MODULES:
            try:
                mod = importlib.import_module(f"app.web.views.{name}")
            except ModuleNotFoundError:
                continue
            mod.register(r)
        _router = r
    return _router


def error_page(request: Request, status: int, message: str) -> Response:
    try:
        ctx = build_context(request, current_principal(request))
        body = render("error.html", ctx, status=status, message=message)
    except Exception:  # noqa: BLE001 — never let the error page itself 500
        body = f"<h1>{status}</h1>"
    return html(body, status)


def handle(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point."""
    request = Request.from_event(event)
    try:
        response = router().dispatch(request)
    except HttpError as e:
        response = error_page(request, e.status, e.message)
    except Exception as exc:  # noqa: BLE001 — the boundary (§12.3): log, say nothing
        logger.exception(
            "web_unhandled", request_id=request.request_id, exc_class=type(exc).__name__
        )
        response = error_page(request, 500, "Something went wrong. It has been logged.")
    # The sign-in POST is answered with a redirect to AuthKit, and `form-action` is
    # checked against that target — so the authorization server has to be named here
    # or the browser drops the submission without a word (§4.9).
    response.headers.setdefault(
        "Content-Security-Policy", content_security_policy(settings().authkit_domain)
    )
    return response.to_event()


__all__ = [
    "PREFIX",
    "WebContext",
    "build_context",
    "handle",
    "page",
    "public",
    "render",
    "router",
    "view",
]
