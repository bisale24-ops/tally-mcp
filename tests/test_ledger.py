"""Invariants for the ledger. The headline law: a shared ledger always sums to zero."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tally.ledger import Household, settle


def household(*names: str, currency: str = "USD") -> Household:
    h = Household(id="h", name="Apartment 4B", currency=currency)
    for name in names:
        h.add_member(name)
    return h


# -- structural laws ----------------------------------------------------


def test_balances_always_sum_to_zero():
    h = household("Ann", "Bob", "Cy")
    ids = [m.id for m in h.members]
    h.record_expense(payer_id=ids[0], total=4200, description="dinner")
    h.record_expense(payer_id=ids[1], total=1501, description="taxi", participant_ids=ids[:2])
    h.record_transfer(from_id=ids[2], to_id=ids[0], amount=700)
    assert sum(h.balances().values()) == 0


@given(
    payers=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=25),
    totals=st.lists(st.integers(min_value=1, max_value=500_000), min_size=25, max_size=25),
)
@settings(max_examples=100)
def test_balances_sum_to_zero_for_any_history(payers, totals):
    """No sequence of expenses can create or destroy money."""
    h = household("Ann", "Bob", "Cy", "Dee")
    ids = [m.id for m in h.members]
    for i, payer in enumerate(payers):
        h.record_expense(payer_id=ids[payer], total=totals[i], description=f"e{i}")
    assert sum(h.balances().values()) == 0


@given(
    payers=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=20),
    totals=st.lists(st.integers(min_value=1, max_value=500_000), min_size=20, max_size=20),
)
@settings(max_examples=100)
def test_settlement_actually_clears_the_ledger(payers, totals):
    """Applying every suggested transfer must leave nobody owing anything."""
    h = household("Ann", "Bob", "Cy", "Dee")
    ids = [m.id for m in h.members]
    for i, payer in enumerate(payers):
        h.record_expense(payer_id=ids[payer], total=totals[i], description=f"e{i}")

    balances = h.balances()
    for transfer in settle(balances):
        balances[transfer.from_id] += transfer.amount
        balances[transfer.to_id] -= transfer.amount
    assert all(v == 0 for v in balances.values())


@given(
    payers=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=20),
    totals=st.lists(st.integers(min_value=1, max_value=500_000), min_size=20, max_size=20),
)
@settings(max_examples=100)
def test_settlement_never_needs_more_than_n_minus_one_transfers(payers, totals):
    """The whole point of settling up is fewer payments than paying everyone back."""
    h = household("Ann", "Bob", "Cy", "Dee", "Eve")
    ids = [m.id for m in h.members]
    for i, payer in enumerate(payers):
        h.record_expense(payer_id=ids[payer], total=totals[i], description=f"e{i}")
    assert len(settle(h.balances())) <= len(h.members) - 1


def test_settling_a_clean_ledger_asks_for_no_payments():
    h = household("Ann", "Bob")
    assert settle(h.balances()) == []


def test_nobody_is_asked_to_pay_themselves():
    h = household("Ann", "Bob", "Cy")
    ids = [m.id for m in h.members]
    h.record_expense(payer_id=ids[0], total=999, description="melon")
    assert all(t.from_id != t.to_id for t in settle(h.balances()))


# -- recording ----------------------------------------------------------


def test_an_expense_only_charges_its_participants():
    h = household("Ann", "Bob", "Cy")
    ann, bob, cy = (m.id for m in h.members)
    h.record_expense(payer_id=ann, total=1000, description="two-person taxi", participant_ids=[ann, bob])
    assert h.balances()[cy] == 0


def test_exact_shares_must_add_up_to_the_total():
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    with pytest.raises(ValueError):
        h.record_expense(payer_id=ann, total=1000, description="x", exact={ann: 400, bob: 400})


def test_weighted_split_charges_in_proportion():
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    h.record_expense(payer_id=ann, total=3000, description="rent", weights={ann: 2, bob: 1})
    assert h.balances()[bob] == -1000


def test_an_expense_must_be_positive():
    h = household("Ann", "Bob")
    ann = h.members[0].id
    with pytest.raises(ValueError):
        h.record_expense(payer_id=ann, total=0, description="nothing")


def test_an_unknown_payer_is_rejected():
    h = household("Ann")
    with pytest.raises(ValueError):
        h.record_expense(payer_id="mem_nope", total=100, description="x")


def test_a_transfer_needs_two_different_people():
    h = household("Ann", "Bob")
    ann = h.members[0].id
    with pytest.raises(ValueError):
        h.record_transfer(from_id=ann, to_id=ann, amount=100)


# -- undo, which voice input makes mandatory ----------------------------


def test_undo_reverses_the_balance_it_created():
    h = household("Ann", "Bob")
    ann = h.members[0].id
    before = h.balances()
    entry = h.record_expense(payer_id=ann, total=2500, description="misheard")
    assert h.balances() != before
    h.void(entry.id)
    assert h.balances() == before


def test_undo_keeps_the_entry_in_the_history():
    h = household("Ann", "Bob")
    entry = h.record_expense(payer_id=h.members[0].id, total=2500, description="misheard")
    h.void(entry.id)
    assert len(h.entries) == 1 and h.entries[0].voided


def test_an_entry_cannot_be_undone_twice():
    h = household("Ann", "Bob")
    entry = h.record_expense(payer_id=h.members[0].id, total=100, description="x")
    h.void(entry.id)
    with pytest.raises(ValueError):
        h.void(entry.id)


def test_last_entry_skips_what_was_undone():
    h = household("Ann", "Bob")
    first = h.record_expense(payer_id=h.members[0].id, total=100, description="first")
    second = h.record_expense(payer_id=h.members[0].id, total=200, description="second")
    h.void(second.id)
    assert h.last_entry().id == first.id


# -- resolving names, which is where voice actually breaks --------------


def test_a_member_answers_to_an_alias():
    h = Household(id="h", name="Apartment 4B", currency="USD")
    h.add_member("Samantha", aliases=("Sam", "Sammy"))
    assert h.find_member("sam").name == "Samantha"


def test_an_unambiguous_prefix_resolves():
    h = household("Samantha", "Chris")
    assert h.find_member("Saman").name == "Samantha"


def test_an_ambiguous_prefix_refuses_to_guess():
    """Guessing here misattributes somebody's money. Better to ask."""
    h = household("Anna", "Andrew")
    assert h.find_member("An") is None


