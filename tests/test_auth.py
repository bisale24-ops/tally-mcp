"""The OAuth 2.1 flow Alexa+ requires, exercised over real HTTP."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tally.server import create_server
from tally.store import Store

PUBLIC_URL = "http://localhost"  # the SDK allows loopback HTTP; production must be https


@pytest.fixture
async def seeded(tmp_path):
    """An ASGI client on the auth-enabled app, plus an outstanding invite code.

    Linking is by code now, so the flow needs a household that has invited
    somebody - exactly the state a real second flatmate signs in from.
    """
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="tally:sam")
    code = store.add_member(flat, flat.add_member("Chris"))

    app = create_server(store, public_url=PUBLIC_URL).streamable_http_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as client:
            yield client, code


@pytest.fixture
async def http(seeded):
    client, _ = seeded
    return client


@pytest.fixture
async def invite(seeded):
    _, code = seeded
    return code


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@pytest.mark.anyio
async def test_discovery_advertises_pkce_as_alexa_requires(http):
    """Alexa+ reads this document to learn how to authenticate. S256 is mandatory."""
    response = await http.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert "S256" in metadata["code_challenge_methods_supported"]
    assert "authorization_code" in metadata["grant_types_supported"]
    assert metadata["issuer"].rstrip("/") == PUBLIC_URL


@pytest.mark.anyio
async def test_an_unauthenticated_call_is_refused(http):
    """The whole point of the auth layer."""
    response = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_the_full_authorization_code_flow_with_pkce(http, invite):
    verifier, challenge = pkce()

    registered = await http.post(
        "/register",
        json={
            "client_name": "Alexa+",
            "redirect_uris": ["https://alexa.example.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert registered.status_code in (200, 201), registered.text
    client_id = registered.json()["client_id"]

    # The authorize endpoint must not mint a code: it does not yet know who the
    # human is. It sends them to the linking page instead.
    authorized = await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://alexa.example.com/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "scope": "ledger",
            "resource": f"{PUBLIC_URL}/mcp",
        },
    )
    assert authorized.status_code in (302, 307)
    login_url = authorized.headers["location"]
    assert login_url.startswith("/login?pending=")
    pending = parse_qs(urlparse(login_url).query)["pending"][0]

    page = await http.get("/login", params={"pending": pending})
    assert page.status_code == 200 and "invite code" in page.text

    linked = await http.post("/login", data={"pending": pending, "code": invite})
    assert linked.status_code == 302
    callback = urlparse(linked.headers["location"])
    assert callback.hostname == "alexa.example.com"
    params = parse_qs(callback.query)
    assert params["state"] == ["xyz"]
    code = params["code"][0]

    tokens = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://alexa.example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{PUBLIC_URL}/mcp",
        },
    )
    assert tokens.status_code == 200, tokens.text
    granted = tokens.json()
    assert granted["token_type"] == "Bearer"
    assert granted["refresh_token"]

    # The token now opens the MCP endpoint.
    called = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {granted['access_token']}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert called.status_code != 401


@pytest.mark.anyio
async def test_a_replayed_code_is_rejected(http, invite):
    """One-time means one time; a stolen code must be useless after first use."""
    verifier, challenge = pkce()
    registered = await http.post(
        "/register",
        json={
            "client_name": "Alexa+",
            "redirect_uris": ["https://alexa.example.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = registered.json()["client_id"]
    authorized = await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://alexa.example.com/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    pending = parse_qs(urlparse(authorized.headers["location"]).query)["pending"][0]
    linked = await http.post("/login", data={"pending": pending, "code": invite})
    code = parse_qs(urlparse(linked.headers["location"]).query)["code"][0]

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://alexa.example.com/callback",
        "client_id": client_id,
        "code_verifier": verifier,
    }
    assert (await http.post("/token", data=body)).status_code == 200
    assert (await http.post("/token", data=body)).status_code >= 400


@pytest.mark.anyio
async def test_a_wrong_pkce_verifier_is_rejected(http, invite):
    """Without this check, intercepting the redirect is enough to steal the account."""
    _, challenge = pkce()
    registered = await http.post(
        "/register",
        json={
            "client_name": "Alexa+",
            "redirect_uris": ["https://alexa.example.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = registered.json()["client_id"]
    authorized = await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://alexa.example.com/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    pending = parse_qs(urlparse(authorized.headers["location"]).query)["pending"][0]
    linked = await http.post("/login", data={"pending": pending, "code": invite})
    code = parse_qs(urlparse(linked.headers["location"]).query)["code"][0]

    stolen = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://alexa.example.com/callback",
            "client_id": client_id,
            "code_verifier": secrets.token_urlsafe(64),  # attacker's guess
        },
    )
    assert stolen.status_code >= 400


@pytest.mark.anyio
async def test_an_expired_link_cannot_be_used(http):
    page = await http.get("/login", params={"pending": "never-existed"})
    assert page.status_code == 400


@pytest.mark.anyio
async def test_the_login_page_does_not_reflect_markup(http):
    """`pending` comes straight from the request body and was interpolated into
    the page unescaped, so an empty submission reflected whatever was sent."""
    payload = '"><script>alert(1)</script>'
    response = await http.post("/login", data={"pending": payload, "code": ""})
    assert "<script>alert(1)</script>" not in response.text


@pytest.mark.anyio
async def test_a_wrong_invite_code_does_not_link_anything(http, invite):
    """Guessing a flatmate's name used to be enough to join their household."""
    _, challenge = pkce()
    registered = await http.post(
        "/register",
        json={
            "client_name": "Alexa+",
            "redirect_uris": ["https://alexa.example.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = registered.json()["client_id"]
    authorized = await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": "https://alexa.example.com/callback",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    pending = parse_qs(urlparse(authorized.headers["location"]).query)["pending"][0]

    refused = await http.post("/login", data={"pending": pending, "code": "Chris"})
    assert refused.status_code == 200
    assert "not valid" in refused.text
