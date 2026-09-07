"""The shared ledger: who paid, who owes, and the fewest transfers that settle it."""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from .money import Allocation, split_by_weights, split_equally


def fold(text: str) -> str:
    """Compare names the way a person reads them.

    "Zoë" typed with a combining accent is a different string from the composed
    form and the same name to everyone who sees it; both come out of speech
    recognition and out of keyboards.
    """
    return unicodedata.normalize("NFC", text).strip().casefold()


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class Member:
    """A person in a household. `aliases` are the names speech recognition produces."""

    id: str
    name: str
    aliases: tuple[str, ...] = ()

    def answers_to(self, spoken: str) -> bool:
        target = fold(spoken)
        return target == fold(self.name) or any(target == fold(a) for a in self.aliases)


@dataclass(frozen=True, slots=True)
class Entry:
    """One immutable ledger fact.

    An expense: `payer_id` paid `total` for the members in `allocations`.
    A transfer: `payer_id` handed money to the single member in `allocations`.
    Same shape, so balances are one pass over both.
    """

    id: str
    kind: str  # "expense" | "transfer"
    payer_id: str
    total: int
    currency: str
    description: str
    allocations: tuple[Allocation, ...]
    created_at: datetime = field(default_factory=_now)
    voided: bool = False

    @property
    def is_live(self) -> bool:
        return not self.voided


