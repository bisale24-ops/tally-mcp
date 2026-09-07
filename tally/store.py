"""SQLite persistence. Households are small, so reads load the whole thing."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .ledger import Entry, Household, Member, _new_id
from .money import Allocation, normalise_currency

_SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    currency   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    aliases      TEXT NOT NULL DEFAULT '[]',
    principal    TEXT,
    invite_code  TEXT,
    position     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    payer_id     TEXT NOT NULL,
    total        INTEGER NOT NULL,
    currency     TEXT NOT NULL,
    description  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    voided       INTEGER NOT NULL DEFAULT 0,
    position     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    entry_id  TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL,
    amount    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS join_attempts (
    scope   TEXT NOT NULL,
    window  INTEGER NOT NULL,
    tries   INTEGER NOT NULL,
    PRIMARY KEY (scope, window)
);
CREATE TABLE IF NOT EXISTS tool_calls (
    key        TEXT PRIMARY KEY,
    entry_id   TEXT NOT NULL,
    spoken     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_members_household   ON members(household_id, position);
-- Two devices adding the same flatmate both read a household without them.
CREATE UNIQUE INDEX IF NOT EXISTS idx_members_name_once
    ON members(household_id, name COLLATE NOCASE);
-- One person, one household. Without this, two devices creating a household at
-- the same moment both pass the "do you have one?" check and both create one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_members_principal_once
    ON members(principal) WHERE principal IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_members_invite
    ON members(invite_code) WHERE invite_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_members_principal   ON members(principal);
CREATE INDEX IF NOT EXISTS idx_entries_household   ON entries(household_id, position);
CREATE INDEX IF NOT EXISTS idx_allocations_entry   ON allocations(entry_id);
"""


