"""Inputs that should be refused rather than half-understood.

Speech recognition hands the server whatever it thought it heard, and a shared
ledger that quietly accepts a wrong number is worse than one that says no.
"""

from __future__ import annotations

import anyio
import pytest

from tally.ledger import Household
from tally.money import MAX_MINOR, normalise_currency, parse_amount, split_by_weights
from tally.store import Store


def household(*names: str) -> Household:
    h = Household(id="h", name="Apartment 4B", currency="USD")
    for n in names:
        h.add_member(n)
    return h


# -- amounts that look numeric but are not -------------------------------


@pytest.mark.parametrize("text", ["1e10", "12abc34", "1x2", "12 dollars", "USD12", "12o5"])
def test_letters_never_get_stripped_out_of_an_amount(text):
    """Filtering non-digits turns "1e10" into 110 and "12abc34" into 1234 - a
    wrong number, accepted silently. Refusing is the only safe answer."""
    with pytest.raises(ValueError):
        parse_amount(text, "USD")


@pytest.mark.parametrize("text", ["", " ", "-", ".", "..", "12..3", "1,2,3", "--5", "1-2"])
def test_malformed_amounts_are_refused(text):
    with pytest.raises(ValueError):
        parse_amount(text, "USD")


def test_an_amount_beyond_the_ledger_is_refused_not_crashed():
    """SQLite stores this as a 64-bit integer; past that the driver raises
    OverflowError from inside the write, long after the tool said yes."""
    with pytest.raises(ValueError):
        parse_amount("9" * 25, "USD")


def test_the_largest_tracked_amount_still_parses():
    assert parse_amount(str(MAX_MINOR // 100), "USD") == MAX_MINOR


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused(value):
    with pytest.raises(ValueError):
        parse_amount(value, "USD")


# -- shares that cannot be divided ---------------------------------------


@pytest.mark.parametrize("weight", [float("nan"), float("inf")])
def test_a_non_finite_share_is_refused(weight):
    with pytest.raises(ValueError):
        split_by_weights(1000, {"a": weight, "b": 1})


def test_shares_that_overflow_when_summed_are_refused():
    """Two enormous floats sum to infinity, and every allocation becomes NaN."""
    with pytest.raises(ValueError):
        split_by_weights(1000, {"a": 1e308, "b": 1e308})


def test_absurdly_small_shares_still_conserve_the_total():
    allocations = split_by_weights(1000, {"a": 1e-320, "b": 1e-320})
    assert sum(a.amount for a in allocations) == 1000


# -- names ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["", " ", "\t", "\n  \n"])
def test_a_blank_name_is_refused(name):
    """An unnamed member can never be addressed again, and blocks the next one."""
    h = household()
    with pytest.raises(ValueError):
        h.add_member(name)


def test_an_absurdly_long_name_is_refused():
    h = household()
    with pytest.raises(ValueError):
        h.add_member("N" * 5000)


def test_a_name_is_stored_trimmed():
    h = household()
    assert h.add_member("  Sam  ").name == "Sam"


def test_blank_aliases_are_dropped():
    h = household()
    assert h.add_member("Chris", ("", "   ", "C")).aliases == ("C",)


def test_a_name_that_differs_only_by_spacing_is_a_duplicate():
    h = household("Sam")
    with pytest.raises(ValueError):
        h.add_member("  Sam ")


def test_unicode_names_survive_a_round_trip(tmp_path):
    store = Store(tmp_path / "t.db")
    flat = store.create_household("Квартира", "USD", founder="Александр")
    store.add_member(flat, flat.add_member("李雷"))
    store.add_member(flat, flat.add_member("Zoë"))
    reloaded = store.load(flat.id)
    assert {m.name for m in reloaded.members} == {"Александр", "李雷", "Zoë"}
    assert reloaded.find_member("zoë").name == "Zoë"


# -- currencies -----------------------------------------------------------


@pytest.mark.parametrize("code", ["US", "USDD", "12A", "", "  ", "$"])
def test_a_nonsense_currency_is_refused(code):
    """Stored unchecked, it would silently set the decimal places for every
    later amount in that household."""
    with pytest.raises(ValueError):
        normalise_currency(code)


@pytest.mark.parametrize(("code", "expected"), [("usd", "USD"), (" eur ", "EUR"), ("Gbp", "GBP")])
def test_currency_codes_are_normalised(code, expected):
    assert normalise_currency(code) == expected


def test_a_household_cannot_be_created_with_a_bad_currency(tmp_path):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.create_household("Apartment 4B", "dollars", founder="Sam")


@pytest.mark.parametrize("name", ["", "   "])
def test_a_household_needs_a_name(tmp_path, name):
    store = Store(tmp_path / "t.db")
    with pytest.raises(ValueError):
        store.create_household(name, "USD", founder="Sam")


# -- one person, one household -------------------------------------------


@pytest.mark.anyio
async def test_simultaneous_signups_create_one_household(tmp_path):
    """Both devices pass the "do you have one?" check before either writes."""
    store = Store(tmp_path / "t.db")
    made: list[str] = []

    async def sign_up(index: int) -> None:
        def write() -> None:
            try:
                if store.household_id_for_principal("same") is None:
                    store.create_household(f"H{index}", "USD", founder="Sam", principal="same")
                    made.append(str(index))
            except ValueError:
                pass

        await anyio.to_thread.run_sync(write)

    async with anyio.create_task_group() as tg:
        for i in range(10):
            tg.start_soon(sign_up, i)

    assert len(made) == 1
    assert store.household_id_for_principal("same") is not None


def test_a_rejected_signup_leaves_no_orphan_household(tmp_path):
    store = Store(tmp_path / "t.db")
    store.create_household("First", "USD", founder="Sam", principal="p")
    with pytest.raises(ValueError):
        store.create_household("Second", "USD", founder="Sam", principal="p")

    with store._connect() as conn:  # reaching past the API to check for a partial write
        households = conn.execute("SELECT COUNT(*) AS n FROM households").fetchone()["n"]
    assert households == 1


# -- the same hostility, through a real client ---------------------------

from mcp import Client  # noqa: E402
from mcp.client.extension import advertise  # noqa: E402
from mcp.server.apps import APP_MIME_TYPE  # noqa: E402

from tally.server import create_server  # noqa: E402


@pytest.fixture
def client_store(tmp_path, monkeypatch):
    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "hostile")
    return Store(tmp_path / "tally.db")


def connected(store):
    return Client(
        create_server(store),
        extensions=[advertise("io.modelcontextprotocol/ui", {"mimeTypes": [APP_MIME_TYPE]})],
    )


def said(result) -> str:
    return " ".join(b.text for b in result.content if b.type == "text")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"name": "X", "currency": "dollars", "your_name": "Sam"}, "currency"),
        ({"name": "   ", "currency": "USD", "your_name": "Sam"}, "needs a name"),
        ({"name": "X", "currency": "USD", "your_name": "  "}, "needs a name"),
    ],
)
@pytest.mark.anyio
async def test_a_bad_household_is_refused_not_crashed(client_store, args, expected):
    """These reached the client as a bare "Error executing tool" with a stack
    trace on the server, because the tool let a ValueError escape."""
    async with connected(client_store) as client:
        result = await client.call_tool("start_household", args)
        assert result.is_error
        assert expected in said(result)


