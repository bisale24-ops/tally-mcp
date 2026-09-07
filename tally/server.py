"""The Tally MCP server: a shared household ledger, spoken.

Every tool returns a sentence meant to be heard; the visual app is additive and
the spoken answer never depends on it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.auth import OAuthClientInformationFull
from mcp.shared.exceptions import MCPError, NoBackChannelError
from mcp_types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, AnyUrl, BaseModel, Field

from .auth import TallyAuthProvider
from .ledger import Household, Member, settle
from .login import make_login_route
from .money import parse_amount, say_amount, show_amount
from .store import Store
from .ui import BALANCE_APP

BALANCE_URI = "ui://tally/balances.html"
LEDGER_URI = "tally://household"
MCP_PATH = "/mcp"


def _icon(path: str) -> Icon:
    """A monochrome line glyph as a data URI, so nothing has to be fetched."""
    document = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round">{path}</svg>'
    )
    return Icon(src="data:image/svg+xml," + quote(document, safe=""), mime_type="image/svg+xml")


_GLYPHS = {
    "record_expense": '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2Z"/><path d="M9 8h6"/><path d="M9 12h6"/>',
    "show_balances": (
        '<path d="M12 4v16"/><path d="M6 8h12"/>'
        '<path d="m4 14 2-6 2 6a2 2 0 0 1-4 0Z"/><path d="m16 14 2-6 2 6a2 2 0 0 1-4 0Z"/>'
    ),
    "settle_up": '<path d="M4 12h16"/><path d="m14 7 5 5-5 5"/><path d="M4 7v10"/>',
    "undo_last": '<path d="M3 10h11a5 5 0 0 1 0 10h-4"/><path d="m3 10 4-4"/><path d="m3 10 4 4"/>',
    "start_household": '<path d="m3 11 9-7 9 7"/><path d="M6 10v9h12v-9"/><path d="M10 19v-5h4v5"/>',
    "add_person": (
        '<circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0 1 12 0"/><path d="M18 8v6"/><path d="M15 11h6"/>'
    ),
    "what_do_i_owe": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M9.5 9.5a2.5 2.5 0 1 1 3.2 2.4c-.5.2-.7.6-.7 1.1v.5"/><path d="M12 17h.01"/>'
    ),
    "recent_activity": (
        '<path d="M4 6h10"/><path d="M4 12h7"/><path d="M4 18h7"/>'
        '<circle cx="17" cy="15" r="4"/><path d="M17 13.5V15l1 .8"/>'
    ),
}

ICONS: dict[str, Icon] = {name: _icon(glyph) for name, glyph in _GLYPHS.items()}
LEDGER_ICON = _icon('<path d="M4 5h16v14H4z"/><path d="M4 9h16"/><path d="M9 9v10"/>')

# -- the visual app's payload -------------------------------------------


class BalanceRow(BaseModel):
    """One person's net position."""

    name: str
    minor: int = Field(description="Net in minor units; positive is owed, negative owes")
    display: str


class SettlementRow(BaseModel):
    """One suggested payment."""

    from_: str = Field(alias="from")
    to: str
    minor: int
    display: str

    model_config = {"populate_by_name": True}


class LedgerView(BaseModel):
    """Balances and settlement plan, for speech and screen."""

    household: str
    summary: str
    balances: list[BalanceRow]
    settle: list[SettlementRow]


# -- elicitation schema --------------------------------------------------


class WhichPerson(BaseModel):
    """Elicited when a spoken name matches more than one member."""

    name: str = Field(description="The full name of the person you meant")


# -- server --------------------------------------------------------------