class Store:
    """Household storage backed by SQLite."""

    def __init__(self, path: str | Path = "tally.db") -> None:
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL: several flatmates can talk to Alexa at once.
            conn.execute("PRAGMA journal_mode = WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # -- households ------------------------------------------------------

    MAX_NAME = 80

    def create_household(self, name: str, currency: str, *, founder: str, principal: str | None = None) -> Household:
        """Create a household and add `founder` as its first member.

        Raises:
            ValueError: If the name is blank or too long, the currency is not a
                three-letter code, or `principal` already belongs to a household.
        """
        clean = name.strip()
        if not clean:
            raise ValueError("a household needs a name")
        if len(clean) > self.MAX_NAME:
            raise ValueError("that name is too long")

        household = Household(id=_new_id("hh"), name=clean, currency=normalise_currency(currency))
        member = household.add_member(founder)
        with self._connect() as conn:
            # One transaction, so a rejected member cannot leave an orphan
            # household behind. The unique index on `principal` is what actually
            # decides the race between two simultaneous creators.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO households (id, name, currency, created_at) VALUES (?, ?, ?, ?)",
                    (household.id, household.name, household.currency, datetime.now(UTC).isoformat()),
                )
                self._insert_member(conn, household.id, member, principal=principal)
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise ValueError("you're already in a household") from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return household

    def load(self, household_id: str) -> Household | None:
        """Load a household with its members and entries."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
            if row is None:
                return None
            household = Household(id=row["id"], name=row["name"], currency=row["currency"])

            for member_row in conn.execute(
                "SELECT * FROM members WHERE household_id = ? ORDER BY position", (household_id,)
            ):
                household.members.append(
                    Member(
                        id=member_row["id"],
                        name=member_row["name"],
                        aliases=tuple(json.loads(member_row["aliases"])),
                    )
                )

            allocations: dict[str, list[Allocation]] = {}
            for alloc_row in conn.execute(
                "SELECT a.* FROM allocations a JOIN entries e ON e.id = a.entry_id WHERE e.household_id = ?",
                (household_id,),
            ):
                allocations.setdefault(alloc_row["entry_id"], []).append(
                    Allocation(alloc_row["member_id"], alloc_row["amount"])
                )

            for entry_row in conn.execute(
                "SELECT * FROM entries WHERE household_id = ? ORDER BY position", (household_id,)
            ):
                household.entries.append(
                    Entry(
                        id=entry_row["id"],
                        kind=entry_row["kind"],
                        payer_id=entry_row["payer_id"],
                        total=entry_row["total"],
                        currency=entry_row["currency"],
                        description=entry_row["description"],
                        allocations=tuple(allocations.get(entry_row["id"], ())),
                        created_at=datetime.fromisoformat(entry_row["created_at"]),
                        voided=bool(entry_row["voided"]),
                    )
                )
            return household

    def household_id_for_principal(self, principal: str) -> str | None:
        """The household this authenticated user belongs to, if any."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT household_id FROM members WHERE principal = ? ORDER BY position LIMIT 1",
                (principal,),
            ).fetchone()
            return row["household_id"] if row else None

    def member_id_for_principal(self, principal: str, household_id: str) -> str | None:
        """Which member in this household is the authenticated caller."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM members WHERE principal = ? AND household_id = ?",
                (principal, household_id),
            ).fetchone()
            return row["id"] if row else None

    # -- writes ----------------------------------------------------------

    def add_member(self, household: Household, member: Member, *, principal: str | None = None) -> str | None:
        """Store a member. Returns the invite code they need to link a device.

        A member with no `principal` is a placeholder somebody else created, and
        the code is the only thing that lets the real person claim it.
        """
        code = None if principal else new_invite_code()
        with self._connect() as conn:
            try:
                self._insert_member(conn, household.id, member, principal=principal, invite_code=code)
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"{member.name} is already in {household.name}") from exc
        return code

    def claim_member(self, code: str, principal: str) -> str | None:
        """Bind an identity to the member holding `code`, and burn the code.

        The UPDATE only matches while the member is still unclaimed, so two
        people racing the same code cannot both get in.

        Returns the member id, or None if the code is wrong or already used.
        """
        offered = code.strip().upper().replace("-", "").replace(" ", "")
        if not offered:
            return None
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id, invite_code FROM members WHERE invite_code = ? AND principal IS NULL",
                    (offered,),
                ).fetchone()
                if row is None or not hmac.compare_digest(row["invite_code"], offered):
                    conn.execute("ROLLBACK")
                    return None
                claimed = conn.execute(
                    "UPDATE members SET principal = ?, invite_code = NULL"
                    " WHERE id = ? AND principal IS NULL RETURNING id",
                    (principal, row["id"]),
                ).fetchone()
            except sqlite3.IntegrityError:
                # This identity already belongs to a household.
                conn.execute("ROLLBACK")
                return None
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return claimed["id"] if claimed else None

    def release_claim(self, member_id: str, code: str) -> None:
        """Undo a claim whose sign-in never completed.

        The code is burned before the OAuth step finishes, so a link that
        expires in between leaves the member owned by an identity that never got
        a token and the person it was meant for locked out.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE members SET principal = NULL, invite_code = ? WHERE id = ?",
                (code.strip().upper().replace("-", "").replace(" ", ""), member_id),
            )

    JOIN_TRIES = 5
    JOIN_WINDOW = 600

    def too_many_join_attempts(self, *scopes: str) -> bool:
        """Count a join attempt against each scope, and say whether to stop.

        Eight characters is a wide space, but nothing was making a script pay
        for walking it. Counting both the caller and the linking session means
        neither a shared address nor a fresh session buys more guesses.
        """
        window = int(time.time()) // self.JOIN_WINDOW
        with self._connect() as conn:
            worst = 0
            for scope in scopes:
                conn.execute(
                    "INSERT INTO join_attempts (scope, window, tries) VALUES (?, ?, 1)"
                    " ON CONFLICT(scope, window) DO UPDATE SET tries = tries + 1",
                    (scope, window),
                )
                row = conn.execute(
                    "SELECT tries FROM join_attempts WHERE scope = ? AND window = ?", (scope, window)
                ).fetchone()
                worst = max(worst, int(row["tries"]))
            conn.execute("DELETE FROM join_attempts WHERE window < ?", (window - 1,))
        return worst > self.JOIN_TRIES

    def add_entry(
        self, household: Household, entry: Entry, *, call_keys: Sequence[str] = (), spoken: str = ""
    ) -> str | None:
        """Append an entry, or return what an identical earlier call already said.

        `call_key` identifies one utterance. The entry and the record of the call
        go in under a single transaction, so a retry arriving while the first is
        still writing loses the race on the primary key rather than adding a
        second expense.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if call_keys:
                    seen = self._seen(conn, call_keys)
                    if seen is not None:
                        conn.execute("COMMIT")
                        return seen

                # The position is picked inside the INSERT. Reading the maximum
                # first and writing it back is a race: two flatmates recording at
                # the same moment both read the same number, and "undo the last
                # one" then has two candidates for last.
                conn.execute(
                    "INSERT INTO entries (id, household_id, kind, payer_id, total, currency, description,"
                    " created_at, voided, position)"
                    " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(MAX(position) + 1, 0)"
                    " FROM entries WHERE household_id = ?",
                    (
                        entry.id,
                        household.id,
                        entry.kind,
                        entry.payer_id,
                        entry.total,
                        entry.currency,
                        entry.description,
                        entry.created_at.isoformat(),
                        int(entry.voided),
                        household.id,
                    ),
                )
                conn.executemany(
                    "INSERT INTO allocations (entry_id, member_id, amount) VALUES (?, ?, ?)",
                    [(entry.id, a.member_id, a.amount) for a in entry.allocations],
                )
                if call_keys:
                    self._remember(conn, call_keys[0], entry.id, spoken)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return self.spoken_for(*call_keys) if call_keys else None
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return None

    def void_entry(self, entry_id: str, *, call_keys: Sequence[str] = (), spoken: str = "") -> tuple[str | None, bool]:
        """Void an entry once.

        Returns what an identical earlier call said (or None), and whether this
        call voided anything. Without the call record a retried "cancel that"
        walks back to the entry before it - two flatmates saying it at once had
        the same effect, so the guarded UPDATE stays.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if call_keys:
                    seen = self._seen(conn, call_keys)
                    if seen is not None:
                        conn.execute("COMMIT")
                        return seen, False

                row = conn.execute(
                    "UPDATE entries SET voided = 1 WHERE id = ? AND voided = 0 RETURNING id",
                    (entry_id,),
                ).fetchone()
                if row is not None and call_keys:
                    self._remember(conn, call_keys[0], entry_id, spoken)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return (self.spoken_for(*call_keys) if call_keys else None), False
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return None, row is not None

    def spoken_for(self, *call_keys: str) -> str | None:
        """What an earlier call under any of these keys answered."""
        if not call_keys:
            return None
        with self._connect() as conn:
            return self._seen(conn, call_keys)

    def _seen(self, conn: sqlite3.Connection, call_keys: Sequence[str]) -> str | None:
        row = conn.execute(
            f"SELECT spoken FROM tool_calls WHERE key IN ({','.join('?' * len(call_keys))})",  # noqa: S608
            tuple(call_keys),
        ).fetchone()
        return row["spoken"] if row else None

    def _remember(self, conn: sqlite3.Connection, call_key: str, entry_id: str, spoken: str) -> None:
        conn.execute(
            "INSERT INTO tool_calls (key, entry_id, spoken, created_at) VALUES (?, ?, ?, ?)",
            (call_key, entry_id, spoken, time.time()),
        )

    def _insert_member(
        self,
        conn: sqlite3.Connection,
        household_id: str,
        member: Member,
        *,
        principal: str | None,
        invite_code: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO members (id, household_id, name, aliases, principal, invite_code, position)"
            " SELECT ?, ?, ?, ?, ?, ?, COALESCE(MAX(position) + 1, 0) FROM members WHERE household_id = ?",
            (
                member.id,
                household_id,
                member.name,
                json.dumps(list(member.aliases)),
                principal,
                invite_code,
                household_id,
            ),
        )


# No 0/O/1/I: the code gets read out loud or typed from a text message.
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def new_invite_code() -> str:
    """An eight-character code. Names are not secrets; this is."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))