@dataclass(slots=True)
class Household:
    """People sharing costs, and the entries between them."""

    id: str
    name: str
    currency: str
    members: list[Member] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    # -- membership ------------------------------------------------------

    MAX_NAME = 60

    def add_member(self, name: str, aliases: tuple[str, ...] = ()) -> Member:
        """Add a person.

        Raises:
            ValueError: If the name is blank, too long, or already taken.
        """
        clean = unicodedata.normalize("NFC", name).strip()
        if not clean:
            raise ValueError("a person needs a name")
        if len(clean) > self.MAX_NAME:
            raise ValueError("that name is too long")
        if self.find_member(clean) is not None:
            raise ValueError(f"{clean} is already in {self.name}")
        # An alias repeating the member's own name is redundant, not an error.
        kept = tuple(
            dict.fromkeys(
                unicodedata.normalize("NFC", a).strip() for a in aliases if a.strip() and fold(a) != fold(clean)
            )
        )
        for alias in kept:
            # An alias belonging to someone else would either be ignored (first
            # match wins) or start answering for the wrong person.
            if self.find_member(alias) is not None:
                raise ValueError(f"{alias} already means someone else in {self.name}")
        member = Member(id=_new_id("mem"), name=clean, aliases=kept)
        self.members.append(member)
        return member

    def find_member(self, spoken: str) -> Member | None:
        """Match a spoken name exactly, then by prefix. Ambiguous prefixes return None."""
        for member in self.members:
            if member.answers_to(spoken):
                return member
        target = fold(spoken)
        if not target:
            return None
        hits = [m for m in self.members if fold(m.name).startswith(target)]
        return hits[0] if len(hits) == 1 else None

    def member_by_id(self, member_id: str) -> Member | None:
        return next((m for m in self.members if m.id == member_id), None)

    def name_of(self, member_id: str) -> str:
        member = self.member_by_id(member_id)
        return member.name if member else "someone who left"

    # -- recording -------------------------------------------------------

    def record_expense(
        self,
        *,
        payer_id: str,
        total: int,
        description: str,
        participant_ids: list[str] | None = None,
        weights: dict[str, int | float] | None = None,
        exact: dict[str, int] | None = None,
    ) -> Entry:
        """Record that `payer_id` paid `total` for some members.

        One split rule applies: `exact` if given, else `weights`, else an equal
        split across `participant_ids` (`None` means the whole household).

        Raises:
            ValueError: If the payer or a participant is unknown, the total is
                not positive, no participant was named, or `exact` amounts are
                negative or do not sum to `total`.
        """
        if self.member_by_id(payer_id) is None:
            raise ValueError("unknown payer")
        if total <= 0:
            raise ValueError("an expense must be more than zero")

        if exact is not None:
            self._require_known(exact)
            if any(amount < 0 for amount in exact.values()):
                raise ValueError("a share must not be negative")
            if sum(exact.values()) != total:
                raise ValueError("the shares do not add up to the total")
            allocations = tuple(Allocation(mid, amt) for mid, amt in exact.items())
        elif weights is not None:
            self._require_known(weights)
            allocations = tuple(split_by_weights(total, weights))
        else:
            # An empty list means nobody was named - not a synonym for everyone.
            if participant_ids is None:
                ids = [m.id for m in self.members]
            elif not participant_ids:
                raise ValueError("no one was named to share this expense")
            else:
                # Speech repeats names ("me, Bob and Bob"): one mention, one share.
                ids = list(dict.fromkeys(participant_ids))
            self._require_known(dict.fromkeys(ids, 0))
            # Entry count seeds the odd-cent rotation.
            allocations = tuple(split_equally(total, ids, seed=len(self.entries)))

        entry = Entry(
            id=_new_id("exp"),
            kind="expense",
            payer_id=payer_id,
            total=total,
            currency=self.currency,
            description=description.strip(),
            allocations=allocations,
        )
        self.entries.append(entry)
        return entry

    def record_transfer(self, *, from_id: str, to_id: str, amount: int, note: str = "settling up") -> Entry:
        """Record a payment from one member to another.

        Raises:
            ValueError: If either member is unknown, they are the same person,
                or the amount is not positive.
        """
        self._require_known({from_id: 0, to_id: 0})
        if from_id == to_id:
            raise ValueError("a transfer needs two different people")
        if amount <= 0:
            raise ValueError("a transfer must be more than zero")
        entry = Entry(
            id=_new_id("pay"),
            kind="transfer",
            payer_id=from_id,
            total=amount,
            currency=self.currency,
            description=note.strip(),
            allocations=(Allocation(to_id, amount),),
        )
        self.entries.append(entry)
        return entry

    def void(self, entry_id: str) -> Entry:
        """Reverse an entry, keeping it in the history.

        Raises:
            ValueError: If no such live entry exists.
        """
        for index, entry in enumerate(self.entries):
            if entry.id == entry_id and entry.is_live:
                voided = replace(entry, voided=True)
                self.entries[index] = voided
                return voided
        raise ValueError("no such entry to undo")

    def last_entry(self) -> Entry | None:
        return next((e for e in reversed(self.entries) if e.is_live), None)

    def _require_known(self, ids: dict[str, object]) -> None:
        for member_id in ids:
            if self.member_by_id(member_id) is None:
                raise ValueError("unknown member in the split")

    # -- balances --------------------------------------------------------

    def owed_between(self, a_id: str, b_id: str) -> int:
        """What `a_id` owes `b_id`, netted. Negative means `b_id` owes `a_id`.

        This is the pairwise debt, not a slice of the settlement plan: "what do I
        owe Chris" means what passed between the two of them, regardless of how
        the group as a whole would most efficiently square up.
        """
        owed = 0
        for entry in self.entries:
            if not entry.is_live:
                continue
            if entry.payer_id == b_id:
                owed += sum(al.amount for al in entry.allocations if al.member_id == a_id)
            elif entry.payer_id == a_id:
                owed -= sum(al.amount for al in entry.allocations if al.member_id == b_id)
        return owed

    def balances(self) -> dict[str, int]:
        """Net position per member: positive is owed money, negative owes it."""
        net = {m.id: 0 for m in self.members}
        for entry in self.entries:
            if not entry.is_live:
                continue
            net[entry.payer_id] = net.get(entry.payer_id, 0) + entry.total
            for allocation in entry.allocations:
                net[allocation.member_id] = net.get(allocation.member_id, 0) - allocation.amount
        return net


@dataclass(frozen=True, slots=True)
class Transfer:
    """A suggested payment."""

    from_id: str
    to_id: str
    amount: int


def settle(balances: dict[str, int]) -> list[Transfer]:
    """Clear every balance, greedily matching largest debtor to largest creditor.

    Each step zeroes at least one person, so this never exceeds n-1 transfers.
    The true minimum is NP-hard; this is optimal unless a strict subset of
    members balances among itself.
    """
    creditors = sorted(((mid, amt) for mid, amt in balances.items() if amt > 0), key=lambda kv: (-kv[1], kv[0]))
    debtors = sorted(((mid, -amt) for mid, amt in balances.items() if amt < 0), key=lambda kv: (-kv[1], kv[0]))

    transfers: list[Transfer] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, owed = debtors[i]
        creditor, due = creditors[j]
        amount = min(owed, due)
        if amount > 0:
            transfers.append(Transfer(from_id=debtor, to_id=creditor, amount=amount))
        owed -= amount
        due -= amount
        debtors[i] = (debtor, owed)
        creditors[j] = (creditor, due)
        if owed == 0:
            i += 1
        if due == 0:
            j += 1
    return transfers