def test_a_duplicate_name_is_rejected():
    h = household("Ann")
    with pytest.raises(ValueError):
        h.add_member("Ann")


# -- pairwise debt -------------------------------------------------------


def test_pairwise_debt_is_antisymmetric():
    h = household("Ann", "Bob", "Cy")
    ann, bob = h.members[0].id, h.members[1].id
    h.record_expense(payer_id=ann, total=3000, description="dinner")
    h.record_expense(payer_id=bob, total=1200, description="coffee", participant_ids=[ann, bob])
    assert h.owed_between(bob, ann) == -h.owed_between(ann, bob)


def test_pairwise_debt_ignores_expenses_the_pair_had_no_part_in():
    h = household("Ann", "Bob", "Cy")
    ann, bob, cy = (m.id for m in h.members)
    h.record_expense(payer_id=cy, total=900, description="just us two", participant_ids=[cy, bob])
    assert h.owed_between(ann, bob) == 0


def test_pairwise_debt_is_not_the_settlement_plan():
    """Cy owes Ann nothing directly, yet the group plan may route a payment there."""
    h = household("Ann", "Bob", "Cy")
    ann, bob, cy = (m.id for m in h.members)
    h.record_expense(payer_id=ann, total=1000, description="x", participant_ids=[ann, bob])
    h.record_expense(payer_id=bob, total=1000, description="y", participant_ids=[bob, cy])
    assert h.owed_between(cy, ann) == 0
    assert h.owed_between(cy, bob) == 500


def test_an_undone_expense_leaves_no_pairwise_debt():
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    entry = h.record_expense(payer_id=ann, total=1000, description="misheard")
    h.void(entry.id)
    assert h.owed_between(bob, ann) == 0
