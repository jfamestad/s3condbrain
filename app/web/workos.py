"""WorkOS User Management API client — the "create a person" half of increment E
(HANDOFF §11.6 "Adding a person", §15.2).

Three calls, all against ``https://api.workos.com`` with the management API key:

    POST /user_management/users                 create_user       → user id (the token ``sub``)
    GET  /user_management/users?email=          find_user_by_email → user id or None
    POST /user_management/invitations           send_invitation   → WorkOS emails the sign-in link

There is deliberately no ``deactivate``: User Management has no such call (only a
hard ``DELETE /user_management/users/{id}``), and disabling a person is ours anyway —
the PROFILE status refuses them at the web login and on the data plane (§12.9,
RUNBOOK §5 step 2). Deleting them at WorkOS is RUNBOOK §5 step 3, done by hand.

The key is a bearer secret. It is never logged, never part of an exception message,
and never echoed; request bodies (which carry email addresses) are not logged either.
Errors surface as ``WorkOSError`` carrying only the HTTP status and WorkOS's short
``code``/``message`` fields, which is what an operator needs to check the dashboard.
"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://api.workos.com"
NOT_CONFIGURED = "WorkOS API key not configured — see docs/DEPLOY.md §5"
_PLACEHOLDERS = frozenset({"", "REPLACE-ME"})
_TIMEOUT_SECONDS = 15


class WorkOSError(Exception):
    """A WorkOS call failed. Carries status and WorkOS's ``code``/``message`` — never
    the request, the key, or the raw response body."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"WorkOS {status} {code}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message


def _error_fields(response: httpx.Response) -> tuple[str, str]:
    """``(code, message)`` from a WorkOS error body, tolerating any shape."""
    try:
        data = response.json()
    except ValueError:
        return "unknown", response.reason_phrase or "no detail"
    if not isinstance(data, dict):
        return "unknown", response.reason_phrase or "no detail"
    code = data.get("code") or data.get("error") or "unknown"
    message = data.get("message") or data.get("error_description") or ""
    return str(code)[:64], str(message)[:200]


class WorkOSClient:
    """Thin client over the three User Management calls the console needs.

    Args:
        api_key: The WorkOS management API key (``sk_...``) from the web secret.
        http: Injected ``httpx.Client`` for tests (``httpx.MockTransport``).
        base_url: Overridable for tests; defaults to the public API.

    Raises:
        RuntimeError: when ``api_key`` is empty or the CDK placeholder ``REPLACE-ME``
            — the page turns this into "WorkOS API key not configured".
    """

    def __init__(
        self, api_key: str, http: httpx.Client | None = None, base_url: str = API_BASE
    ) -> None:
        if api_key.strip() in _PLACEHOLDERS:
            raise RuntimeError(NOT_CONFIGURED)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        self._base = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=_TIMEOUT_SECONDS)

    def __repr__(self) -> str:  # never the key
        return f"WorkOSClient({self._base})"

    # --- calls -----------------------------------------------------------------------

    def create_user(self, email: str, first_name: str, last_name: str) -> str:
        """``POST /user_management/users``. Returns the new user's id — the ``sub``
        every token for this person will carry, and therefore our subject.

        Raises:
            WorkOSError: on any non-2xx (``email_not_available`` when the address is
                already a user — call ``find_user_by_email`` first).
        """
        body: dict[str, Any] = {"email": email, "email_verified": False}
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name
        data = self._request("POST", "/user_management/users", json=body)
        user_id = data.get("id")
        if not isinstance(user_id, str) or not user_id:
            raise WorkOSError(200, "malformed", "create user response carried no id")
        return user_id

    def find_user_by_email(self, email: str) -> str | None:
        """``GET /user_management/users?email=`` → the user id, or ``None``."""
        data = self._request("GET", "/user_management/users", params={"email": email})
        users = data.get("data")
        if not isinstance(users, list):
            return None
        for user in users:
            if isinstance(user, dict) and isinstance(user.get("id"), str):
                if str(user.get("email", "")).lower() == email.lower():
                    return user["id"]
        return None

    def send_invitation(self, email: str) -> None:
        """``POST /user_management/invitations``. WorkOS sends the email with the
        sign-in link; this returns nothing because nothing here needs the token.

        Raises:
            WorkOSError: on any non-2xx. The console logs it and tells the owner to
                invite from the dashboard instead; the connector block still shows.
        """
        self._request("POST", "/user_management/invitations", json={"email": email})

    # --- transport -------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.http.request(
                method, self._base + path, headers=self._headers, json=json, params=params
            )
        except httpx.HTTPError as exc:
            # httpx messages name the URL at most; never the headers.
            raise WorkOSError(0, "transport", type(exc).__name__) from exc
        if response.status_code >= 400:
            code, message = _error_fields(response)
            raise WorkOSError(response.status_code, code, message)
        try:
            data = response.json()
        except ValueError:
            raise WorkOSError(response.status_code, "malformed", "non-JSON response") from None
        if not isinstance(data, dict):
            raise WorkOSError(response.status_code, "malformed", "unexpected response shape")
        return data


__all__ = ["API_BASE", "NOT_CONFIGURED", "WorkOSClient", "WorkOSError"]
