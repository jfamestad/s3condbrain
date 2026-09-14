"""The admin console (HANDOFF §11.6 "Administration", increments E and F).

Everything §4.7 keeps off the tool surface lives here, behind an interactive human
session: people, grants, revocations, the unowned-grant review, the per-path audit
view and the hard-delete runbook. **Nothing here is an MCP tool** and nothing here
runs under the MCP function's role — the web role is the only one that can write a
grant (§8.8).

Scope (§11.6, §4.10): every page is scoped to ``GrantAdmin.owned_roots(viewer)``.
An admin is simply the owner of ``/`` (§4.5); an owner of ``/racing`` sees grants
at or below ``/racing``, grants only there, and sees people only insofar as they
hold grants there. Someone who owns nothing sees a sentence and no data. The owner
guard itself is enforced in ``GrantAdmin`` — this module only decides what to show.

That scoping extends to the directory itself: a subtree owner is never shown, and
never told, who else has an account. The grant form lists only people already in
their scope, by display name; anyone else is reached by typing an email. "Add a
person" with an email that already has an account simply grants to it and lands on
their page, as for a new account, so the answer is the same either way; a lookup
that cannot be acted on gets one fixed sentence whatever the reason
(``EMAIL_NOT_GRANTABLE``, ``EMAIL_NOT_ADDABLE``).

Grants read as sentences, not ACL rows: *"Dana can write everything under
/racing/setup (granted by Josh, 2026-09-13)"*.

Cost note: the table holds tens of rows (§1.3). The "grants in scope" list is built
from ``list_profiles()`` (one GSI query) plus ``grants_of`` per profile (one query
each) filtered by owned-root prefix — there is no prefix index over nodes and none is
worth adding at this size. The unowned review runs ``grants_by_granter`` (a Scan)
per profile; it is a root-owner page and a rare one.

A grant whose subject has no PROFILE row is invisible here: nobody without a
profile can sign in (default deny, §3.3), and the bootstrap script writes the first
owner's grant without one — create that profile before expecting the console to
list it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.auth.admin import NotAnOwner, Profile
from app.auth.credentials import Shape
from app.auth.grants import ancestors
from app.auth.types import Grant, Permission
from app.mcp.tools._common import ARTICLE_PATH_PATTERN, FOLDER_PATH_PATTERN, PATH_MAX_LENGTH
from app.storage.articles import AccessDenied, StoredObject, key_for
from app.web.app import PREFIX, page, view
from app.web.audit import AuditQuery, AuditResult
from app.web.context import WebContext
from app.web.http import HttpError, Request, Response, Router, redirect
from app.web.workos import WorkOSClient, WorkOSError

AUDIT_DAYS = 30
LOG_GROUP_ENV = "MCP_LOG_GROUP"  # set by the compute stack; "" → audit page says so
PROCESS_ACTOR_PREFIX = "process:"
VERSIONS_PAGE = 100

_ARTICLE_RE = re.compile(ARTICLE_PATH_PATTERN)
_FOLDER_RE = re.compile(FOLDER_PATH_PATTERN)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Said wherever a grant is made or shown (§12.9). Templates carry the same words.
REVOCATION_SENTENCE = (
    "Revoking access stops future reads. It does not remove anything already in "
    "someone's conversation history."
)

# Said whenever an email typed into the console cannot be acted on, whatever the
# reason, so the answer never says which reason — and so never confirms that an
# address has an account elsewhere in the tree. Constants because the tests assert
# the sentence is identical across reasons and across viewers.
EMAIL_NOT_GRANTABLE = "No account with that email can be granted here."
EMAIL_NOT_ADDABLE = (
    "No account with that email can be added here; if they already have an account, "
    "grant them access from the Grants page by email."
)


# --- scope -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What the viewer administers: the minimal set of nodes they own (§11.6)."""

    roots: tuple[str, ...]

    @property
    def is_root(self) -> bool:
        return "/" in self.roots

    @property
    def owns_anything(self) -> bool:
        return bool(self.roots)

    def covers(self, node: str) -> bool:
        """True when ``node`` is at or below one of the owned roots."""
        try:
            chain = ancestors(node)
        except ValueError:
            return False
        return any(root in chain for root in self.roots)


