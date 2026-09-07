"""Invariants for the money layer. Money bugs are silent, so we assert laws, not examples."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tally.money import (
    format_amount,
    parse_amount,
    say_amount,
    show_amount,
    split_by_weights,
    split_equally,
)

CURRENCIES = st.sampled_from(["USD", "EUR", "GBP", "JPY", "KWD"])
TOTALS = st.integers(min_value=0, max_value=10**9)
MEMBERS = st.lists(st.text(min_size=1, max_size=6), min_size=1, max_size=12, unique=True)


@given(total=TOTALS, members=MEMBERS, seed=st.integers(min_value=0, max_value=50))
def test_equal_split_conserves_the_total(total, members, seed):
    """Nothing is created or destroyed by splitting."""
    allocations = split_equally(total, members, seed=seed)
    assert sum(a.amount for a in allocations) == total


@given(total=TOTALS, members=MEMBERS, seed=st.integers(min_value=0, max_value=50))
def test_equal_split_is_as_equal_as_integers_allow(total, members, seed):
    """No one pays more than one minor unit above anyone else."""
    amounts = [a.amount for a in split_equally(total, members, seed=seed)]
    assert max(amounts) - min(amounts) <= 1


@given(total=TOTALS, members=MEMBERS)
def test_equal_split_covers_every_member_exactly_once(total, members):
    allocations = split_equally(total, members)
    assert [a.member_id for a in allocations] == members


@given(
    total=TOTALS,
    weights=st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.integers(min_value=1, max_value=1000),
        min_size=1,
        max_size=10,
    ),
)
def test_weighted_split_conserves_the_total(total, weights):
    allocations = split_by_weights(total, weights)
    assert sum(a.amount for a in allocations) == total


@given(
    total=TOTALS,
    weights=st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.integers(min_value=1, max_value=1000),
        min_size=1,
        max_size=10,
    ),
)
def test_weighted_split_respects_the_ordering_of_weights(total, weights):
    """A bigger weight never pays less than a smaller one."""
    allocations = {a.member_id: a.amount for a in split_by_weights(total, weights)}
    for a in weights:
        for b in weights:
            if weights[a] > weights[b]:
                assert allocations[a] >= allocations[b]


@given(minor=st.integers(min_value=0, max_value=10**9), currency=CURRENCIES)
def test_format_then_parse_round_trips(minor, currency):
    assert parse_amount(format_amount(minor, currency), currency) == minor


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("12.50", "USD", 1250),
        ("12,50", "EUR", 1250),
        ("1,200.75", "USD", 120075),
        ("1 200,75", "USD", 120075),
        ("$12.50", "USD", 1250),
        ("500", "JPY", 500),
        ("1.234", "KWD", 1234),
        (Decimal("0.01"), "USD", 1),
    ],
)
def test_parses_the_shapes_speech_to_text_produces(text, currency, expected):
    assert parse_amount(text, currency) == expected


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("12.345", "USD"),  # more precision than the currency has
        ("1.5", "JPY"),  # JPY has no minor unit at all
        ("-5.00", "USD"),  # an expense is never negative
        ("", "USD"),
        ("abc", "USD"),
    ],
)
def test_refuses_amounts_it_cannot_represent_exactly(text, currency):
    with pytest.raises(ValueError):
        parse_amount(text, currency)


def test_split_between_nobody_is_an_error():
    with pytest.raises(ValueError):
        split_equally(100, [])
    with pytest.raises(ValueError):
        split_by_weights(100, {})


def test_weights_that_sum_to_zero_are_an_error():
    with pytest.raises(ValueError):
        split_by_weights(100, {"a": 0, "b": 0})


# -- speech vs screen ----------------------------------------------------
# Alexa reads the tool's text aloud verbatim, so the two renderings diverge on
# purpose: "$42.50" is unreadable out loud, "42 dollars 50 cents" is unreadable
# on a screen.


@pytest.mark.parametrize(
    ("minor", "currency", "spoken"),
    [
        (4200, "USD", "42 dollars"),
        (4250, "USD", "42 dollars 50 cents"),
        (100, "USD", "1 dollar"),
        (50, "USD", "50 cents"),
        (1, "USD", "1 cent"),
        (0, "USD", "0 dollars"),
        (500, "JPY", "500 yen"),
        (1250, "GBP", "12 pounds 50 pence"),
    ],
)
def test_amounts_are_spoken_the_way_a_person_says_them(minor, currency, spoken):
    assert say_amount(minor, currency) == spoken


def test_an_unknown_currency_falls_back_to_its_code():
    """Wrong-sounding is acceptable; wrong is not."""
    assert say_amount(1234, "KWD") == "1.234 KWD"


@pytest.mark.parametrize(
    ("minor", "currency", "shown"),
    [(4250, "USD", "$42.50"), (500, "JPY", "¥500"), (1250, "GBP", "£12.50"), (1234, "KWD", "1.234 KWD")],
)
def test_amounts_are_shown_with_a_symbol_on_screen(minor, currency, shown):
    assert show_amount(minor, currency) == shown


@given(minor=st.integers(min_value=0, max_value=10**7), currency=CURRENCIES)
def test_no_amount_is_ever_spoken_as_a_currency_code_when_we_know_the_words(minor, currency):
    said = say_amount(minor, currency)
    if currency in {"USD", "EUR", "GBP", "JPY"}:
        assert currency not in said
