"""Minimal HTTP layer over the API Gateway REST proxy event.

No framework: a request object, a response object, cookie helpers, and a router with
``{name}`` path segments. Everything the web application does is a handful of
server-rendered pages for a few people; a framework would be more surface than
substance (HANDOFF §11.6, §12.3).
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any


def content_security_policy(*form_action: str) -> str:
    """The CSP carried by every response (HANDOFF §4.9, §12.3).

    Args:
        form_action: Origins beyond ``'self'`` that a form may be submitted to. The
            authorization server belongs here: the sign-in POST answers with a
            redirect to AuthKit, and ``form-action`` is enforced against the
            *redirect target*, not only the form's action. Without it the browser
            drops the submission silently — no request, no console error.

    Returns:
        The header value.
    """
    allowed = " ".join(("'self'", *form_action))
    return (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        f"script-src 'self'; frame-ancestors 'none'; form-action {allowed}; base-uri 'none'"
    )


# Security headers on every response (HANDOFF §4.9 framing rule, §12.3). The CSP here
# is the floor; `handle` replaces it with one naming the authorization server.
BASE_HEADERS: dict[str, str] = {
    "Content-Security-Policy": content_security_policy(),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@dataclass
class Request:
    """One inbound request, already decoded."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]  # lowercase keys
    cookies: dict[str, str]
    body: bytes
    request_id: str = ""
    params: dict[str, str] = field(default_factory=dict)  # route captures

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> Request:
        headers: dict[str, str] = {}
        for k, v in (event.get("headers") or {}).items():
            if v is not None:
                headers[k.lower()] = str(v)
        for k, vs in (event.get("multiValueHeaders") or {}).items():
            if vs and k.lower() not in headers:
                headers[k.lower()] = str(vs[0])
        raw = event.get("body") or ""
        body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
        query = {
            k: v for k, v in (event.get("queryStringParameters") or {}).items() if v is not None
        }
        cookies: dict[str, str] = {}
        if "cookie" in headers:
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(headers["cookie"])
                cookies = {k: m.value for k, m in jar.items()}
            except Exception:  # noqa: BLE001 — a malformed cookie header is just no cookies
                cookies = {}
        return cls(
            method=str(event.get("httpMethod", "GET")).upper(),
            path=str(event.get("path", "/")),
            query=query,
            headers=headers,
            cookies=cookies,
            body=body,
            request_id=str((event.get("requestContext") or {}).get("requestId", "")),
        )

    def form(self) -> dict[str, str]:
        """Decode an ``application/x-www-form-urlencoded`` body (last value wins)."""
        ctype = self.headers.get("content-type", "")
        if not ctype.startswith("application/x-www-form-urlencoded"):
            return {}
        return {
            k: v[-1]
            for k, v in urllib.parse.parse_qs(self.body.decode(), keep_blank_values=True).items()
        }


@dataclass
class Response:
    status: int = 200
    body: str | bytes = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)  # raw Set-Cookie values

    def to_event(self) -> dict[str, Any]:
        headers = {**BASE_HEADERS, **self.headers}
        out: dict[str, Any] = {"statusCode": self.status, "headers": headers}
        if self.cookies:
            out["multiValueHeaders"] = {"Set-Cookie": list(self.cookies)}
        if isinstance(self.body, bytes):
            out["body"] = base64.b64encode(self.body).decode()
            out["isBase64Encoded"] = True
        else:
            out["body"] = self.body
        return out


def html(body: str, status: int = 200, **kw: Any) -> Response:
    return Response(status, body, {"Content-Type": "text/html; charset=utf-8"}, **kw)


def redirect(location: str, status: int = 303, **kw: Any) -> Response:
    return Response(status, "", {"Location": location}, **kw)


def json_response(data: Any, status: int = 200) -> Response:
    return Response(status, json.dumps(data), {"Content-Type": "application/json"})


def set_cookie(
    name: str,
    value: str,
    *,
    max_age: int | None = None,
    path: str = "/",
    http_only: bool = True,
    same_site: str = "Lax",
) -> str:
    """Build a ``Set-Cookie`` value. Always ``Secure``; ``__Host-`` names force
    ``Path=/`` and no ``Domain`` (HANDOFF §4.9 cookie row)."""
    if name.startswith("__Host-"):
        path = "/"
    parts = [f"{name}={value}", f"Path={path}", "Secure", f"SameSite={same_site}"]
    if http_only:
        parts.append("HttpOnly")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def clear_cookie(name: str) -> str:
    return set_cookie(name, "", max_age=0)


Handler = Callable[[Request], Response]


class HttpError(Exception):
    """Short-circuit with a status. The app renders it through the error page."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.message = message


class Router:
    """``{name}`` captures one segment; ``{name:path}`` captures the rest."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix.rstrip("/")
        self._routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = "^" + self.prefix
        for part in re.split(r"(\{[a-z_]+(?::path)?\})", pattern):
            if part.startswith("{") and part.endswith("}"):
                name = part[1:-1]
                if name.endswith(":path"):
                    regex += f"(?P<{name[:-5]}>.+)"
                else:
                    regex += f"(?P<{name}>[^/]+)"
            else:
                regex += re.escape(part)
        regex += "/?$"
        self._routes.append((method.upper(), re.compile(regex), handler))

    def get(self, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.add("GET", pattern, fn)
            return fn

        return deco

    def post(self, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.add("POST", pattern, fn)
            return fn

        return deco

    def dispatch(self, request: Request) -> Response:
        method_matched = False
        for method, regex, handler in self._routes:
            m = regex.match(request.path)
            if not m:
                continue
            if method != request.method:
                method_matched = True
                continue
            request.params = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
            return handler(request)
        raise HttpError(405 if method_matched else 404)


__all__ = [
    "BASE_HEADERS",
    "Handler",
    "HttpError",
    "Request",
    "Response",
    "Router",
    "clear_cookie",
    "html",
    "json_response",
    "redirect",
    "set_cookie",
]
