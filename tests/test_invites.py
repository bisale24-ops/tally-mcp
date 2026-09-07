"""Invite codes are the only thing standing between a stranger and a ledger."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tally.server import create_server
from tally.store import Store

PUBLIC_URL = "http://localhost"


@pytest.fixture
async def linking(tmp_path):
    """The app, plus a household with one outstanding invite."""
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="tally:sam")
    code = store.add_member(flat, flat.add_member("Chris"))
    app = create_server(store, public_url=PUBLIC_URL).streamable_http_app()
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as client:
            yield client, store, code


async def start_linking(client) -> str:
    registered = await client.post(
        "/register",
        json={
            "client_name": "Alexa+",
            "redirect_uris": ["https://alexa.example.com/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    authorized = await client.get(
        "/authorize",
        params={
            "client_id": registered.json()["client_id"],
            "redirect_uri": "https://alexa.example.com/callback",
            "response_type": "code",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
            "scope": "ledger",
        },
    )
    return parse_qs(urlparse(authorized.headers["location"]).query)["pending"][0]


@pytest.mark.anyio
async def test_guessing_is_cut_off_after_a_handful_of_tries(linking):
    """Eight characters is a large space, but nothing was stopping a script
    from walking it."""
    client, _, _ = linking
    pending = await start_linking(client)

    refusals = [
        (await client.post("/login", data={"pending": pending, "code": f"WRONG{i:03d}"})).text for i in range(8)
    ]
    assert any("too many" in text.lower() for text in refusals), "guessing was never throttled"


@pytest.mark.anyio
async def test_a_throttled_client_cannot_use_the_right_code_either(linking):
    """Otherwise the limit is decorative: keep guessing until one lands."""
    client, _, code = linking
    pending = await start_linking(client)

    for i in range(8):
        await client.post("/login", data={"pending": pending, "code": f"WRONG{i:03d}"})

    blocked = await client.post("/login", data={"pending": pending, "code": code})
    assert blocked.status_code == 200
    assert "too many" in blocked.text.lower()


@pytest.mark.anyio
async def test_a_good_code_still_works_when_nobody_has_been_guessing(linking):
    client, _, code = linking
    pending = await start_linking(client)

    linked = await client.post("/login", data={"pending": pending, "code": code})
    assert linked.status_code == 302


def test_an_invite_code_never_reaches_the_ledger_view(tmp_path, monkeypatch):
    """The balance sheet goes to a screen in a shared room."""
    import json

    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "p")
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="p")
    code = store.add_member(flat, flat.add_member("Chris"))
    store.add_entry(flat, flat.record_expense(payer_id=flat.members[0].id, total=1000, description="x"))

    from tally.server import LEDGER_URI, create_server

    server = create_server(store)
    import anyio

    async def read() -> str:
        contents = await server.read_resource(LEDGER_URI)
        return "".join(c.content for c in contents)

    payload = anyio.run(read)
    assert code not in payload
    assert "invite" not in json.loads(payload)


# -- every string that reaches a browser ---------------------------------


def test_no_free_text_from_an_expense_reaches_the_screen(tmp_path, monkeypatch):
    """Descriptions are the least controlled field there is. They belong in the
    spoken answer, not in the markup the app builds."""
    import json

    import anyio

    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "p")
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="p")
    store.add_member(flat, flat.add_member("Chris"))
    store.add_entry(
        flat,
        flat.record_expense(payer_id=flat.members[0].id, total=1000, description="<img src=x onerror=alert(1)>"),
    )

    from tally.server import LEDGER_URI, create_server

    server = create_server(store)

    async def read() -> str:
        return "".join(c.content for c in await server.read_resource(LEDGER_URI))

    payload = json.loads(anyio.run(read))
    assert "onerror" not in json.dumps(payload)


@pytest.mark.anyio
async def test_the_linking_page_escapes_both_of_its_fields(linking):
    client, _, _ = linking
    pending = await start_linking(client)

    reflected = await client.post("/login", data={"pending": pending, "code": ""})
    assert "<script" not in reflected.text

    injected = await client.post("/login", data={"pending": '"><script>alert(1)</script>', "code": "x"})
    assert "<script>alert(1)</script>" not in injected.text


@pytest.mark.anyio
async def test_a_failed_sign_in_gives_the_code_back(tmp_path):
    """The code is burned before the OAuth step finishes. If that step fails the
    member is claimed by an identity that never got a token, and the person it
    was meant for can never join."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    from tally.auth import TallyAuthProvider
    from tally.login import make_login_route

    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="tally:sam")
    code = store.add_member(flat, flat.add_member("Chris"))

    class LinkThatExpiresMidway(TallyAuthProvider):
        def complete_login(self, pending_id: str, subject: str) -> str:
            raise KeyError("gone")

    provider = LinkThatExpiresMidway(str(tmp_path / "tally.db"))
    app = Starlette(routes=[Route("/login", make_login_route(provider, store), methods=["GET", "POST"])])

    pending = await provider.authorize(_client(), _params())
    pending_id = pending.split("pending=")[1]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        failed = await client.post("/login", data={"pending": pending_id, "code": code})
    assert failed.status_code == 400

    assert store.claim_member(code, "tally:chris") is not None, "the code was burned for nothing"


def _client():
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(client_id="c", redirect_uris=["https://example.com/cb"])


def _params():
    from mcp.server.auth.provider import AuthorizationParams
    from pydantic import AnyUrl

    return AuthorizationParams(
        state=None,
        scopes=["ledger"],
        code_challenge="x",
        redirect_uri=AnyUrl("https://example.com/cb"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def test_someone_already_in_a_household_is_refused_not_crashed(tmp_path):
    """One principal, one household is enforced by an index. Redeeming a second
    invite under the same identity hit it as an unhandled IntegrityError."""
    store = Store(tmp_path / "tally.db")
    first = store.create_household("Apartment 4B", "USD", founder="Sam", principal="tally:sam")
    store.add_member(first, first.add_member("Chris"))
    second = store.create_household("Apartment 9", "USD", founder="Dana", principal="tally:dana")
    code = store.add_member(second, second.add_member("Maya"))

    assert store.claim_member(code, "tally:sam") is None
