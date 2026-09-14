"""``WorkOSClient`` over ``httpx.MockTransport``: the three endpoints, bearer auth,
error mapping, and the placeholder-key refusal."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.web.workos import API_BASE, NOT_CONFIGURED, WorkOSClient, WorkOSError

KEY = "sk_test_123"


def _client(handler: Any) -> WorkOSClient:
    return WorkOSClient(KEY, http=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("key", ["", "REPLACE-ME", "  "])
def test_placeholder_key_is_refused_with_a_clear_message(key: str) -> None:
    with pytest.raises(RuntimeError, match="not configured"):
        WorkOSClient(key)
    assert "docs/DEPLOY.md" in NOT_CONFIGURED


def test_create_user_posts_and_returns_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == f"{API_BASE}/user_management/users"
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        assert body == {
            "email": "dana@example.com",
            "email_verified": False,
            "first_name": "Dana",
            "last_name": "Q",
        }
        return httpx.Response(
            201, json={"object": "user", "id": "user_01ABC", "email": body["email"]}
        )

    assert _client(handler).create_user("dana@example.com", "Dana", "Q") == "user_01ABC"
    assert len(seen) == 1


def test_create_user_omits_empty_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"email": "d@x.co", "email_verified": False}
        return httpx.Response(201, json={"id": "user_02"})

    assert _client(handler).create_user("d@x.co", "", "") == "user_02"


def test_find_user_by_email_filters_and_matches_case_insensitively() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/user_management/users"
        assert request.url.params["email"] == "Dana@Example.com"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "user_other", "email": "other@example.com"},
                    {"id": "user_dana", "email": "dana@example.com"},
                ],
                "list_metadata": {},
            },
        )

    assert _client(handler).find_user_by_email("Dana@Example.com") == "user_dana"


def test_find_user_by_email_none_when_absent_or_malformed() -> None:
    assert (
        _client(lambda r: httpx.Response(200, json={"data": []})).find_user_by_email("x@y.z")
        is None
    )
    assert (
        _client(lambda r: httpx.Response(200, json={"nope": 1})).find_user_by_email("x@y.z") is None
    )


def test_send_invitation_posts_email() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/user_management/invitations"
        assert json.loads(request.content) == {"email": "dana@example.com"}
        return httpx.Response(
            201, json={"object": "invitation", "id": "invitation_01", "state": "pending"}
        )

    _client(handler).send_invitation("dana@example.com")


def test_errors_carry_status_and_code_but_never_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"code": "email_not_available", "message": "Email already taken."},
        )

    with pytest.raises(WorkOSError) as exc:
        _client(handler).create_user("d@x.co", "D", "")
    assert exc.value.status == 422 and exc.value.code == "email_not_available"
    assert KEY not in str(exc.value) and "already taken" in str(exc.value)


def test_non_json_and_transport_errors_are_workos_errors() -> None:
    with pytest.raises(WorkOSError) as e1:
        _client(lambda r: httpx.Response(500, text="<html>boom</html>")).send_invitation("a@b.c")
    assert e1.value.status == 500 and e1.value.code == "unknown"

    with pytest.raises(WorkOSError) as e2:
        _client(lambda r: httpx.Response(200, text="not json")).find_user_by_email("a@b.c")
    assert e2.value.code == "malformed"

    def raiser(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(WorkOSError) as e3:
        _client(raiser).send_invitation("a@b.c")
    assert e3.value.status == 0 and e3.value.code == "transport"
    assert KEY not in str(e3.value)


def test_create_user_without_id_is_an_error() -> None:
    with pytest.raises(WorkOSError, match="no id"):
        _client(lambda r: httpx.Response(201, json={"object": "user"})).create_user(
            "a@b.c", "A", ""
        )


def test_repr_hides_the_key() -> None:
    assert KEY not in repr(_client(lambda r: httpx.Response(200, json={})))