def _scope(ctx: WebContext) -> Scope:
    return Scope(tuple(ctx.admin.owned_roots(ctx.subject)))


def _require_root(scope: Scope, what: str) -> None:
    if not scope.is_root:
        raise HttpError(403, f"Only an owner of / can {what}.")


# --- people and names ---------------------------------------------------------------


def _profiles(ctx: WebContext) -> dict[str, Profile]:
    return {p.subject: p for p in ctx.admin.list_profiles()}


def _profile_by_email(ctx: WebContext, email: str) -> Profile | None:
    """The profile whose email matches, case-insensitively; one GSI query."""
    wanted = email.lower()
    for p in ctx.admin.list_profiles():
        if p.email.lower() == wanted:
            return p
    return None


def _grant_existing(
    ctx: WebContext, profile: Profile, node: str, permission: Permission
) -> Response:
    """ "Add a person" whose email already has a profile: grant and land on their
    page, exactly as a new account would (§15.2 step 1). No WorkOS call, no
    invitation — they can already sign in. From the owner's side the two paths
    answer alike, so the form never says whether an address was known here; the
    person is then legitimately in their scope (§11.6). The one refusal is a
    disabled account, and it is the same sentence for every owner.
    """
    if profile.status == "disabled":
        raise HttpError(400, EMAIL_NOT_ADDABLE)
    ctx.admin.grant(ctx.subject, profile.subject, node, permission)
    ctx.log.info(
        "person_added",
        request_id=ctx.request_id,
        subject=profile.subject,
        by=ctx.subject,
        existing=True,
    )
    return redirect(f"{PREFIX}/admin/people/{profile.subject}?added=1&invite=none")


def _names(profiles: dict[str, Profile]) -> dict[str, str]:
    return {sub: p.display_name or p.email or sub for sub, p in profiles.items()}


def _who(names: dict[str, str], subject: str) -> str:
    if subject in names:
        return names[subject]
    if subject.startswith(PROCESS_ACTOR_PREFIX):
        return "the install"
    return subject


def _date(iso: str) -> str:
    return iso[:10] if iso else "date unknown"


def sentence(grant: Grant, names: dict[str, str]) -> str:
    """One grant as a sentence (§11.6)."""
    who = _who(names, grant.subject)
    if grant.node == "/":
        what = "everything"
    elif grant.is_article:
        what = f"the article {grant.node}"
    else:
        what = f"everything under {grant.node}"
    if grant.permission is Permission.OWN:
        beneath = "it" if grant.is_article else "anything beneath it"
        head = f"{who} owns {what} and can grant access to {beneath}"
    elif grant.permission is Permission.WRITE:
        head = f"{who} can write {what}"
    else:
        head = f"{who} can read {what}"
    return f"{head} (granted by {_who(names, grant.granted_by)}, {_date(grant.granted_at)})"


def _grant_view(grant: Grant, names: dict[str, str]) -> dict[str, Any]:
    return {"grant": grant, "text": sentence(grant, names)}


def _grants_in_scope(
    ctx: WebContext, scope: Scope, profiles: dict[str, Profile]
) -> dict[str, list[Grant]]:
    """``{subject: [grants under an owned root]}`` for every profile holding one.

    One ``grants_of`` query per profile — see the module note on cost.
    """
    out: dict[str, list[Grant]] = {}
    for sub in profiles:
        mine = [g for g in ctx.admin.grants_of(sub) if scope.covers(g.node)]
        if mine:
            out[sub] = sorted(mine, key=lambda g: (g.node, g.subject))
    return out


# --- validation ---------------------------------------------------------------------