def create_server(store: Store | None = None, *, public_url: str | None = None) -> MCPServer:
    """Build the Tally MCP server.

    Args:
        store: Persistence. Defaults to the path in `TALLY_DB`, else `tally.db`.
        public_url: The origin Alexa+ reaches this server on. Supplying it turns
            on OAuth 2.1 and the account-linking page. Without it the server runs
            unauthenticated - fine for the Inspector, never for a real device.
    """
    store = store or Store(os.environ.get("TALLY_DB", "tally.db"))
    apps = Apps()

    apps.add_html_resource(
        BALANCE_URI,
        BALANCE_APP,
        name="Tally balances",
        title="Who owes what",
        description="The household's balances and the shortest way to settle them.",
        # The app reaches no network at all, so it declares no domains.
        csp=ResourceCsp(),
        prefers_border=True,
    )

    # -- session helpers -------------------------------------------------

    def principal() -> str:
        """The authenticated caller, or a dev identity when auth is off."""
        token = get_access_token()
        if token is not None and token.subject:
            return token.subject
        return os.environ.get("TALLY_DEV_PRINCIPAL", "dev-principal")

    BUSY = "I couldn't reach the ledger just now. Try that again in a moment."

    def current_household() -> Household:
        """The caller's household, or a ToolError telling them how to make one.

        Storage trouble is translated here and at each write. Left alone a busy
        or missing database surfaces as an unexpected tool failure - a stack
        trace on the server and "Error executing tool" for the listener.
        """
        try:
            household_id = store.household_id_for_principal(principal())
            household = store.load(household_id) if household_id else None
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        if household_id is None:
            raise ToolError("You don't have a household yet. Say something like: start a household called Flat.")
        if household is None:  # pragma: no cover - referential integrity
            raise ToolError("That household no longer exists.")
        return household

    def me(household: Household) -> Member | None:
        member_id = store.member_id_for_principal(principal(), household.id)
        return household.member_by_id(member_id) if member_id else None

    async def resolve(household: Household, spoken: str | None, ctx: Context[Any]) -> Member:
        """Resolve a spoken name. `None` or a self-reference means the caller."""
        if spoken is None or spoken.strip().casefold() in {"me", "i", "myself"}:
            caller = me(household)
            if caller is None:
                raise ToolError("I don't know which member you are in this household yet.")
            return caller

        member = household.find_member(spoken)
        if member is not None:
            return member

        candidates = [m for m in household.members if m.name.casefold().startswith(spoken.strip().casefold()[:1])]
        names = ", ".join(m.name for m in household.members)

        if len(candidates) > 1:
            question = f"Did you mean {' or '.join(c.name for c in candidates)}?"
            try:
                answer = await ctx.elicit(question, WhichPerson)
            except (NoBackChannelError, MCPError):
                # No back-channel: put the question in the error rather than crash.
                raise ToolError(question) from None
            if answer.action == "accept" and answer.data is not None:
                chosen = household.find_member(answer.data.name)
                if chosen is not None:
                    return chosen
            raise ToolError(question)

        raise ToolError(f"I don't know who {spoken} is. This household has: {names}.")

    def call_keys(tool: str, explicit: str | None, **args: Any) -> tuple[str, ...]:
        """Identify one utterance, so a retransmission is not a second expense.

        A key supplied by the host is trusted as given. Without one the same
        arguments from the same person stand in, over the current minute and the
        one before it - a resend two seconds later can fall the far side of a
        bucket boundary. The first key is the one a new call is written under.
        """
        if explicit:
            return (f"{principal()}|{tool}|{explicit}",)
        shape = json.dumps(args, sort_keys=True, default=str)
        now = int(time.time() // 60)
        return tuple(
            hashlib.sha256(f"{principal()}|{tool}|{shape}|{stamp}".encode()).hexdigest() for stamp in (now, now - 1)
        )

    def view(household: Household) -> LedgerView:
        """Balances and settlement plan for speech and screen."""
        balances = household.balances()
        rows = [
            BalanceRow(
                name=household.name_of(member.id),
                minor=balances.get(member.id, 0),
                display=show_amount(abs(balances.get(member.id, 0)), household.currency),
            )
            for member in household.members
        ]
        transfers = settle(balances)
        plan = [
            SettlementRow(
                from_=household.name_of(t.from_id),
                to=household.name_of(t.to_id),
                minor=t.amount,
                display=show_amount(t.amount, household.currency),
            )
            for t in transfers
        ]
        return LedgerView(
            household=household.name,
            summary=_spoken_summary(household, transfers),
            balances=rows,
            settle=plan,
        )

    def reply(ctx: Context[Any], data: LedgerView, confirmation: str, summary: str | None = None) -> CallToolResult:
        """Speak `confirmation`, and `summary` too when no screen will show it.

        The structured ledger always ships - a host may use it without rendering
        anything. What a screen changes is how much has to be said out loud.
        """
        spoken = confirmation
        if summary and not client_supports_apps(ctx):
            spoken = f"{confirmation} {summary}"
        return CallToolResult(
            content=[TextContent(type="text", text=spoken)],
            structured_content=data.model_dump(mode="json", by_alias=True),
        )

    # Registered on the Apps extension before the server is built - otherwise
    # the tools silently ship without `_meta.ui.resourceUri` and nothing renders.

    @apps.tool(
        resource_uri=BALANCE_URI,
        title="Record an expense",
        icons=[ICONS["record_expense"]],
        description=(
            "Record that someone paid for something on the household's behalf. "
            "This is the main tool: use it the moment the user mentions paying."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    async def record_expense(
        ctx: Context[Any],
        amount: Annotated[str, Field(description="What was paid, e.g. '42.50'")],
        description: Annotated[str, Field(description="What it was for, e.g. 'dinner'")],
        paid_by: Annotated[str | None, Field(description="Who paid. Omit when the speaker paid.")] = None,
        split_between: Annotated[
            list[str] | None,
            Field(description="Who shares it. Omit to split across the whole household."),
        ] = None,
        shares: Annotated[
            dict[str, str] | None,
            Field(description="Name to amount, when people owe different amounts. Must sum to the total."),
        ] = None,
        idempotency_key: Annotated[
            str | None, Field(description="Repeat unchanged when resending; omit otherwise.")
        ] = None,
    ) -> LedgerView:
        household = current_household()
        payer = await resolve(household, paid_by, ctx)

        if not description.strip():
            raise ToolError("What was it for?")
        try:
            total = parse_amount(amount, household.currency)
        except ValueError as exc:
            raise ToolError(f"I couldn't read {amount!r} as an amount.") from exc

        participants: list[str] | None = None
        if split_between is not None:
            if not split_between:
                raise ToolError("Who is sharing it?")
            participants = [(await resolve(household, name, ctx)).id for name in split_between]

        exact: dict[str, int] | None = None
        if shares is not None:
            if not shares:
                raise ToolError("Who is sharing it?")
            exact = {}
            for name, owed in shares.items():
                try:
                    exact[(await resolve(household, name, ctx)).id] = parse_amount(owed, household.currency)
                except ValueError as exc:
                    raise ToolError(f"I couldn't read {owed!r} as an amount.") from exc

        try:
            entry = household.record_expense(
                payer_id=payer.id,
                total=total,
                description=description,
                participant_ids=participants,
                exact=exact,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        opening = f"Recorded {say_amount(total, household.currency)} for {entry.description}, paid by {payer.name}"
        shares = {a.amount for a in entry.allocations}
        if len(entry.allocations) == 1:
            spoken = f"{opening}. Not shared with anyone."
        elif len(shares) == 1:
            spoken = (
                f"{opening}, split {len(entry.allocations)} ways. "
                f"That's {say_amount(shares.pop(), household.currency)} each."
            )
        else:
            spoken = f"{opening}, split {len(entry.allocations)} ways."

        keys = call_keys(
            "record_expense",
            idempotency_key,
            amount=total,
            description=entry.description,
            payer=payer.id,
            split=sorted((a.member_id, a.amount) for a in entry.allocations),
        )
        try:
            replayed = store.add_entry(household, entry, call_keys=keys, spoken=spoken)
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        if replayed is not None:
            return reply(ctx, view(store.load(household.id) or household), replayed)
        await ledger_changed(ctx)
        return reply(ctx, view(household), spoken)

    @apps.tool(
        resource_uri=BALANCE_URI,
        title="Who owes what",
        icons=[ICONS["show_balances"]],
        description="Report every member's net position and the fewest payments that settle it.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def show_balances(ctx: Context[Any]) -> LedgerView:
        household = current_household()
        data = view(household)
        return reply(ctx, data, data.summary)

    @apps.tool(
        resource_uri=BALANCE_URI,
        title="Settle up",
        icons=[ICONS["settle_up"]],
        description="Record that one person paid another back.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    async def settle_up(
        ctx: Context[Any],
        amount: Annotated[str, Field(description="How much changed hands")],
        to: Annotated[str, Field(description="Who received the money")],
        paid_by: Annotated[str | None, Field(description="Who paid. Omit when the speaker paid.")] = None,
        idempotency_key: Annotated[
            str | None, Field(description="Repeat unchanged when resending; omit otherwise.")
        ] = None,
    ) -> LedgerView:
        household = current_household()
        sender = await resolve(household, paid_by, ctx)
        recipient = await resolve(household, to, ctx)
        try:
            paid = parse_amount(amount, household.currency)
            entry = household.record_transfer(from_id=sender.id, to_id=recipient.id, amount=paid)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

        spoken = f"Noted: {sender.name} paid {recipient.name} {say_amount(paid, household.currency)}."
        keys = call_keys("settle_up", idempotency_key, amount=paid, sender=sender.id, recipient=recipient.id)
        try:
            replayed = store.add_entry(household, entry, call_keys=keys, spoken=spoken)
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        if replayed is not None:
            data = view(store.load(household.id) or household)
            return reply(ctx, data, replayed, data.summary)
        await ledger_changed(ctx)
        data = view(household)
        return reply(ctx, data, spoken, data.summary)

    @apps.tool(
        resource_uri=BALANCE_URI,
        title="Undo the last entry",
        icons=[ICONS["undo_last"]],
        description="Reverse the most recent expense or payment, for when something was misheard.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    )
    async def undo_last(
        ctx: Context[Any],
        idempotency_key: Annotated[
            str | None, Field(description="Repeat unchanged when resending; omit otherwise.")
        ] = None,
    ) -> LedgerView:
        household = current_household()
        entry = household.last_entry()
        # Not keyed on which entry is last: after the first undo succeeds that
        # is a different entry, and the resend would take it too. The cost is
        # that a second undo inside the same minute replays the first answer
        # instead of walking further back.
        keys = call_keys("undo_last", idempotency_key)

        if entry is None:
            # A retry after the ledger was emptied still owes the first answer.
            try:
                replayed = store.spoken_for(*keys)
            except sqlite3.Error as exc:
                raise ToolError(BUSY) from exc
            if replayed is None:
                raise ToolError("There's nothing to undo yet.")
            data = view(household)
            return reply(ctx, data, replayed, data.summary)

        spoken = f"Undone: {say_amount(entry.total, household.currency)} for {entry.description}."
        try:
            replayed, voided = store.void_entry(entry.id, call_keys=keys, spoken=spoken)
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        if replayed is not None:
            data = view(store.load(household.id) or household)
            return reply(ctx, data, replayed, data.summary)
        if not voided:
            raise ToolError("Someone just undid that one. Ask me what's left.")

        household.void(entry.id)
        await ledger_changed(ctx)
        data = view(household)
        return reply(ctx, data, spoken, data.summary)

    auth_provider = TallyAuthProvider(store.path, static_client=_preconfigured_client()) if public_url else None
    # A token's audience is the MCP endpoint, not the origin; pointing this at
    # the origin makes every correctly issued token look foreign.
    resource_url = f"{public_url.rstrip(chr(47))}{MCP_PATH}" if public_url else None
    auth_settings = (
        AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(resource_url),
            # Kept on for the Inspector and other hosts that do register. Alexa+
            # does not: it needs TALLY_CLIENT_ID and TALLY_REDIRECT_URI.
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["ledger"], default_scopes=["ledger"]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["ledger"],
            # RFC 8707: a token minted for another server must not open this one.
            validate_token_resource=True,
        )
        if public_url
        else None
    )

    mcp = MCPServer(
        name="tally",
        title="Tally - shared household ledger",
        auth_server_provider=auth_provider,
        auth=auth_settings,
        instructions=(
            "Tally tracks shared expenses for a household out loud. Record what someone "
            "paid as it happens, then ask who owes what. Amounts are spoken as plain "
            "numbers. When the user says 'I' or 'me', omit the payer argument."
        ),
        version="0.1.0",
        extensions=[apps],
    )

    if auth_provider is not None:
        mcp.custom_route("/login", methods=["GET", "POST"], include_in_schema=False)(
            make_login_route(auth_provider, store)
        )

    # -- the ledger as a resource ----------------------------------------

    @mcp.resource(
        LEDGER_URI,
        name="Household ledger",
        title="The current balances",
        description="Balances and the settlement plan as JSON, without invoking a tool.",
        mime_type="application/json",
        icons=[LEDGER_ICON],
    )
    def ledger_resource() -> str:
        """Read-only access for a host that wants the state, not an action.

        A tool call is a side-effect-shaped thing to ask for; reading where the
        household stands is not. Hosts can also subscribe here and be told when
        it changes, which is what makes a display on a shared device stay honest.
        """
        return view(current_household()).model_dump_json(by_alias=True)

    async def ledger_changed(ctx: Context[Any]) -> None:
        """Tell listening hosts the ledger moved.

        This reaches `subscriptions/listen` streams, which arrived in
        2026-07-28. A host on 2025-11-25 - the version Alexa+ negotiates - would
        have to use `resources/subscribe`, which this SDK's high-level server
        does not serve, so those hosts poll instead. See FRICTION.md.
        """
        with contextlib.suppress(Exception):
            await ctx.notify_resource_updated(LEDGER_URI)

    # -- prompts ---------------------------------------------------------

    @mcp.prompt(
        title="Settle up",
        icons=[ICONS["settle_up"]],
        description="Work out who pays whom, and record the payments as they happen.",
    )
    def settle_up_time() -> str:
        return (
            "Show me where the household stands, then walk me through settling up "
            "one payment at a time. After each person pays, record it."
        )

    @mcp.prompt(
        title="Weekly review",
        description="Recap what the household spent and where everyone stands.",
    )
    def weekly_review() -> str:
        return (
            "Summarise what we have spent lately and who owes what. Point out anything "
            "that looks like it was recorded twice or misheard."
        )

    @mcp.prompt(
        title="Split an uneven bill",
        description="Record a bill where people owe different amounts.",
    )
    def split_an_uneven_bill(total: str, description: str = "dinner") -> str:
        return (
            f"I paid {total} for {description}, but we are not splitting it evenly. "
            "Ask me who was there and what each person had, then record it with the "
            "exact shares rather than an even split."
        )

    # -- tools with nothing to render ------------------------------------

    @mcp.tool(
        title="Start a household",
        icons=[ICONS["start_household"]],
        description="Create a shared ledger and put the speaker in it as the first member.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    def start_household(
        name: Annotated[str, Field(description="What to call it, e.g. 'Apartment 4B' or 'Tahoe trip'")],
        currency: Annotated[str, Field(description="ISO code, e.g. USD, EUR, GBP")] = "USD",
        your_name: Annotated[str, Field(description="The speaker's own name")] = "Me",
    ) -> str:
        try:
            existing = store.household_id_for_principal(principal())
            if existing is not None:
                loaded = store.load(existing)
                raise ToolError(f"You're already in {loaded.name if loaded else 'a household'}.")
            household = store.create_household(name, currency, founder=your_name, principal=principal())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        return f"Started {household.name}, tracking in {household.currency}. Who else is in it?"

    @mcp.tool(
        title="Add a person",
        icons=[ICONS["add_person"]],
        description="Add someone to the household so expenses can be split with them.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    )
    async def add_person(
        ctx: Context[Any],
        name: Annotated[str, Field(description="Their name")],
        also_called: Annotated[
            list[str] | None, Field(description="Nicknames speech recognition might produce")
        ] = None,
    ) -> str:
        household = current_household()
        try:
            member = household.add_member(name, tuple(also_called or ()))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        try:
            code = store.add_member(household, member)
        except sqlite3.Error as exc:
            raise ToolError(BUSY) from exc
        await ledger_changed(ctx)
        roster = ", ".join(m.name for m in household.members)
        spoken = f"Added {member.name}. {household.name} is now {roster}."
        if code:
            # Read out character by character; it gets heard, not seen.
            spoken += f" To link their Alexa, {member.name} needs the code {' '.join(code)}."
        return spoken

    @mcp.tool(
        title="What do I owe",
        icons=[ICONS["what_do_i_owe"]],
        description=(
            "Answer what the speaker owes one other person, or their overall position "
            "when no one is named. Use this for 'how much do I owe Chris'."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    async def what_do_i_owe(
        ctx: Context[Any],
        person: Annotated[
            str | None, Field(description="Who to compare against. Omit for the overall position.")
        ] = None,
    ) -> str:
        household = current_household()
        caller = await resolve(household, None, ctx)

        if person is None:
            net = household.balances().get(caller.id, 0)
            if net == 0:
                return f"You're square with {household.name}."
            money = say_amount(abs(net), household.currency)
            return f"You're owed {money} overall." if net > 0 else f"You owe {money} overall."

        other = await resolve(household, person, ctx)
        if other.id == caller.id:
            raise ToolError("That's you.")
        owed = household.owed_between(caller.id, other.id)
        if owed == 0:
            return f"You and {other.name} are square."
        money = say_amount(abs(owed), household.currency)
        return f"You owe {other.name} {money}." if owed > 0 else f"{other.name} owes you {money}."

    @mcp.tool(
        title="Recent activity",
        icons=[ICONS["recent_activity"]],
        description="List what was recorded lately, most recent first.",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def recent_activity(
        limit: Annotated[int, Field(description="How many entries", ge=1, le=25)] = 5,
    ) -> str:
        household = current_household()
        live = [e for e in reversed(household.entries) if e.is_live][:limit]
        if not live:
            return f"Nothing recorded in {household.name} yet."
        lines = [
            f"{household.name_of(e.payer_id)} paid {say_amount(e.total, household.currency)} for {e.description}"
            for e in live
        ]
        return "; ".join(lines) + "."

    return mcp


def _preconfigured_client() -> OAuthClientInformationFull | None:
    """The client Alexa+ will present, since it cannot register itself.

    Set `TALLY_CLIENT_ID` and `TALLY_REDIRECT_URI` (comma-separated if several)
    to the values from the Alexa developer console.
    """
    client_id = os.environ.get("TALLY_CLIENT_ID")
    redirects = os.environ.get("TALLY_REDIRECT_URI", "")
    if not client_id or not redirects:
        return None
    secret = os.environ.get("TALLY_CLIENT_SECRET")
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=[AnyUrl(uri.strip()) for uri in redirects.split(",") if uri.strip()],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="ledger",
        token_endpoint_auth_method="client_secret_post" if secret else "none",
    )


def _spoken_summary(household: Household, transfers: list[Any]) -> str:
    """One sentence a person can act on after hearing it once.

    A balance table read aloud is useless, so this leads with what to do.
    """
    if not any(e.is_live for e in household.entries):
        return f"Nothing recorded in {household.name} yet."
    if not transfers:
        return f"Everyone in {household.name} is square."
    if len(transfers) == 1:
        t = transfers[0]
        return (
            f"{household.name_of(t.from_id)} owes {household.name_of(t.to_id)} "
            f"{say_amount(t.amount, household.currency)}."
        )
    biggest = max(transfers, key=lambda t: t.amount)
    return (
        f"{len(transfers)} payments settle {household.name}. The biggest: "
        f"{household.name_of(biggest.from_id)} owes {household.name_of(biggest.to_id)} "
        f"{say_amount(biggest.amount, household.currency)}."
    )