@pytest.mark.anyio
async def test_an_expense_needs_something_to_call_it(client_store):
    """A blank description reads aloud as "Recorded 10 dollars for ,"."""
    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        result = await client.call_tool("record_expense", {"amount": "10", "description": "  "})
        assert result.is_error and "What was it for?" in said(result)


@pytest.mark.anyio
async def test_an_empty_split_list_does_not_silently_bill_everyone(client_store):
    """`if split_between:` treated [] as "not given" and charged the household."""
    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await client.call_tool("add_person", {"name": "Chris"})
        result = await client.call_tool("record_expense", {"amount": "10", "description": "lunch", "split_between": []})
        assert result.is_error and "sharing" in said(result)


@pytest.mark.anyio
async def test_an_expense_shared_with_nobody_says_so(client_store):
    """It moves no balance, and "split 1 ways" is not English."""
    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await client.call_tool("add_person", {"name": "Chris"})
        result = await client.call_tool(
            "record_expense", {"amount": "10", "description": "my own lunch", "split_between": ["Sam"]}
        )
        assert not result.is_error
        assert "Not shared with anyone" in said(result)
        assert "ways" not in said(result)


@pytest.mark.anyio
async def test_an_enormous_amount_is_refused_before_it_reaches_the_database(client_store):
    """Past 64 bits the driver raises OverflowError from inside the write."""
    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        result = await client.call_tool("record_expense", {"amount": "9" * 25, "description": "yacht"})
        assert result.is_error and "amount" in said(result)


# -- the visual app -------------------------------------------------------


def test_the_app_escapes_every_value_it_writes_as_html():
    """Member names and household names are user text that reaches a browser.

    Verified in a real host: a member called `<script>window.__pwned=1</script>`
    renders as text and runs nothing. This guards the code path that makes that
    true - the title and subtitle go in as `textContent`, and every value built
    into markup passes through `esc`.
    """
    from tally.ui import BALANCE_APP

    body = BALANCE_APP[BALANCE_APP.index("function render(") :]
    for field in ("b.name", "b.display", "t.from", "t.to", "t.display"):
        assert f"esc({field})" in body, f"{field} is interpolated unescaped"
    assert 'getElementById("title").textContent' in body
    assert 'getElementById("subtitle").textContent' in body
    assert ".innerHTML = html" in body
    # Nothing else may write markup.
    assert body.count(".innerHTML") == 1


# -- rendering ------------------------------------------------------------


def test_a_negative_amount_keeps_the_sign_in_front_of_the_symbol():
    from tally.money import show_amount

    assert show_amount(-4200, "USD") == "-$42.00"
    assert show_amount(4200, "USD") == "$42.00"


# -- aliases --------------------------------------------------------------


def test_an_alias_belonging_to_someone_else_is_refused():
    """First match wins, so the alias would either be dead or start answering
    for the wrong person."""
    h = household("Sam")
    with pytest.raises(ValueError):
        h.add_member("Chris", ("Sam",))


