"""Adversarial cases: malformed input, precision, and things voice input will actually do."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tally.ledger import Household, settle
from tally.money import split_by_weights


def household(*names, currency="USD"):
    h = Household(id="h", name="Apartment 4B", currency=currency)
    for n in names:
        h.add_member(n)
    return h


def test_a_member_named_twice_in_one_split_is_not_charged_twice():
    """'split between me, Bob and Bob' must not make Bob pay two shares."""
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    h.record_expense(payer_id=ann, total=900, description="x", participant_ids=[ann, bob, bob])
    assert h.balances()[bob] == -450


def test_an_explicitly_empty_participant_list_is_an_error_not_everyone():
    """Charging the whole household when told 'nobody' is a silent money bug."""
    h = household("Ann", "Bob")
    with pytest.raises(ValueError):
        h.record_expense(payer_id=h.members[0].id, total=100, description="x", participant_ids=[])


@given(
    total=st.integers(min_value=0, max_value=10**9),
    weights=st.dictionaries(
        st.text(min_size=1, max_size=4),
        st.floats(min_value=0.01, max_value=1000, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=200)
def test_fractional_weights_still_conserve_the_total(total, weights):
    """Percentages arrive as floats; float drift must not lose or invent money."""
    allocations = split_by_weights(total, weights)
    assert sum(a.amount for a in allocations) == total


@given(
    total=st.integers(min_value=1, max_value=10**9),
    weights=st.dictionaries(
        st.text(min_size=1, max_size=4),
        st.floats(min_value=0.01, max_value=1000, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=8,
    ),
)
@settings(max_examples=200)
def test_nobody_is_allocated_a_negative_share(total, weights):
    assert all(a.amount >= 0 for a in split_by_weights(total, weights))


def test_a_zero_weight_member_pays_nothing():
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    h.record_expense(payer_id=ann, total=1000, description="x", weights={ann: 1, bob: 0})
    assert h.balances()[bob] == 0


def test_exact_shares_may_not_be_negative():
    """A negative share would pay somebody to attend dinner."""
    h = household("Ann", "Bob")
    ann, bob = (m.id for m in h.members)
    with pytest.raises(ValueError):
        h.record_expense(payer_id=ann, total=1000, description="x", exact={ann: 1200, bob: -200})


def test_a_transfer_to_an_unknown_member_is_rejected():
    h = household("Ann")
    with pytest.raises(ValueError):
        h.record_transfer(from_id=h.members[0].id, to_id="mem_ghost", amount=100)


def test_settling_an_unbalanced_ledger_does_not_hang():
    """Defensive: settle() must terminate even on input that cannot sum to zero."""
    assert settle({"a": 100, "b": 100}) is not None


def test_the_smallest_possible_expense_is_representable():
    h = household("Ann", "Bob")
    ann = h.members[0].id
    h.record_expense(payer_id=ann, total=1, description="one cent")
    assert sum(h.balances().values()) == 0
    assert sorted(h.balances().values()) == [-1, 0] or sorted(h.balances().values()) == [0, 0]


def test_a_member_added_after_an_expense_owes_nothing_for_it():
    """Joining a flat share must not retroactively bill you for last month."""
    h = household("Ann", "Bob")
    h.record_expense(payer_id=h.members[0].id, total=1000, description="past")
    cy = h.add_member("Cy")
    assert h.balances()[cy.id] == 0
