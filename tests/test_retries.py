"""Alexa+ retries over Streamable HTTP. A retried write must not charge twice."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE

from tally.server import create_server
from tally.store import Store

APPS = "io.modelcontextprotocol/ui"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "retry-user")
    return Store(tmp_path / "tally.db")


def connected(store):
    return Client(create_server(store), extensions=[advertise(APPS, {"mimeTypes": [APP_MIME_TYPE]})])


def said(result) -> str:
    return " ".join(b.text for b in result.content if b.type == "text")


def balances_of(result) -> dict[str, int]:
    return {row["name"]: row["minor"] for row in result.structured_content["balances"]}


async def furnished(client) -> None:
    await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
    await client.call_tool("add_person", {"name": "Chris"})


@pytest.mark.anyio
async def test_a_resent_expense_is_recorded_once(store):
    """The transport retried; the user said it once."""
    async with connected(store) as client:
        await furnished(client)
        args = {"amount": "60.00", "description": "groceries", "idempotency_key": "utterance-1"}

        first = await client.call_tool("record_expense", args)
        second = await client.call_tool("record_expense", args)

        assert said(first) == said(second)
        assert balances_of(second) == {"Sam": 3000, "Chris": -3000}
        history = await client.call_tool("recent_activity", {"limit": 10})
        assert said(history).count("groceries") == 1


@pytest.mark.anyio
async def test_a_resent_expense_without_a_key_is_still_recorded_once(store):
    """Alexa+ replays the same arguments; it does not invent an idempotency key."""
    async with connected(store) as client:
        await furnished(client)
        args = {"amount": "60.00", "description": "groceries"}

        first = await client.call_tool("record_expense", args)
        second = await client.call_tool("record_expense", args)

        assert said(first) == said(second)
        assert balances_of(second) == {"Sam": 3000, "Chris": -3000}


@pytest.mark.anyio
async def test_two_genuinely_different_expenses_both_land(store):
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})
        await client.call_tool("record_expense", {"amount": "60.00", "description": "beer"})
        assert balances_of(await client.call_tool("show_balances", {})) == {"Sam": 6000, "Chris": -6000}


@pytest.mark.anyio
async def test_a_resent_settle_up_is_recorded_once(store):
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})
        args = {"amount": "30.00", "to": "Sam", "paid_by": "Chris", "idempotency_key": "utterance-2"}

        first = await client.call_tool("settle_up", args)
        second = await client.call_tool("settle_up", args)

        assert said(first) == said(second)
        assert balances_of(second) == {"Sam": 0, "Chris": 0}


@pytest.mark.anyio
async def test_a_resent_undo_does_not_reach_the_entry_before_it(store):
    """The dangerous one: a retried undo eats a second, unrelated expense."""
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "10.00", "description": "coffee"})
        await client.call_tool("record_expense", {"amount": "90.00", "description": "misheard"})

        first = await client.call_tool("undo_last", {"idempotency_key": "utterance-3"})
        second = await client.call_tool("undo_last", {"idempotency_key": "utterance-3"})

        assert said(first) == said(second)
        history = await client.call_tool("recent_activity", {"limit": 10})
        assert "coffee" in said(history)


@pytest.mark.anyio
async def test_a_retry_racing_the_original_still_records_once(store):
    """Both copies arrive before either has finished writing."""
    server = create_server(store)
    async with connected(store) as setup:
        await furnished(setup)

    results: list[str] = []

    async def send() -> None:
        async with Client(server, extensions=[advertise(APPS, {"mimeTypes": [APP_MIME_TYPE]})]) as client:
            result = await client.call_tool(
                "record_expense",
                {"amount": "60.00", "description": "groceries", "idempotency_key": "utterance-4"},
            )
            results.append(said(result))

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(send)

    assert len(set(results)) == 1, "the retries disagreed about what happened"
    async with connected(store) as client:
        assert balances_of(await client.call_tool("show_balances", {})) == {"Sam": 3000, "Chris": -3000}


@pytest.mark.anyio
async def test_writing_tools_do_not_claim_to_be_idempotent(store):
    """The dedup window is a safety net, not a promise a host may rely on."""
    async with connected(store) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("record_expense", "settle_up", "undo_last", "add_person", "start_household"):
            assert tools[name].annotations.idempotent_hint is not True, name


@pytest.mark.anyio
async def test_a_resent_undo_without_a_key_does_not_reach_the_entry_before_it(store):
    """Alexa resends the arguments it had, which for undo is nothing at all.
    Keying on "whatever is last" makes the second copy eat the entry before."""
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "10.00", "description": "coffee"})
        await client.call_tool("record_expense", {"amount": "90.00", "description": "misheard"})

        first = await client.call_tool("undo_last", {})
        second = await client.call_tool("undo_last", {})

        assert said(first) == said(second)
        history = await client.call_tool("recent_activity", {"limit": 10})
        assert "coffee" in said(history)


@pytest.mark.anyio
async def test_a_retry_landing_in_the_next_minute_is_still_one_expense(store, monkeypatch):
    """The bucket is a boundary, and a resend two seconds later can fall the
    other side of it."""
    clock = [60 * 16667 - 1.0]
    monkeypatch.setattr("tally.server.time.time", lambda: clock[0])

    async with connected(store) as client:
        await furnished(client)
        args = {"amount": "60.00", "description": "groceries"}

        first = await client.call_tool("record_expense", args)
        clock[0] += 2.0
        second = await client.call_tool("record_expense", args)

        assert said(first) == said(second)
        assert balances_of(second) == {"Sam": 3000, "Chris": -3000}