def test_an_alias_repeating_the_persons_own_name_is_just_dropped():
    h = household()
    assert h.add_member("Dana", ("Dana", "D")).aliases == ("D",)


def test_duplicate_aliases_are_collapsed():
    h = household()
    assert h.add_member("Dana", ("D", "D", "Dee")).aliases == ("D", "Dee")


# -- undoing the same thing twice -----------------------------------------


@pytest.mark.anyio
async def test_only_one_person_is_told_they_undid_it(tmp_path):
    """Everyone in the room can say "cancel that". A blind UPDATE told each of
    them it worked while a single entry was actually voided."""
    store = Store(tmp_path / "t.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="p")
    store.add_member(flat, flat.add_member("Chris"))
    store.add_entry(flat, flat.record_expense(payer_id=flat.members[0].id, total=1000, description="one"))

    confirmed: list[str] = []

    async def undo() -> None:
        def press() -> None:
            live = store.load(flat.id)
            last = live.last_entry()
            if last and store.void_entry(last.id)[1]:
                confirmed.append(last.id)

        await anyio.to_thread.run_sync(press)

    async with anyio.create_task_group() as tg:
        for _ in range(6):
            tg.start_soon(undo)

    assert len(confirmed) == 1
    assert not store.load(flat.id).last_entry()


# -- the authorization tables -------------------------------------------


@pytest.mark.anyio
async def test_expired_authorization_rows_do_not_pile_up(tmp_path):
    """Nothing in these tables outlives its TTL, so without a sweep they only grow."""
    import time

    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    from tally.auth import TallyAuthProvider

    provider = TallyAuthProvider(str(tmp_path / "auth.db"))
    with provider._db() as conn:  # seeding rows that expired long ago
        for i in range(50):
            conn.execute(
                "INSERT INTO oauth_pending (id, payload, expires_at) VALUES (?, ?, ?)",
                (f"stale{i}", "{}", time.time() - 3600),
            )

    client = OAuthClientInformationFull(client_id="c", redirect_uris=["https://example.com/cb"])
    await provider.register_client(client)
    await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=["ledger"],
            code_challenge="x",
            redirect_uri=AnyUrl("https://example.com/cb"),
            redirect_uri_provided_explicitly=True,
            resource=None,
        ),
    )

    with provider._db() as conn:
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM oauth_pending WHERE expires_at <= ?", (time.time(),)
        ).fetchone()["n"]
    assert stale == 0


# -- storage that is not answering ---------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("record_expense", {"amount": "10", "description": "lunch"}),
        ("settle_up", {"amount": "10", "to": "Chris"}),
        ("undo_last", {}),
        ("add_person", {"name": "Maya"}),
    ],
)
@pytest.mark.anyio
async def test_a_busy_database_is_reported_as_something_to_retry(tmp_path, monkeypatch, tool, args):
    """A locked or full database surfaced as an unexpected tool failure: a stack
    trace on the server and "Error executing tool" for the listener."""
    import sqlite3

    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "busy")
    path = tmp_path / "tally.db"
    store = Store(path)
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="busy")
    store.add_member(flat, flat.add_member("Chris"))
    store.add_entry(flat, flat.record_expense(payer_id=flat.members[0].id, total=1000, description="lunch"))

    blocker = sqlite3.connect(path, isolation_level=None, timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        async with connected(Store(path)) as client:
            result = await client.call_tool(tool, args)
            assert result.is_error
            assert "try that again" in said(result).lower()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


@pytest.mark.anyio
async def test_adding_the_same_person_twice_is_answered_not_crashed(client_store):
    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await client.call_tool("add_person", {"name": "Chris"})
        again = await client.call_tool("add_person", {"name": "Chris"})

        assert again.is_error
        assert "already" in said(again)
        roster = await client.call_tool("show_balances", {})
        names = [row["name"] for row in roster.structured_content["balances"]]
        assert names.count("Chris") == 1


@pytest.mark.anyio
async def test_a_read_that_fails_on_the_replay_path_is_still_speakable(client_store):
    """The load behind a replayed answer sat outside the guard that covers the
    write, so a database going away mid-call came back as a raw failure."""
    import sqlite3

    async with connected(client_store) as client:
        await client.call_tool("start_household", {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"})
        await client.call_tool("add_person", {"name": "Chris"})
        args = {"amount": "10.00", "description": "lunch", "idempotency_key": "same-utterance"}
        assert not (await client.call_tool("record_expense", args)).is_error

        real_load = client_store.load
        reads = {"count": 0}

        def load_until_the_replay(household_id):
            reads["count"] += 1
            if reads["count"] > 1:
                raise sqlite3.OperationalError("database is locked")
            return real_load(household_id)

        client_store.load = load_until_the_replay
        retried = await client.call_tool("record_expense", args)

    # Two reads means the first, inside current_household, went through and the
    # one behind the replayed answer is the one that failed.
    assert reads["count"] == 2
    assert retried.is_error
    assert "try that again" in said(retried).lower()
