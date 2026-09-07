"""End-to-end tests over a real MCP client session.

These exercise the wire: tool schemas, the Apps extension handshake, structured
content, elicitation, and error shape. Unit tests prove the maths; these prove
that a host talking MCP actually gets what we think we are sending.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE

from tally.server import BALANCE_URI, create_server
from tally.store import Store

APPS_EXTENSION = "io.modelcontextprotocol/ui"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "test-principal")
    return Store(tmp_path / "tally.db")


def screen_client(server):
    """A client that can render MCP Apps, like an Echo Show."""
    return Client(server, extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})])


def speaker_client(server):
    """A client with no screen, like an Echo Dot."""
    return Client(server)


def text_of(result) -> str:
    return " ".join(block.text for block in result.content if block.type == "text")


async def furnished(client) -> None:
    """A household with three people and one dinner already recorded."""
    await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Ann"})
    await client.call_tool("add_person", {"name": "Bob"})
    await client.call_tool("add_person", {"name": "Cy"})


# -- the conversation a person actually has -----------------------------


@pytest.mark.anyio
async def test_the_whole_flow_from_empty_to_settled(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)

        recorded = await client.call_tool("record_expense", {"amount": "42.00", "description": "dinner"})
        assert "42 dollars" in text_of(recorded)
        assert "14 dollars each" in text_of(recorded)

        balances = await client.call_tool("show_balances", {})
        assert "Bob" in text_of(balances) or "Cy" in text_of(balances)

        settled = await client.call_tool("settle_up", {"amount": "14.00", "to": "Ann", "paid_by": "Bob"})
        assert "Bob paid Ann 14 dollars" in text_of(settled)


@pytest.mark.anyio
async def test_an_expense_splits_only_between_the_named_people(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool(
            "record_expense",
            {"amount": "20.00", "description": "taxi", "split_between": ["Ann", "Bob"]},
        )
        assert "split 2 ways" in text_of(result)
        assert "10 dollars each" in text_of(result)


@pytest.mark.anyio
async def test_undo_reverses_the_last_entry_out_loud(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "99.00", "description": "misheard"})
        undone = await client.call_tool("undo_last", {})
        assert "Undone" in text_of(undone) and "99 dollars" in text_of(undone)

        # Undoing the only entry leaves an empty ledger, and the server says so
        # rather than claiming everyone is square - those are different facts.
        after = await client.call_tool("show_balances", {})
        assert "Nothing recorded" in text_of(after)


# -- the Apps extension, and degrading without it ------------------------


@pytest.mark.anyio
async def test_a_screen_client_receives_the_balance_sheet(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("record_expense", {"amount": "42.00", "description": "dinner"})

        assert result.structured_content is not None
        assert result.structured_content["household"] == "Apartment 4B"
        assert {row["name"] for row in result.structured_content["balances"]} == {"Ann", "Bob", "Cy"}
        assert result.structured_content["settle"][0]["from"] in {"Bob", "Cy"}


@pytest.mark.anyio
async def test_a_speaker_with_no_screen_still_gets_the_answer(store):
    """A device with no display must lose nothing but the picture."""
    async with speaker_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("record_expense", {"amount": "42.00", "description": "dinner"})

        assert "42 dollars" in text_of(result)
        assert "14 dollars each" in text_of(result)


@pytest.mark.anyio
async def test_the_structured_ledger_ships_to_every_client(store):
    """The tools publish an outputSchema, so structured content is not optional -
    what a screen changes is how much gets said out loud, not what is sent."""
    async with speaker_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("show_balances", {})
        assert result.structured_content is not None
        assert result.structured_content["household"] == "Apartment 4B"


@pytest.mark.anyio
async def test_the_tools_publish_their_output_schema(store):
    async with screen_client(create_server(store)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("record_expense", "show_balances", "settle_up", "undo_last"):
            schema = tools[name].output_schema
            assert schema is not None, name
            assert set(schema["properties"]) >= {"household", "summary", "balances", "settle"}


@pytest.mark.anyio
async def test_a_screen_shortens_what_is_said_aloud(store):
    """On a display the balance summary is redundant - it is already on screen.
    On a speaker it is the only way the user learns where things stand."""
    async with speaker_client(create_server(store)) as spoken_only:
        await furnished(spoken_only)
        await spoken_only.call_tool("record_expense", {"amount": "42.00", "description": "dinner"})
        heard = await spoken_only.call_tool("settle_up", {"amount": "14.00", "to": "Ann", "paid_by": "Bob"})

    async with screen_client(create_server(store)) as with_screen:
        seen = await with_screen.call_tool("settle_up", {"amount": "14.00", "to": "Ann", "paid_by": "Cy"})

    # Both confirm the payment; only the speaker-only reply reads the balances out.
    assert "paid Ann" in text_of(heard) and "paid Ann" in text_of(seen)
    assert "owes" in text_of(heard), "a speaker must hear where things stand"
    assert "owes" not in text_of(seen), "a screen already shows it"


@pytest.mark.anyio
async def test_the_ui_resource_is_served_as_an_mcp_app(store):
    async with screen_client(create_server(store)) as client:
        resources = await client.list_resources()
        app = next(r for r in resources.resources if str(r.uri) == BALANCE_URI)
        assert app.mime_type == APP_MIME_TYPE

        contents = await client.read_resource(BALANCE_URI)
        html = contents.contents[0].text
        assert "ui/initialize" in html and "ui/notifications/tool-result" in html


@pytest.mark.anyio
async def test_every_tool_that_changes_the_ledger_offers_the_balance_sheet(store):
    async with screen_client(create_server(store)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("record_expense", "show_balances", "settle_up", "undo_last"):
            assert (tools[name].meta or {})["ui"]["resourceUri"] == BALANCE_URI


@pytest.mark.anyio
async def test_read_only_tools_are_annotated_as_such(store):
    """Hosts use these hints to decide what needs confirming before it runs."""
    async with screen_client(create_server(store)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["show_balances"].annotations.read_only_hint is True
        assert tools["undo_last"].annotations.destructive_hint is True
        assert tools["record_expense"].annotations.read_only_hint is False


# -- what happens when speech goes wrong ---------------------------------


@pytest.mark.anyio
async def test_an_unknown_name_is_reported_not_guessed(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("record_expense", {"amount": "10.00", "description": "x", "paid_by": "Zebedee"})
        assert result.is_error
        assert "Zebedee" in text_of(result)
        assert "Ann, Bob, Cy" in text_of(result)


@pytest.mark.anyio
async def test_an_ambiguous_name_is_asked_about(store):
    """Two people match: the server must ask rather than pick one."""
    asked: list[str] = []

    async def answer_the_question(context, params):
        asked.append(params.message)
        from mcp_types import ElicitResult

        return ElicitResult(action="accept", content={"name": "Andrew"})

    server = create_server(store)
    # Elicitation is a server-initiated request, so it needs a transport with a
    # back-channel; the default in-process mode does not have one.
    async with Client(
        server,
        mode="legacy",
        extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})],
        elicitation_callback=answer_the_question,
    ) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Anna"})
        await client.call_tool("add_person", {"name": "Andrew"})

        result = await client.call_tool("record_expense", {"amount": "10.00", "description": "x", "paid_by": "An"})
        assert asked and "Anna" in asked[0] and "Andrew" in asked[0]
        assert not result.is_error
        assert "paid by Andrew" in text_of(result)


@pytest.mark.anyio
async def test_an_unreadable_amount_is_refused_clearly(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("record_expense", {"amount": "a bunch", "description": "x"})
        assert result.is_error and "amount" in text_of(result)


@pytest.mark.anyio
async def test_acting_without_a_household_explains_how_to_make_one(store):
    async with screen_client(create_server(store)) as client:
        result = await client.call_tool("show_balances", {})
        assert result.is_error and "start a household" in text_of(result)


@pytest.mark.anyio
async def test_undoing_nothing_says_so(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("undo_last", {})
        assert result.is_error and "nothing to undo" in text_of(result)


# -- persistence ---------------------------------------------------------


@pytest.mark.anyio
async def test_the_ledger_survives_a_new_session(store):
    """Alexa sessions are short; the ledger is not."""
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "30.00", "description": "groceries"})

    async with screen_client(create_server(store)) as client:
        result = await client.call_tool("recent_activity", {"limit": 5})
        assert "groceries" in text_of(result)


@pytest.mark.anyio
async def test_a_host_that_cannot_be_asked_still_gets_the_question(store):
    """Not every host supports elicitation. It must degrade to a usable answer,
    not to a server crash."""
    async with screen_client(create_server(store)) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Anna"})
        await client.call_tool("add_person", {"name": "Andrew"})

        result = await client.call_tool("record_expense", {"amount": "10.00", "description": "x", "paid_by": "An"})
        assert result.is_error
        assert "Anna" in text_of(result) and "Andrew" in text_of(result)


# -- the question people actually ask ------------------------------------


@pytest.mark.anyio
async def test_it_answers_what_one_person_owes_another(store):
    """ "How much do I owe Chris" is the commonest follow-up, and the group
    settlement plan does not answer it - that is a different number."""
    async with screen_client(create_server(store)) as client:
        await furnished(client)  # Ann (the speaker), Bob, Cy
        await client.call_tool("record_expense", {"amount": "30.00", "description": "dinner"})
        await client.call_tool(
            "record_expense",
            {"amount": "12.00", "description": "coffee", "paid_by": "Bob", "split_between": ["Ann", "Bob"]},
        )

        # Ann fronted 10 of Bob's dinner; Bob fronted 6 of Ann's coffee.
        owed = await client.call_tool("what_do_i_owe", {"person": "Bob"})
        assert "Bob owes you 4 dollars" in text_of(owed)


@pytest.mark.anyio
async def test_being_square_with_someone_is_said_plainly(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("what_do_i_owe", {"person": "Cy"})
        assert "square" in text_of(result)


@pytest.mark.anyio
async def test_it_answers_the_overall_position_when_no_one_is_named(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "30.00", "description": "dinner"})
        result = await client.call_tool("what_do_i_owe", {})
        assert "owed 20 dollars overall" in text_of(result)


@pytest.mark.anyio
async def test_asking_what_you_owe_yourself_is_refused(store):
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        result = await client.call_tool("what_do_i_owe", {"person": "Ann"})
        assert result.is_error


# -- resources and prompts -----------------------------------------------


@pytest.mark.anyio
async def test_the_ledger_is_readable_as_a_resource(store):
    """Reading where the household stands is not a side effect, so it is a
    resource as well as a tool."""
    async with screen_client(create_server(store)) as client:
        await furnished(client)
        await client.call_tool("record_expense", {"amount": "30.00", "description": "dinner"})

        listed = {str(r.uri) for r in (await client.list_resources()).resources}
        assert "tally://household" in listed

        contents = await client.read_resource("tally://household")
        payload = json.loads(contents.contents[0].text)
        assert payload["household"] == "Apartment 4B"
        assert {row["name"] for row in payload["balances"]} == {"Ann", "Bob", "Cy"}


@pytest.mark.anyio
async def test_the_ledger_resource_is_served_as_json(store):
    async with screen_client(create_server(store)) as client:
        resource = next(r for r in (await client.list_resources()).resources if str(r.uri) == "tally://household")
        assert resource.mime_type == "application/json"


@pytest.mark.anyio
async def test_it_offers_prompts_a_host_can_start_from(store):
    async with screen_client(create_server(store)) as client:
        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert {"settle_up_time", "weekly_review", "split_an_uneven_bill"} <= set(prompts)

        rendered = await client.get_prompt("split_an_uneven_bill", {"total": "80 dollars"})
        text = " ".join(m.content.text for m in rendered.messages if getattr(m.content, "type", None) == "text")
        assert "80 dollars" in text and "exact shares" in text


@pytest.mark.anyio
async def test_a_listening_host_is_told_when_the_ledger_moves(store):
    """A display on a shared device goes stale the moment someone else records
    something, unless the server says so."""
    server = create_server(store)
    async with screen_client(server) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Ann"})

        seen: list[str] = []
        async with (
            client.listen(resource_subscriptions=["tally://household"]) as sub,
            anyio.create_task_group() as tg,
        ):

            async def collect() -> None:
                async for event in sub:
                    seen.append(str(getattr(event, "uri", "")))
                    return

            tg.start_soon(collect)
            await anyio.sleep(0.05)
            await client.call_tool("record_expense", {"amount": "30.00", "description": "dinner"})
            with anyio.move_on_after(2):
                while not seen:
                    await anyio.sleep(0.02)
            tg.cancel_scope.cancel()

    assert seen and seen[0].rstrip("/") == "tally://household"


@pytest.mark.anyio
async def test_every_tool_carries_an_icon(store):
    """Hosts render these next to the tool name; a data URI needs no fetch,
    which matters for a page served under a no-network CSP."""
    async with screen_client(create_server(store)) as client:
        for tool in (await client.list_tools()).tools:
            assert tool.icons, tool.name
            assert tool.icons[0].src.startswith("data:image/svg+xml,")