def _node_arg(raw: str) -> str:
    """An absolute folder or article path in the §10.2 grammar, trailing slash dropped.

    Raises:
        HttpError: 400 with a message a person can act on.
    """
    value = (raw or "").strip()
    if not value or len(value) > PATH_MAX_LENGTH:
        raise HttpError(400, "Give an absolute path such as /racing or /racing/setup.md.")
    try:
        node = ancestors(value)[-1]
    except ValueError:
        raise HttpError(
            400, f"{value!r} is not an absolute path. Start with / and avoid '..'."
        ) from None
    if not (_FOLDER_RE.match(node) or _ARTICLE_RE.match(node)):
        raise HttpError(
            400,
            f"{node!r} is not a wiki path: lowercase letters, digits, '.', '_' and '-', "
            "folders like /racing/setup or articles like /racing/setup/rear-bar.md.",
        )
    return node


def _article_arg(raw: str) -> str:
    node = _node_arg(raw)
    if not _ARTICLE_RE.match(node):
        raise HttpError(400, "Give an article path ending in .md.")
    return node


def _permission_arg(raw: str) -> Permission:
    try:
        return Permission((raw or "").strip().lower())
    except ValueError:
        raise HttpError(400, "Permission must be read, write or own.") from None


def _email_arg(raw: str) -> str:
    value = (raw or "").strip()
    if not _EMAIL_RE.match(value) or len(value) > 254:
        raise HttpError(400, "Give one email address.")
    return value


def _back_arg(raw: str, default: str) -> str:
    """A same-app admin path to return to after a POST; never an open redirect."""
    value = raw or ""
    return value if value.startswith(PREFIX + "/admin") else default


def _split_name(display_name: str) -> tuple[str, str]:
    first, _, last = display_name.strip().partition(" ")
    return first, last.strip()


def _workos(ctx: WebContext) -> WorkOSClient:
    try:
        return WorkOSClient(ctx.secrets.api_key)
    except RuntimeError as exc:
        raise HttpError(503, str(exc)) from None


def _connector(ctx: WebContext, profile: Profile) -> str:
    """The block an owner copies and sends on (§15.2 step 2, §15.3)."""
    return (
        f"Connector URL: {ctx.settings.canonical_mcp_url}\n"
        "\n"
        "In Claude, on web or desktop (this step cannot start on a phone; reading "
        "on a phone works afterwards):\n"
        "Claude → Settings → Connectors → Add custom connector → paste the URL → "
        f"Connect → sign in with {profile.email} → Allow.\n"
        "\n"
        "A Free plan holds one custom connector, so adding this one replaces any other."
    )


# --- overview -------------------------------------------------------------------------


