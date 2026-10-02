"""The browser playground: sandboxes, the model router's guard rails, and the fallback."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))

import playground

TOOLS = [{"name": "show_balances", "description": "", "schema": {"type": "object", "properties": {}}}]


@pytest.mark.parametrize(
    ("said", "digits"),
    [
        ("a hundred and thirty two dollars", "132 dollars"),
        ("thirty four fifty for the Uber", "34.50 for the Uber"),
        ("forty four dollars", "44 dollars"),
        ("twelve", "12"),
        ("a pizza and a salad", "a pizza and a salad"),
        ("Sam paid 20 for lunch", "Sam paid 20 for lunch"),
    ],
)
def test_amounts_said_aloud_become_digits(said: str, digits: str) -> None:
    assert playground.words_to_digits(said) == digits


def test_fallback_keeps_the_speaker_in_a_split() -> None:
    routed = playground.route_with_patterns(
        "I paid a hundred and thirty two dollars for dinner, split with Chris and Maya"
    )
    (call,) = routed["calls"]
    assert call == {
        "tool": "record_expense",
        "arguments": {
            "paid_by": "me",
            "amount": "132",
            "description": "dinner",
            "split_between": ["Chris", "Maya", "me"],
        },
    }
    assert routed["router"] == "pattern fallback"


def test_fallback_reads_just_him_and_me() -> None:
    (call,) = playground.route_with_patterns("Chris paid thirty four fifty for the Uber home, just him and me")["calls"]
    assert call["arguments"]["split_between"] == ["Chris", "me"]
    assert call["arguments"]["amount"] == "34.50"


def test_fallback_admits_what_it_cannot_route() -> None:
    routed = playground.route_with_patterns("What's the weather like?")
    assert routed["calls"] == []
    assert "unreachable" in routed["text"]


def _fake_model(message: dict) -> object:
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return lambda request, timeout: Response(json.dumps({"choices": [{"message": message}]}).encode())


def test_a_tool_the_server_never_published_is_not_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground, "llm_key", lambda: "k")
    message = {
        "content": None,
        "tool_calls": [
            {"function": {"name": "transfer_money", "arguments": '{"amount": "500"}'}},
            {"function": {"name": "show_balances", "arguments": "{}"}},
            {"function": {"name": "show_balances", "arguments": "not json"}},
        ],
    }
    monkeypatch.setattr(playground.urllib.request, "urlopen", _fake_model(message))
    routed = playground.route_with_model("who owes what", TOOLS, ["Sam"])
    assert routed["calls"] == [{"tool": "show_balances", "arguments": {}}]


def test_the_model_never_sees_an_unset_tool_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forcing tool_choice makes this model write calls into its text; the request must leave it out."""
    seen = {}

    def capture(request, timeout):
        seen.update(json.loads(request.data))
        return _fake_model({"content": "ok"})(request, timeout)

    monkeypatch.setattr(playground, "llm_key", lambda: "k")
    monkeypatch.setattr(playground.urllib.request, "urlopen", capture)
    playground.route_with_model("hi", TOOLS, ["Sam"])
    assert "tool_choice" not in seen
    assert seen["tools"][0]["function"]["name"] == "show_balances"


@pytest.fixture
async def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(playground, "llm_key", lambda: None)  # no network: the fallback answers
    app = playground.build(tmp_path)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _say(client: httpx.AsyncClient, text: str) -> dict:
    response = await client.post("/say", json={"text": text})
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_a_conversation_runs_on_the_real_server(site: httpx.AsyncClient) -> None:
    await site.get("/")  # sets the sandbox cookie
    first = await _say(site, "I paid a hundred and thirty two dollars for dinner, split with Chris and Maya")
    assert first["results"][0]["error"] is False
    assert first["spoken"].startswith("Recorded 132 dollars for dinner")
    assert "split 3 ways" in first["spoken"]

    answer = await _say(site, "Who owes what?")
    owed = {row["name"]: row["minor"] for row in answer["ledger"]["balances"]}
    assert owed == {"Sam": 8800, "Chris": -4400, "Maya": -4400}
    assert sum(owed.values()) == 0


@pytest.mark.anyio
async def test_each_browser_gets_its_own_household(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playground, "llm_key", lambda: None)
    app = playground.build(tmp_path)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as alice,
            httpx.AsyncClient(transport=transport, base_url="http://test") as bob,
        ):
            await alice.get("/")
            await bob.get("/")
            await _say(alice, "I paid 90 for groceries")
            theirs = (await bob.get("/state")).json()["ledger"]
            assert all(row["minor"] == 0 for row in theirs["balances"])


@pytest.mark.anyio
async def test_reset_gives_a_fresh_household(site: httpx.AsyncClient) -> None:
    await site.get("/")
    await _say(site, "I paid 90 for groceries")
    await site.post("/reset")
    ledger = (await site.get("/state")).json()["ledger"]
    assert all(row["minor"] == 0 for row in ledger["balances"])


@pytest.mark.anyio
async def test_an_empty_sentence_is_refused(site: httpx.AsyncClient) -> None:
    response = await site.post("/say", json={"text": "   "})
    assert response.status_code == 400
