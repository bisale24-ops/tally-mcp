"""What Alexa+ needs from the OAuth side, per its MCP QuickStart."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
import pytest
from asgi_lifespan import LifespanManager

from tally.server import create_server
from tally.store import Store

PUBLIC_URL = "http://localhost"
STATIC_CLIENT = "alexa-plus-tally"
STATIC_REDIRECT = "https://layla.amazon.com/api/skill/link/tally"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@pytest.fixture
async def preconfigured(tmp_path, monkeypatch):
    """A server configured for a host that cannot register itself."""
    monkeypatch.setenv("TALLY_CLIENT_ID", STATIC_CLIENT)
    monkeypatch.setenv("TALLY_REDIRECT_URI", STATIC_REDIRECT)
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="tally:sam")
    code = store.add_member(flat, flat.add_member("Chris"))
    app = create_server(store, public_url=PUBLIC_URL).streamable_http_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as client:
            yield client, code


async def link(client, invite, *, client_id=STATIC_CLIENT, redirect=STATIC_REDIRECT):
    verifier, challenge = pkce()
    authorized = await client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
            "resource": f"{PUBLIC_URL}/mcp",
        },
    )
    assert authorized.status_code in (302, 307), authorized.text
    pending = parse_qs(urlparse(authorized.headers["location"]).query)["pending"][0]
    linked = await client.post("/login", data={"pending": pending, "code": invite})
    assert linked.status_code == 302, linked.text
    auth_code = parse_qs(urlparse(linked.headers["location"]).query)["code"][0]

    granted = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{PUBLIC_URL}/mcp",
        },
    )
    assert granted.status_code == 200, granted.text
    return granted.json()


@pytest.mark.anyio
async def test_a_host_that_cannot_register_itself_can_still_link(preconfigured):
    """Alexa+ does not do dynamic client registration. Without a preconfigured
    client there is no way in at all."""
    client, invite = preconfigured
    tokens = await link(client, invite)
    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"]


@pytest.mark.anyio
async def test_an_unknown_client_is_still_refused(preconfigured):
    client, _ = preconfigured
    _, challenge = pkce()
    response = await client.get(
        "/authorize",
        params={
            "client_id": "somebody-else",
            "redirect_uri": STATIC_REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    assert response.status_code >= 400 or "error" in str(response.headers.get("location", ""))


@pytest.mark.anyio
async def test_a_preconfigured_client_may_not_redirect_anywhere_it_likes(preconfigured):
    client, _ = preconfigured
    _, challenge = pkce()
    response = await client.get(
        "/authorize",
        params={
            "client_id": STATIC_CLIENT,
            "redirect_uri": "https://attacker.example.com/steal",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    assert response.status_code >= 400 or "attacker.example.com" not in str(response.headers.get("location", ""))


@pytest.mark.anyio
async def test_a_retried_refresh_does_not_kill_the_session(preconfigured):
    """Rotation deleted the old token first, so a resent refresh - or two racing
    ones - left the device holding nothing it could use."""
    client, invite = preconfigured
    tokens = await link(client, invite)

    body = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": STATIC_CLIENT,
        "resource": f"{PUBLIC_URL}/mcp",
    }
    first = await client.post("/token", data=body)
    second = await client.post("/token", data=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["access_token"] == second.json()["access_token"]


@pytest.mark.anyio
async def test_two_refreshes_racing_agree_on_one_answer(preconfigured):
    client, invite = preconfigured
    tokens = await link(client, invite)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": STATIC_CLIENT,
    }

    answers: list[str] = []

    async def refresh() -> None:
        response = await client.post("/token", data=body)
        if response.status_code == 200:
            answers.append(response.json()["access_token"])

    async with anyio.create_task_group() as tg:
        for _ in range(4):
            tg.start_soon(refresh)

    assert answers, "every racing refresh failed"
    assert len(set(answers)) == 1


@pytest.mark.anyio
async def test_an_unauthenticated_call_points_at_the_metadata(preconfigured):
    """Alexa+ discovers where to authenticate from this header. Without it the
    401 is a dead end."""
    client, _ = preconfigured
    refused = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
    )
    assert refused.status_code == 401
    challenge = refused.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer ")
    assert "resource_metadata=" in challenge

    described = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert described.status_code == 200
    assert described.json()["authorization_servers"]