def index(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    profiles = _profiles(ctx)
    in_scope = _grants_in_scope(ctx, scope, profiles)
    return page(
        "admin/index.html",
        ctx,
        scope=scope,
        people=len(profiles) if scope.is_root else len(in_scope),
        grants=sum(len(v) for v in in_scope.values()),
    )


# --- people -------------------------------------------------------------------------


def people(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    profiles = _profiles(ctx)
    names = _names(profiles)
    in_scope = _grants_in_scope(ctx, scope, profiles)
    # Root owners see every profile, grants or not; subtree owners only the people
    # who hold a grant under one of their roots (§11.6).
    visible = profiles if scope.is_root else {s: profiles[s] for s in in_scope}
    rows = [
        {
            "profile": p,
            "grants": [_grant_view(g, names) for g in in_scope.get(sub, [])],
        }
        for sub, p in sorted(visible.items(), key=lambda kv: names[kv[0]].lower())
    ]
    return page(
        "admin/people.html",
        ctx,
        scope=scope,
        rows=rows,
        permissions=[p.value for p in Permission],
        revocation=REVOCATION_SENTENCE,
    )


def add_person(request: Request, ctx: WebContext) -> Response:
    """§15.2 step 1: create at WorkOS, record the profile and the first grant, have
    WorkOS send the sign-in email. Then show the connector block to send on."""
    form = request.form()
    email = _email_arg(form.get("email", ""))
    display_name = (form.get("display_name") or "").strip()[:120] or email.split("@", 1)[0]
    node = _node_arg(form.get("node", ""))
    permission = _permission_arg(form.get("permission", ""))
    # The owner guard runs before anything touches WorkOS: a refused grant must not
    # leave a stray user behind.
    try:
        ctx.admin.assert_owner(ctx.subject, node)
    except NotAnOwner:
        raise HttpError(403, f"You do not own {node}, so you cannot grant there.") from None
    existing = _profile_by_email(ctx, email)
    if existing is not None:
        return _grant_existing(ctx, existing, node, permission)
    workos = _workos(ctx)
    try:
        subject = workos.find_user_by_email(email)
        if subject is None:
            first, last = _split_name(display_name)
            subject = workos.create_user(email, first, last)
    except WorkOSError as exc:
        ctx.log.warning(
            "workos_create_user_failed",
            request_id=ctx.request_id,
            status=exc.status,
            code=exc.code,
        )
        raise HttpError(
            502, f"WorkOS could not create the user ({exc.code}). Nothing was changed."
        ) from None
    try:
        ctx.admin.create_profile(subject, email, display_name, "invited")
    except ValueError:
        # The WorkOS user already has a profile under this subject (their email
        # changed upstream). Same answer as an email we knew.
        held = ctx.admin.get_profile(subject)
        if held is None:  # a profile that vanished between the two calls
            raise HttpError(400, EMAIL_NOT_ADDABLE) from None
        return _grant_existing(ctx, held, node, permission)
    ctx.admin.grant(ctx.subject, subject, node, permission)
    invite = "sent"
    try:
        workos.send_invitation(email)
    except WorkOSError as exc:
        invite = "failed"
        ctx.log.warning(
            "workos_invitation_failed",
            request_id=ctx.request_id,
            subject=subject,
            status=exc.status,
            code=exc.code,
        )
    ctx.log.info("person_added", request_id=ctx.request_id, subject=subject, by=ctx.subject)
    return redirect(f"{PREFIX}/admin/people/{subject}?added=1&invite={invite}")


def person(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    subject = request.params["sub"]
    profile = ctx.admin.get_profile(subject)
    if profile is None:
        raise HttpError(404, "No such person.")
    profiles = _profiles(ctx)
    names = _names(profiles)
    theirs = [g for g in ctx.admin.grants_of(subject) if scope.covers(g.node)]
    if not theirs and not scope.is_root:
        # Not someone this owner administers (§11.6): absence and denial look alike.
        raise HttpError(404, "No such person.")
    return page(
        "admin/person.html",
        ctx,
        scope=scope,
        profile=profile,
        is_self=subject == ctx.subject,
        grants=[_grant_view(g, names) for g in sorted(theirs, key=lambda g: g.node)],
        connector=_connector(ctx, profile),
        added=request.query.get("added") == "1",
        invite=request.query.get("invite", ""),
        revocation=REVOCATION_SENTENCE,
    )


def set_status(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    _require_root(scope, "disable or enable a person")
    subject = request.params["sub"]
    status = (request.form().get("status") or "").strip()
    if status not in ("active", "disabled"):
        raise HttpError(400, "Status must be active or disabled.")
    if subject == ctx.subject and status == "disabled":
        raise HttpError(400, "You cannot disable yourself. Ask another owner of /.")
    try:
        ctx.admin.set_status(subject, status)
    except ValueError:
        raise HttpError(404, "No such person.") from None
    return redirect(f"{PREFIX}/admin/people/{subject}")


# --- grants -------------------------------------------------------------------------


def _grant_groups(in_scope: dict[str, list[Grant]], names: dict[str, str]) -> list[dict[str, Any]]:
    """Grants grouped by node, nodes sorted, sentences within a node by person."""
    by_node: dict[str, list[Grant]] = {}
    for grants in in_scope.values():
        for g in grants:
            by_node.setdefault(g.node, []).append(g)
    return [
        {
            "node": node,
            "grants": [
                _grant_view(g, names)
                for g in sorted(by_node[node], key=lambda g: _who(names, g.subject).lower())
            ],
        }
        for node in sorted(by_node)
    ]


def grants(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    profiles = _profiles(ctx)
    names = _names(profiles)
    in_scope = _grants_in_scope(ctx, scope, profiles)
    # The grant form lists the people this viewer already administers: everyone who
    # can sign in for a root owner, otherwise only those holding a grant under an
    # owned root (§11.6). Display names only, never emails. Someone outside that
    # list is reached by typing their email into the same form (``grant`` below).
    pool = profiles.values() if scope.is_root else (profiles[s] for s in in_scope)
    grantees = sorted(
        (p for p in pool if p.status != "disabled"),
        key=lambda p: names[p.subject].lower(),
    )
    return page(
        "admin/grants.html",
        ctx,
        scope=scope,
        groups=_grant_groups(in_scope, names),
        grantees=grantees,
        permissions=[p.value for p in Permission],
        revocation=REVOCATION_SENTENCE,
    )


def grant(request: Request, ctx: WebContext) -> Response:
    """Grant to a person picked from the list, or — when none is picked — to the
    account behind a typed email. The email path is how a subtree owner brings
    someone new into their subtree; it confirms nothing when it fails."""
    form = request.form()
    subject = (form.get("subject") or "").strip()
    email = (form.get("email") or "").strip()
    node = _node_arg(form.get("node", ""))
    permission = _permission_arg(form.get("permission", ""))
    # Ownership before any lookup: a node the viewer does not own answers 403
    # whatever the email, so the form cannot probe accounts from outside its scope.
    try:
        ctx.admin.assert_owner(ctx.subject, node)
    except NotAnOwner:
        raise HttpError(403, f"You do not own {node}, so you cannot grant there.") from None
    if subject:
        if ctx.admin.get_profile(subject) is None:
            raise HttpError(400, "Pick a person from the list.")
    elif email:
        found = _profile_by_email(ctx, _email_arg(email))
        if found is None or found.status == "disabled":
            raise HttpError(400, EMAIL_NOT_GRANTABLE)
        subject = found.subject
    else:
        raise HttpError(400, "Pick a person from the list, or give their email.")
    try:
        ctx.admin.grant(ctx.subject, subject, node, permission)
    except NotAnOwner:
        raise HttpError(403, f"You do not own {node}, so you cannot grant there.") from None
    except ValueError as exc:
        raise HttpError(400, str(exc)) from None
    return redirect(_back_arg(form.get("back", ""), f"{PREFIX}/admin/grants"))


def revoke(request: Request, ctx: WebContext) -> Response:
    form = request.form()
    subject = (form.get("subject") or "").strip()
    if not subject:
        raise HttpError(400, "Which person?")
    node = _node_arg(form.get("node", ""))
    try:
        ctx.admin.revoke(ctx.subject, subject, node)
    except NotAnOwner:
        raise HttpError(403, f"You do not own {node}, so you cannot revoke there.") from None
    except ValueError as exc:
        raise HttpError(400, str(exc)) from None
    return redirect(_back_arg(form.get("back", ""), f"{PREFIX}/admin/grants"))


# --- unowned grants (§4.10) ---------------------------------------------------------


def unowned(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    _require_root(scope, "review unowned grants")
    profiles = _profiles(ctx)
    names = _names(profiles)
    rows: list[dict[str, Any]] = []
    for sub, prof in profiles.items():
        made = ctx.admin.grants_by_granter(sub)
        if not made:
            continue
        if prof.status == "disabled":
            reason = f"{names[sub]} is disabled"
            rows += [{**_grant_view(g, names), "reason": reason} for g in made]
            continue
        for g in made:
            if not ctx.admin.store.resolve(sub, g.node).allows(Permission.OWN):
                reason = f"{names[sub]} no longer owns {g.node}"
                rows.append({**_grant_view(g, names), "reason": reason})
    rows.sort(key=lambda r: (r["grant"].node, r["text"]))
    return page("admin/unowned.html", ctx, scope=scope, rows=rows, revocation=REVOCATION_SENTENCE)


# --- audit (§12.7) ----------------------------------------------------------------


def audit(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    raw = request.query.get("path", "")
    if not raw:
        return page("admin/audit.html", ctx, scope=scope, path="", days=AUDIT_DAYS)
    path = _node_arg(raw)
    if not scope.covers(path):
        raise HttpError(404, "No such path.")
    log_group = os.environ.get(LOG_GROUP_ENV, "")
    if not log_group:
        return page(
            "admin/audit.html", ctx, scope=scope, path=path, days=AUDIT_DAYS, unconfigured=True
        )
    names = _names(_profiles(ctx))
    error = ""
    result = AuditResult()
    try:
        result = AuditQuery(log_group).query(path, since_days=AUDIT_DAYS)
    except (ClientError, BotoCoreError) as exc:
        ctx.log.warning(
            "audit_query_failed", request_id=ctx.request_id, exc_class=type(exc).__name__
        )
        error = "The audit log could not be queried just now. It has been logged."
    rows = [
        {
            "at": r.at,
            "who": _who(names, r.subject),
            "tool": r.tool,
            "decision": r.decision,
            "status": r.status,
        }
        for r in result.rows
    ]
    return page(
        "admin/audit.html",
        ctx,
        scope=scope,
        path=path,
        days=AUDIT_DAYS,
        rows=rows,
        partial=result.partial,
        error=error,
    )


# --- hard delete runbook (§5.2, §8.9, §11.6) -------------------------------------------


def _all_versions(ctx: WebContext, s3: Any, path: str) -> list[StoredObject]:
    """Every version of the key, newest first, with actor and kind from a head each.
    A hard delete is every version or it is not a delete (RUNBOOK §3 step 3)."""
    out: list[StoredObject] = []
    cursor: str | None = None
    while True:
        entries, cursor = ctx.store.list_versions(s3, path, limit=VERSIONS_PAGE, cursor=cursor)
        for entry in entries:
            headed = ctx.store.head_version(s3, path, entry.version_id or "")
            out.append(headed if headed is not None else entry)
        if cursor is None:
            return out


def delete_runbook(request: Request, ctx: WebContext) -> Response:
    scope = _scope(ctx)
    if not scope.owns_anything:
        return page("admin/index.html", ctx, scope=scope, people=0, grants=0)
    path = _article_arg("/" + request.params["path"].lstrip("/"))
    if not scope.covers(path):
        raise HttpError(404, "No such path.")
    s3 = ctx.minter.s3(ctx.subject, Shape.READ, path)
    try:
        versions = _all_versions(ctx, s3, path)
    except AccessDenied:
        raise HttpError(404, "No such path.") from None
    if not versions:
        raise HttpError(404, "No such path.")
    ctx.log.info(
        "hard_delete_runbook_viewed",
        request_id=ctx.request_id,
        subject=ctx.subject,
        path=path,
        versions=len(versions),
    )
    key = key_for(path)
    version_ids = [v.version_id or "" for v in versions]
    return page(
        "admin/delete.html",
        ctx,
        scope=scope,
        path=path,
        key=key,
        bucket=ctx.settings.bucket,
        versions=[
            {
                "version_id": v.version_id or "",
                "at": v.last_modified.isoformat() if v.last_modified else "",
                "actor": v.metadata.get("actor", "unknown"),
                "kind": v.metadata.get("kind", "write"),
                "size": v.size,
            }
            for v in versions
        ],
        delete_commands="\n".join(
            f'aws s3api delete-object --bucket {ctx.settings.bucket} --key "{key}" '
            f'--version-id "{vid}" --bypass-governance-retention'
            for vid in version_ids
        ),
    )


# --- registration ---------------------------------------------------------------------


def register(router: Router) -> None:
    router.add("GET", "/admin", view(index))
    router.add("GET", "/admin/people", view(people))
    router.add("POST", "/admin/people", view(add_person))
    router.add("GET", "/admin/people/{sub}", view(person))
    router.add("POST", "/admin/people/{sub}/status", view(set_status))
    router.add("GET", "/admin/grants", view(grants))
    router.add("POST", "/admin/grants", view(grant))
    router.add("POST", "/admin/grants/revoke", view(revoke))
    router.add("GET", "/admin/unowned", view(unowned))
    router.add("GET", "/admin/audit", view(audit))
    router.add("GET", "/admin/delete/{path:path}", view(delete_runbook))


__all__ = [
    "EMAIL_NOT_ADDABLE",
    "EMAIL_NOT_GRANTABLE",
    "REVOCATION_SENTENCE",
    "Scope",
    "register",
    "sentence",
]
