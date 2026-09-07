"""Amounts as integer minor units. Floats never touch the arithmetic."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_MINOR = 10**15
"""Ceiling on a single amount. Well past any real household bill, and well
inside the 64-bit integer SQLite stores it in - past that, writing the row
raises OverflowError from the driver instead of being refused up front."""

# What may decorate an amount: whitespace and currency signs.
_SIGNS = "$\u20ac\u00a3\u00a5\u20bd\u20b8\u20b4\u20a9\u20b9"
_NUMERIC = re.compile(r"[+-]?[0-9][0-9,]*(\.[0-9]+)?|[+-]?\.[0-9]+")
_THOUSANDS = re.compile(r"[+-]?[0-9]{1,3}(,[0-9]{3})+")
_DECIMAL_COMMA = re.compile(r"[+-]?[0-9]+,[0-9]{1,2}")

# Currencies whose minor unit is not 1/100; the default is 2.
_EXPONENTS: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "UGX": 0,
    "XAF": 0,
    "XOF": 0,
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


_CODE = re.compile(r"[A-Za-z]{3}")


def normalise_currency(code: str) -> str:
    """Upper-case a three-letter currency code.

    Raises:
        ValueError: If it is not three letters. A nonsense code would otherwise
            be stored and every later amount silently formatted against it.
    """
    clean = code.strip()
    if not _CODE.fullmatch(clean):
        raise ValueError(f"not a currency code: {code!r}")
    return clean.upper()


def exponent(currency: str) -> int:
    """Number of decimal places `currency` uses."""
    return _EXPONENTS.get(currency.upper(), 2)


def parse_amount(value: str | int | float | Decimal, currency: str) -> int:
    """Parse what speech-to-text produces: "12.50", "12,50", "1 200.75", "$12.50".

    Raises:
        ValueError: If the text is not a number, is negative, or carries more
            decimal places than `currency` supports.
    """
    exp = exponent(currency)
    if isinstance(value, str):
        # Drop only what decorates an amount - spaces and currency signs. Letters
        # are never dropped: filtering them out turns "1e10" into 110 and
        # "12abc34" into 1234, which is a wrong number nobody would notice.
        cleaned = "".join(ch for ch in value if not ch.isspace() and ch not in _SIGNS)
        if not _NUMERIC.fullmatch(cleaned):
            raise ValueError(f"not an amount: {value!r}")
        # A comma before three digits groups thousands; before one or two it is
        # the decimal point. "1,200" was being read as one dollar twenty.
        if "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            if _THOUSANDS.fullmatch(cleaned):
                # "1,234" reads as both 1234 and 1.234 where three decimals
                # exist, and the two differ by a factor of a thousand.
                if exp == 3 and cleaned.count(",") == 1:
                    raise ValueError(f"ambiguous amount for {currency}: {value!r}")
                cleaned = cleaned.replace(",", "")
            elif _DECIMAL_COMMA.fullmatch(cleaned):
                cleaned = cleaned.replace(",", ".")
            else:
                raise ValueError(f"not an amount: {value!r}")
        try:
            dec = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"not an amount: {value!r}") from exc
    else:
        dec = Decimal(str(value))
        if not dec.is_finite():
            raise ValueError(f"not an amount: {value!r}")

    if dec < 0:
        raise ValueError(f"amount must not be negative: {value!r}")
    scaled = dec * (10**exp)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{currency} has {exp} decimal places, got {value!r}")
    minor = int(scaled)
    if minor > MAX_MINOR:
        raise ValueError(f"amount is larger than this ledger tracks: {value!r}")
    return minor


def format_amount(minor: int, currency: str) -> str:
    """Render for display: ``(1250, "USD") -> "12.50"``."""
    exp = exponent(currency)
    if exp == 0:
        return str(minor)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 10**exp)
    return f"{sign}{whole}.{frac:0{exp}d}"


# Singular and plural for the major and minor unit. A currency that is missing
# here is spoken as its code, which is wrong-sounding but never wrong.
_SPOKEN: dict[str, tuple[str, str, str, str]] = {
    "USD": ("dollar", "dollars", "cent", "cents"),
    "CAD": ("dollar", "dollars", "cent", "cents"),
    "AUD": ("dollar", "dollars", "cent", "cents"),
    "EUR": ("euro", "euros", "cent", "cents"),
    "GBP": ("pound", "pounds", "penny", "pence"),
    "JPY": ("yen", "yen", "", ""),
}


def say_amount(minor: int, currency: str) -> str:
    """Render for speech: ``(1250, "USD") -> "12 dollars 50 cents"``.

    Alexa reads the tool's text aloud verbatim, so "12.50 USD" would come out as
    "twelve point five zero U S D". Money in a spoken answer has to be written
    the way a person says it.
    """
    code = currency.upper()
    names = _SPOKEN.get(code)
    if names is None:
        return f"{format_amount(minor, currency)} {code}"

    major_one, major_many, minor_one, minor_many = names
    scale = 10 ** exponent(currency)
    sign = "minus " if minor < 0 else ""
    whole, part = divmod(abs(minor), scale)

    if scale == 1:
        return f"{sign}{whole} {major_one if whole == 1 else major_many}"
    if whole and part:
        return (
            f"{sign}{whole} {major_one if whole == 1 else major_many} {part} {minor_one if part == 1 else minor_many}"
        )
    if whole:
        return f"{sign}{whole} {major_one if whole == 1 else major_many}"
    if part:
        return f"{sign}{part} {minor_one if part == 1 else minor_many}"
    return f"0 {major_many}"


_SYMBOLS = {"USD": "$", "CAD": "$", "AUD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}


def show_amount(minor: int, currency: str) -> str:
    """Render for a screen: ``(1250, "USD") -> "$12.50"``."""
    code = currency.upper()
    symbol = _SYMBOLS.get(code)
    body = format_amount(abs(minor), currency)
    sign = "-" if minor < 0 else ""
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{body} {code}"


@dataclass(frozen=True, slots=True)
class Allocation:
    """One participant's share, in minor units."""

    member_id: str
    amount: int


