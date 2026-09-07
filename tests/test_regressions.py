"""Ground already covered once, re-checked from the angles that broke it."""

from __future__ import annotations

import unicodedata

import pytest
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE

from tally.ledger import Household
from tally.money import parse_amount
from tally.server import create_server
from tally.store import Store

APPS = "io.modelcontextprotocol/ui"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "regression-user")
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


# -- unicode --------------------------------------------------------------


def test_the_same_name_typed_two_ways_is_the_same_person():
    """ "Zoë" composed and decomposed are different byte strings and identical
    to a reader. Speech and keyboards produce both."""
    composed = unicodedata.normalize("NFC", "Zoë")
    decomposed = unicodedata.normalize("NFD", "Zoë")
    assert composed != decomposed

    h = Household(id="h", name="Apartment 4B", currency="USD")
    h.add_member(composed)
    assert h.find_member(decomposed) is not None
    with pytest.raises(ValueError):
        h.add_member(decomposed)


def test_a_decomposed_alias_resolves():
    h = Household(id="h", name="Apartment 4B", currency="USD")
    h.add_member("Zoe", (unicodedata.normalize("NFC", "Zoë"),))
    assert h.find_member(unicodedata.normalize("NFD", "Zoë")).name == "Zoe"


# -- the parser -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,000.50", 100050), ("$10", 1000), ("0.00", 0), ("0", 0), ("1 000", 100000)],
)
def test_amounts_a_person_would_say_or_type(text, expected):
    assert parse_amount(text, "USD") == expected


@pytest.mark.parametrize("text", ["10k", "ten fifty", "1.2.3", "0x10", "١٢٣"])
def test_amounts_that_are_not_numbers_are_refused(text):
    with pytest.raises(ValueError):
        parse_amount(text, "USD")


def test_the_ceiling_sits_below_what_sqlite_can_hold(tmp_path):
    """Anything the parser lets through has to survive the write."""
    from tally.money import MAX_MINOR

    assert MAX_MINOR < 2**63 - 1
    store = Store(tmp_path / "t.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="p")
    store.add_member(flat, flat.add_member("Chris"))
    store.add_entry(flat, flat.record_expense(payer_id=flat.members[0].id, total=MAX_MINOR, description="x"))
    assert store.load(flat.id).entries[0].total == MAX_MINOR


# -- settling -------------------------------------------------------------


@pytest.mark.anyio
async def test_paying_back_more_than_you_owe_turns_the_debt_around(store):
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "20.00", "description": "lunch"})
        over = await client.call_tool("settle_up", {"amount": "50.00", "to": "Sam", "paid_by": "Chris"})
        assert balances_of(over) == {"Sam": -4000, "Chris": 4000}


@pytest.mark.anyio
async def test_paying_yourself_back_is_refused(store):
    async with connected(store) as client:
        await furnished(client)
        result = await client.call_tool("settle_up", {"amount": "5.00", "to": "Sam", "paid_by": "Sam"})
        assert result.is_error


@pytest.mark.anyio
async def test_settling_nothing_is_refused(store):
    async with connected(store) as client:
        await furnished(client)
        result = await client.call_tool("settle_up", {"amount": "0", "to": "Chris"})
        assert result.is_error


@pytest.mark.anyio
async def test_settling_up_with_nothing_owed_still_records_the_payment(store):
    """People hand each other money for reasons the ledger did not see."""
    async with connected(store) as client:
        await furnished(client)
        paid = await client.call_tool("settle_up", {"amount": "5.00", "to": "Chris"})
        assert balances_of(paid) == {"Sam": 500, "Chris": -500}


# -- undoing --------------------------------------------------------------


@pytest.mark.anyio
async def test_undo_walks_back_through_the_history_one_at_a_time(store):
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "10.00", "description": "coffee"})
        await client.call_tool("record_expense", {"amount": "20.00", "description": "lunch"})

        first = await client.call_tool("undo_last", {"idempotency_key": "a"})
        second = await client.call_tool("undo_last", {"idempotency_key": "b"})
        assert "lunch" in said(first)
        assert "coffee" in said(second)
        assert balances_of(second) == {"Sam": 0, "Chris": 0}


@pytest.mark.anyio
async def test_undo_after_someone_elses_entry_takes_theirs(store):
    """ "The last one" means the ledger's last, not yours - and that is what a
    shared device should mean."""
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "10.00", "description": "mine"})
        await client.call_tool("record_expense", {"amount": "20.00", "description": "theirs", "paid_by": "Chris"})
        undone = await client.call_tool("undo_last", {})
        assert "theirs" in said(undone)


# -- starting a household -------------------------------------------------


@pytest.mark.anyio
async def test_starting_a_second_household_is_refused_clearly(store):
    async with connected(store) as client:
        await furnished(client)
        again = await client.call_tool("start_household", {"name": "Tahoe trip", "currency": "USD", "your_name": "Sam"})
        assert again.is_error
        assert "already" in said(again)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,000", 100000),
        ("1,200", 120000),
        ("12,500", 1250000),
        ("12,50", 1250),
        ("1,200.75", 120075),
        ("0,99", 99),
    ],
)
def test_a_comma_group_of_three_is_not_a_decimal_point(text, expected):
    """ "a thousand two hundred" comes back as "1,200" and was being read as
    one dollar twenty."""
    assert parse_amount(text, "USD") == expected


@pytest.mark.anyio
async def test_an_uneven_split_is_not_announced_as_an_equal_one(store):
    """Ten dollars three ways is 3.34 and 3.33 and 3.33. Saying "3.34 each"
    is a number nobody owes."""
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("add_person", {"name": "Maya"})
        result = await client.call_tool("record_expense", {"amount": "10.00", "description": "lunch"})
        assert "each" not in said(result)


@pytest.mark.anyio
async def test_an_even_split_still_says_each(store):
    async with connected(store) as client:
        await furnished(client)
        result = await client.call_tool("record_expense", {"amount": "10.00", "description": "lunch"})
        assert "5 dollars each" in said(result)


@pytest.mark.anyio
async def test_a_bill_can_be_recorded_with_the_shares_people_actually_owe(store):
    """The uneven-bill prompt tells the model to record exact shares, so the
    tool has to accept them."""
    async with connected(store) as client:
        await furnished(client)
        await client.call_tool("add_person", {"name": "Maya"})
        result = await client.call_tool(
            "record_expense",
            {
                "amount": "30.00",
                "description": "dinner",
                "shares": {"Sam": "5.00", "Chris": "10.00", "Maya": "15.00"},
            },
        )
        assert not result.is_error, said(result)
        assert balances_of(result) == {"Sam": 2500, "Chris": -1000, "Maya": -1500}


@pytest.mark.anyio
async def test_shares_that_do_not_add_up_are_refused(store):
    async with connected(store) as client:
        await furnished(client)
        result = await client.call_tool(
            "record_expense",
            {"amount": "30.00", "description": "dinner", "shares": {"Sam": "5.00", "Chris": "10.00"}},
        )
        assert result.is_error
