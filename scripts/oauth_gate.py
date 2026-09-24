"""The §2 gate, as a script. Run it against the WorkOS staging environment before
writing the authorizer (HANDOFF §2, §11.3 step 4).

    uv run python scripts/oauth_gate.py \\
        --authkit https://<slug>.authkit.app \\
        --client-id client_01... \\
        --resource https://wiki-dev.famestad.com/mcp

It performs, in order:

  1. Metadata      — CIMD advertised, `none` in token auth methods, S256 PKCE.
  3. Round trip    — authorization-code + PKCE with `resource=`; decodes the token;
                     `aud` must equal the resource byte for byte.
  4. Negative      — the same flow with an UNREGISTERED resource; must be refused.
                     (Runs last because it needs a second browser round.)

Checks 2, 6 and 7 are dashboard facts; the script prints reminders for them. Check 5
(scopes) is retired: ADR-0016 withdrew custom scopes — a CIMD client cannot be
granted scopes, so the check tested a client type no user has. The token's scope
claim is still printed, for information.

The redirect URI is http://127.0.0.1:<port>/callback — register it on the client
exactly. A public client needs no secret; pass --client-secret only if the dashboard
created a confidential one.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field

import httpx
import jwt


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


@dataclass
class Callback:
    params: dict[str, str] = field(default_factory=dict)
    event: threading.Event = field(default_factory=threading.Event)


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _serve_callback(port: int, cb: Callback) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            cb.params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"You can close this tab and return to the terminal.\n")
            cb.event.set()

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _authorize(
    meta: dict,
    client_id: str,
    client_secret: str | None,
    resource: str,
    scope: str,
    port: int,
    timeout: int,
) -> tuple[dict | None, str, bool]:
    """Run one authorization-code flow. Returns (token_response, detail, refused).

    ``refused`` is True only when the authorization server actively refused the
    request — an ``error`` at /authorize, or a non-200 at /token. A timeout, a
    state mismatch, or a missing code are inconclusive: no request was rejected,
    none was ever completed either. Callers must not treat those as a refusal.
    """
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    cb = Callback()
    server = _serve_callback(port, cb)
    try:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
        print(f"\n  opening browser for resource={resource}")
        print(f"  if it does not open, visit:\n  {url}\n")
        webbrowser.open(url)
        if not cb.event.wait(timeout):
            return None, "timed out waiting for the browser callback", False
    finally:
        server.shutdown()

    if "error" in cb.params:
        err = cb.params.get("error")
        desc = cb.params.get("error_description", "")
        return None, f"refused at /authorize: {err} {desc}".strip(), True
    if cb.params.get("state") != state:
        return None, "state mismatch at callback — treat as refused", False
    code = cb.params.get("code")
    if not code:
        return None, "no code in callback", False

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": resource,
    }
    if client_secret:
        data["client_secret"] = client_secret
    r = httpx.post(meta["token_endpoint"], data=data, timeout=20)
    if r.status_code != 200:
        return None, f"refused at /token: HTTP {r.status_code} {r.text[:300]}", True
    return r.json(), "", False


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--authkit", required=True, help="https://<slug>.authkit.app")
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", default=None)
    p.add_argument("--resource", required=True, help="the canonical MCP URL you registered")
    p.add_argument(
        "--unregistered-resource",
        default="https://not-registered.invalid/mcp",
        help="a resource you did NOT register (check 4)",
    )
    p.add_argument(
        "--scope",
        default="openid profile email offline_access",
        help="requested scope string; offline_access is included by default so "
        "AS-9's refresh-token check has something to assert on",
    )
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--skip-negative", action="store_true")
    a = p.parse_args()

    authkit = a.authkit.rstrip("/")
    results: list[Result] = []

    # ---- 1. metadata -----------------------------------------------------------
    meta_url = f"{authkit}/.well-known/oauth-authorization-server"
    r = httpx.get(meta_url, timeout=20)
    if r.status_code != 200:
        print(f"FATAL: {meta_url} returned {r.status_code}")
        return 2
    meta = r.json()
    cimd = meta.get("client_id_metadata_document_supported") is True
    none_auth = "none" in meta.get("token_endpoint_auth_methods_supported", [])
    s256 = "S256" in meta.get("code_challenge_methods_supported", [])
    issuer_ok = meta.get("issuer", "").rstrip("/") == authkit
    results.append(
        Result(
            "1 metadata: client_id_metadata_document_supported",
            cimd,
            str(meta.get("client_id_metadata_document_supported")),
        )
    )
    results.append(
        Result(
            "1 metadata: 'none' in token_endpoint_auth_methods_supported",
            none_auth,
            str(meta.get("token_endpoint_auth_methods_supported")),
        )
    )
    results.append(
        Result(
            "1 metadata: S256 in code_challenge_methods_supported",
            s256,
            str(meta.get("code_challenge_methods_supported")),
        )
    )
    results.append(
        Result("1 metadata: issuer equals the AuthKit domain", issuer_ok, str(meta.get("issuer")))
    )
    print(f"issuer                 {meta.get('issuer')}")
    print(f"authorization_endpoint {meta.get('authorization_endpoint')}")
    print(f"token_endpoint         {meta.get('token_endpoint')}")
    print(f"jwks_uri               {meta.get('jwks_uri')}")
    print(f"scopes_supported       {meta.get('scopes_supported')}")

    # ---- 3. round trip -------------------------------------------------------------
    tok, why, _ = _authorize(
        meta, a.client_id, a.client_secret, a.resource, a.scope, a.port, a.timeout
    )
    if tok is None:
        results.append(Result("3 round trip: token issued for the registered resource", False, why))
    else:
        access = tok.get("access_token", "")
        try:
            claims = jwt.decode(access, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            claims = {}
            results.append(
                Result(
                    "3 round trip: access token is a decodable JWT",
                    False,
                    f"{type(e).__name__}: {e}",
                )
            )
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        aud_ok = aud_list == [a.resource]
        results.append(
            Result(
                "3 round trip: aud equals the resource byte for byte",
                aud_ok,
                f"aud={aud!r} resource={a.resource!r}",
            )
        )
        results.append(
            Result(
                "3 round trip: iss equals the AuthKit domain",
                claims.get("iss", "").rstrip("/") == authkit,
                str(claims.get("iss")),
            )
        )
        scope_claim = claims.get("scope") or claims.get("scp") or tok.get("scope") or ""
        scopes = set(scope_claim.split()) if isinstance(scope_claim, str) else set(scope_claim)
        print(f"scope claim            {sorted(scopes)}  (informational — check 5 is retired)")
        lifetime = int(claims.get("exp", 0)) - int(claims.get("iat", time.time()))
        results.append(
            Result(
                "AS-9: access token lifetime <= 15 minutes (SHOULD)",
                0 < lifetime <= 900,
                f"{lifetime}s",
            )
        )
        results.append(
            Result(
                "AS-9: refresh token present",
                bool(tok.get("refresh_token")),
                "yes" if tok.get("refresh_token") else "no",
            )
        )
        print(f"\nsub={claims.get('sub')}  (this is the value for: make grant-owner SUBJECT=...)")
        header = jwt.get_unverified_header(access)
        print(f"alg={header.get('alg')}  kid={header.get('kid')}")

    # ---- 4. negative ---------------------------------------------------------------
    if a.skip_negative:
        results.append(
            Result("4 negative: unregistered resource refused", False, "skipped (--skip-negative)")
        )
    else:
        tok2, why2, refused2 = _authorize(
            meta, a.client_id, a.client_secret, a.unregistered_resource, a.scope, a.port, a.timeout
        )
        if tok2 is None and refused2:
            results.append(Result("4 negative: unregistered resource refused", True, why2))
        elif tok2 is None:
            results.append(
                Result(
                    "4 negative: unregistered resource refused",
                    False,
                    f"inconclusive: {why2} — no refusal was observed and no token "
                    "request completed. This is not a pass; re-run the gate.",
                )
            )
        else:
            try:
                aud2 = jwt.decode(
                    tok2.get("access_token", ""), options={"verify_signature": False}
                ).get("aud")
            except jwt.PyJWTError:
                aud2 = "<undecodable>"
            results.append(
                Result(
                    "4 negative: unregistered resource refused",
                    False,
                    f"A TOKEN WAS ISSUED with aud={aud2!r}. "
                    "WorkOS is rejected; switch providers (§2).",
                )
            )

    # ---- report ---------------------------------------------------------------------
    print("\n" + "=" * 78)
    worst = 0
    for res in results:
        mark = "PASS" if res.ok else "FAIL"
        worst = max(worst, 0 if res.ok else 1)
        print(f"{mark}  {res.name}\n      {res.detail}")
    print("=" * 78)
    print("Dashboard-only checks (confirm by eye):")
    print("  2  the resource indicator is registered and matches --resource exactly")
    print("  6  Connect settings offer a CIMD domain allowlist (else §4.9 last row is a wish)")
    print("  7  self-signup is disabled for this environment")
    print("\nGATE " + ("PASSED" if worst == 0 else "FAILED — do not write the authorizer yet"))
    return worst


if __name__ == "__main__":
    sys.exit(main())