def split_equally(total: int, member_ids: list[str], *, seed: int = 0) -> list[Allocation]:
    """Split `total` as evenly as integers allow.

    A three-way split of 10.00 leaves an odd cent. `seed` rotates who absorbs it
    so it does not always fall on the same person.

    Raises:
        ValueError: If `member_ids` is empty.
    """
    if not member_ids:
        raise ValueError("cannot split between nobody")
    n = len(member_ids)
    base, remainder = divmod(total, n)
    offset = seed % n
    rotated = member_ids[offset:] + member_ids[:offset]
    extra = set(rotated[:remainder])
    return [Allocation(mid, base + (1 if mid in extra else 0)) for mid in member_ids]


def split_by_weights(total: int, weights: dict[str, int | float]) -> list[Allocation]:
    """Split `total` in proportion to `weights`, by largest remainder.

    Raises:
        ValueError: If `weights` is empty, any weight is negative, or they sum to zero.
    """
    if not weights:
        raise ValueError("cannot split between nobody")
    if any(not math.isfinite(w) for w in weights.values()):
        raise ValueError("a share must be a real number")
    if any(w < 0 for w in weights.values()):
        raise ValueError("weights must not be negative")
    denominator = sum(weights.values())
    if not math.isfinite(denominator):
        raise ValueError("those shares are too large to divide")
    if denominator <= 0:
        raise ValueError("weights must sum to more than zero")

    exact = {mid: total * w / denominator for mid, w in weights.items()}
    floors = {mid: int(v) for mid, v in exact.items()}
    shortfall = total - sum(floors.values())
    # Ties break on member id so the split is reproducible.
    ranked = sorted(exact, key=lambda mid: (-(exact[mid] - floors[mid]), mid))
    for mid in ranked[:shortfall]:
        floors[mid] += 1
    return [Allocation(mid, floors[mid]) for mid in weights]
