"""Two people, one ledger.

This is the claim the whole product rests on: Alexa sits in the kitchen and
belongs to everyone in the apartment, so a ledger on it has to be genuinely
shared - not one person's account that others are guests in.
"""

from __future__ import annotations

import pytest
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE

from tally.server import create_server
from tally.store import Store

APPS_EXTENSION = "io.modelcontextprotocol/ui"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "tally.db")


def as_person(server, principal: str, monkeypatch):
    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", principal)
    return Client(server, extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})])


def text_of(result) -> str:
    return " ".join(b.text for b in result.content if b.type == "text")


def balances_of(result) -> dict[str, int]:
    return {row["name"]: row["minor"] for row in result.structured_content["balances"]}


def invite_for(store: Store, name: str) -> str:
    """The code the founder would read out to this person."""
    with store._connect() as conn:  # test-only introspection
        row = conn.execute(
            "SELECT invite_code FROM members WHERE name = ? AND invite_code IS NOT NULL", (name,)
        ).fetchone()
    assert row, f"no outstanding invite for {name}"
    return row["invite_code"]


@pytest.mark.anyio
async def test_a_second_person_joins_and_sees_the_same_ledger(store, monkeypatch):
    server = create_server(store)

    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})
        await sam.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})

    # Chris redeems the invite code, which is what the linking page does.
    assert store.claim_member(invite_for(store, "Chris"), "person:chris")

    async with as_person(server, "person:chris", monkeypatch) as chris:
        seen = await chris.call_tool("show_balances", {})
        assert seen.structured_content["household"] == "Apartment 4B"
        assert balances_of(seen) == {"Sam": 3000, "Chris": -3000}


@pytest.mark.anyio
async def test_i_means_a_different_person_for_each_speaker(store, monkeypatch):
    """The same words from two flatmates must charge two different people."""
    server = create_server(store)

    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})
        mine = await sam.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})
        assert "paid by Sam" in text_of(mine)

    assert store.claim_member(invite_for(store, "Chris"), "person:chris")

    async with as_person(server, "person:chris", monkeypatch) as chris:
        theirs = await chris.call_tool("record_expense", {"amount": "40.00", "description": "beer"})
        assert "paid by Chris" in text_of(theirs)
        # Sam is up 30 on groceries and down 20 on beer; Chris the reverse.
        assert balances_of(theirs) == {"Sam": 1000, "Chris": -1000}


@pytest.mark.anyio
async def test_either_flatmate_can_undo_a_misheard_entry(store, monkeypatch):
    """A shared device hears everyone, so anyone must be able to correct it."""
    server = create_server(store)

    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})
        await sam.call_tool("record_expense", {"amount": "800.00", "description": "misheard"})

    assert store.claim_member(invite_for(store, "Chris"), "person:chris")

    async with as_person(server, "person:chris", monkeypatch) as chris:
        undone = await chris.call_tool("undo_last", {})
        assert "800 dollars" in text_of(undone)
        assert balances_of(undone) == {"Sam": 0, "Chris": 0}


@pytest.mark.anyio
async def test_a_stranger_sees_nothing(store, monkeypatch):
    """Someone outside the household must not reach its ledger at all."""
    server = create_server(store)

    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})

    async with as_person(server, "person:nobody", monkeypatch) as stranger:
        blocked = await stranger.call_tool("show_balances", {})
        assert blocked.is_error
        assert "Apartment 4B" not in text_of(blocked)


# -- invite codes ---------------------------------------------------------


@pytest.mark.anyio
async def test_a_name_alone_does_not_get_you_into_a_household(store, monkeypatch):
    """Names are not secrets. Linking used to match on one, which let anyone who
    guessed a flatmate's first name attach their own device to that ledger."""
    server = create_server(store)
    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})

    assert store.claim_member("Chris", "person:stranger") is None
    assert store.household_id_for_principal("person:stranger") is None


@pytest.mark.anyio
async def test_an_invite_code_works_only_once(store, monkeypatch):
    server = create_server(store)
    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})

    code = invite_for(store, "Chris")
    assert store.claim_member(code, "person:chris")
    assert store.claim_member(code, "person:someone-else") is None


@pytest.mark.anyio
async def test_the_founder_hears_the_code_to_pass_on(store, monkeypatch):
    server = create_server(store)
    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        added = await sam.call_tool("add_person", {"name": "Chris"})

    spoken = text_of(added)
    code = invite_for(store, "Chris")
    assert " ".join(code) in spoken, "the code has to be readable out loud"


@pytest.mark.anyio
async def test_a_code_is_accepted_in_the_shape_a_person_would_type_it(store, monkeypatch):
    server = create_server(store)
    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})

    code = invite_for(store, "Chris")
    typed = f"  {code[:4].lower()}-{code[4:].lower()} "
    assert store.claim_member(typed, "person:chris")


@pytest.mark.anyio
async def test_two_households_cannot_see_each_other(store, monkeypatch):
    """One database, many families. Nothing may leak sideways."""
    server = create_server(store)

    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})
        await sam.call_tool("record_expense", {"amount": "60.00", "description": "groceries"})

    async with as_person(server, "person:dana", monkeypatch) as dana:
        await dana.call_tool("start_household", {"name": "Apartment 9", "currency": "EUR", "your_name": "Dana"})
        await dana.call_tool("add_person", {"name": "Maya"})
        await dana.call_tool("record_expense", {"amount": "10.00", "description": "beer"})

        seen = await dana.call_tool("show_balances", {})
        assert seen.structured_content["household"] == "Apartment 9"
        assert set(balances_of(seen)) == {"Dana", "Maya"}

        history = await dana.call_tool("recent_activity", {"limit": 10})
        assert "groceries" not in text_of(history)

        # And a name from the other household is simply unknown here.
        stranger = await dana.call_tool("what_do_i_owe", {"person": "Chris"})
        assert stranger.is_error


@pytest.mark.anyio
async def test_an_invite_from_one_household_does_not_open_another(store, monkeypatch):
    server = create_server(store)
    async with as_person(server, "person:sam", monkeypatch) as sam:
        await sam.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await sam.call_tool("add_person", {"name": "Chris"})
    async with as_person(server, "person:dana", monkeypatch) as dana:
        await dana.call_tool("start_household", {"name": "Apartment 9", "currency": "USD", "your_name": "Dana"})
        await dana.call_tool("add_person", {"name": "Maya"})

    chris_code = invite_for(store, "Chris")
    member = store.claim_member(chris_code, "person:newcomer")
    assert member is not None
    joined = store.household_id_for_principal("person:newcomer")
    assert store.load(joined).name == "Apartment 4B"
