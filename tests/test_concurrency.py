"""Two flatmates talking at once.

Alexa sits in a shared room, so two people recording something in the same
second is the normal case, not the exotic one. The README claims WAL makes that
safe, which means it has to be shown rather than asserted.
"""

from __future__ import annotations

import contextlib
import sqlite3

import anyio
import pytest

from tally.store import Store

WRITERS = 24


@pytest.fixture
def furnished(tmp_path):
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="p")
    for name in ("Chris", "Maya", "Dana"):
        store.add_member(flat, flat.add_member(name))
    return store, flat


async def record_all(store: Store, household_id: str, count: int, *, total: int = 500) -> None:
    """Fire `count` expense writes at the same household concurrently."""

    async def record(index: int) -> None:
        def write() -> None:
            live = store.load(household_id)
            entry = live.record_expense(
                payer_id=live.members[index % len(live.members)].id,
                total=total + index,
                description=f"expense {index}",
            )
            store.add_entry(live, entry)

        await anyio.to_thread.run_sync(write)

    async with anyio.create_task_group() as tg:
        for i in range(count):
            tg.start_soon(record, i)


@pytest.mark.anyio
async def test_simultaneous_expenses_are_all_recorded(furnished):
    store, flat = furnished
    await record_all(store, flat.id, WRITERS)

    final = store.load(flat.id)
    assert len(final.entries) == WRITERS, "a concurrent write was lost"
    assert {e.description for e in final.entries} == {f"expense {i}" for i in range(WRITERS)}


@pytest.mark.anyio
async def test_simultaneous_expenses_keep_the_ledger_balanced(furnished):
    store, flat = furnished
    await record_all(store, flat.id, WRITERS, total=1000)
    assert sum(store.load(flat.id).balances().values()) == 0


@pytest.mark.anyio
async def test_simultaneous_writes_get_distinct_positions(furnished):
    """Order decides what "undo the last one" means, so two entries must never
    claim the same place in the history."""
    store, flat = furnished
    await record_all(store, flat.id, WRITERS)

    with store._connect() as conn:
        positions = [
            row["position"] for row in conn.execute("SELECT position FROM entries WHERE household_id = ?", (flat.id,))
        ]
    assert sorted(positions) == list(range(WRITERS)), f"positions collided: {sorted(positions)}"


@pytest.mark.anyio
async def test_simultaneous_members_are_all_added(furnished):
    store, flat = furnished

    async def add(index: int) -> None:
        def write() -> None:
            live = store.load(flat.id)
            store.add_member(live, live.add_member(f"Guest {index}"))

        await anyio.to_thread.run_sync(write)

    async with anyio.create_task_group() as tg:
        for i in range(12):
            tg.start_soon(add, i)

    names = {m.name for m in store.load(flat.id).members}
    assert {f"Guest {i}" for i in range(12)} <= names


@pytest.mark.anyio
async def test_a_thousand_entries_stay_well_inside_the_latency_budget(tmp_path, monkeypatch):
    """Alexa+ allows 500 ms. Reads load the whole household, so this is the
    guard on that choice; at ten thousand entries p95 is around 120 ms."""
    import statistics
    import time

    from mcp import Client
    from mcp.client.extension import advertise
    from mcp.server.apps import APP_MIME_TYPE

    from tally.server import create_server

    monkeypatch.setenv("TALLY_DEV_PRINCIPAL", "perf")
    store = Store(tmp_path / "tally.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="perf")
    for name in ("Chris", "Maya", "Dana"):
        store.add_member(flat, flat.add_member(name))
    for i in range(1000):
        store.add_entry(
            flat,
            flat.record_expense(payer_id=flat.members[i % 4].id, total=100 + i, description=f"e{i}"),
        )

    client = Client(
        create_server(store),
        extensions=[advertise("io.modelcontextprotocol/ui", {"mimeTypes": [APP_MIME_TYPE]})],
    )
    async with client:
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            await client.call_tool("show_balances", {})
            samples.append((time.perf_counter() - started) * 1000)

    samples.sort()
    assert samples[int(len(samples) * 0.95) - 1] < 500, statistics.median(samples)


@pytest.mark.anyio
async def test_the_same_person_cannot_be_added_twice_at_once(furnished):
    """The duplicate check reads the household first, so two devices adding
    "Robin" in the same moment both saw a household without one."""
    store, flat = furnished

    # Everyone reads, and builds their Robin, before anyone writes - which is
    # what two devices in one room actually do.
    pending = []
    for _ in range(10):
        live = store.load(flat.id)
        pending.append((live, live.add_member("Robin")))

    async def add(live, member) -> None:
        def write() -> None:
            with contextlib.suppress(ValueError, sqlite3.IntegrityError):
                store.add_member(live, member)

        await anyio.to_thread.run_sync(write)

    async with anyio.create_task_group() as tg:
        for live, member in pending:
            tg.start_soon(add, live, member)

    names = [m.name for m in store.load(flat.id).members]
    assert names.count("Robin") == 1, names
