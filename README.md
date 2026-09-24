# Tally — a shared household ledger you talk to

[![tests](https://github.com/bisale24-ops/tally-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/bisale24-ops/tally-mcp/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)

An MCP server for Alexa+ that tracks who paid for what in a household, and works
out the fewest payments that settle everyone up.

```
"Alexa, Sam paid 132 dollars for dinner, split with Chris and Maya."
   → Recorded 132 dollars for dinner, paid by Sam, split 3 ways.
     That's 44 dollars each.

"Alexa, who owes what?"
   → 2 payments settle Apartment 4B. The biggest: Maya owes Sam 44 dollars.
```

## Why voice, and why a shared device

Splitting expenses is not an arithmetic problem — a calculator solved that
decades ago. It is a **capture** problem. Nobody unlocks a phone and opens an
app at a restaurant table, so the expense goes unrecorded, and a week later the
evening is settled from memory and somebody quietly eats the difference.

Voice removes the capture cost: you say it while the bill is still in your hand.

And Alexa is not a personal device — it sits in the kitchen and belongs to
everyone in the apartment. A shared ledger on a shared device is something a
per-phone app structurally cannot be: any flatmate can record a purchase or ask
where things stand without installing anything.

Being shared is also why the payer gets named out loud. "Sam paid for dinner"
works from the kitchen counter whoever is standing at it; "I paid" only means
something on an account linked to one person, which is what the invite code
establishes.

## What it does

| Tool | Spoken example |
|---|---|
| `record_expense` | "Sam paid 132 for dinner, split with Chris and Maya" |
| `show_balances` | "Who owes what?" |
| `settle_up` | "Chris paid Sam back 44" |
| `undo_last` | "No — cancel that" |
| `add_person` | "Add Dana to the apartment" |
| `start_household` | "Start a household called Apartment 4B" |
| `what_do_i_owe` | "How much do I owe Chris?" |
| `recent_activity` | "What did we spend lately?" |

## Three decisions that shaped the build

**The spoken answer is the product; the screen is a bonus.** Every tool returns
a sentence written to be *heard once* — it leads with what you should do, not
with a table nobody can hold in their head. `client_supports_apps()` decides
whether a screen exists at all: an Echo Show additionally gets the
[MCP Apps](https://modelcontextprotocol.io/specification/draft/extensions/apps)
balance sheet, an Echo Dot loses nothing but the picture. Both paths are tested.

**Never guess whose money it is.** Speech recognition is lossy, so names get
resolved exactly, then by *unambiguous* prefix. "An" with both Anna and Andrew
in the apartment does not pick one — it asks, over MCP elicitation, and falls back to
putting the question in the error text when the host has no back-channel.
Misattributing an expense is worse than one extra question.

**Money is integers, end to end.** Every amount is a count of minor units, and
floats never touch the arithmetic. A three-way split of 10.00 cannot be equal,
so the odd cent is handed out on a rotation instead of always landing on the
same person. Property-based tests assert the invariants that matter: balances
always sum to zero, and the suggested transfers actually clear the ledger.

## Architecture

```
tally/
  money.py    Minor-unit arithmetic; equal / weighted / exact splits
  ledger.py   Households, entries, balances, debt simplification
  store.py    SQLite persistence (WAL)
  server.py   MCP server: tools, Apps binding, elicitation
  ui.py       The ui:// app resource for screen devices
  auth.py     OAuth 2.1 authorization server (PKCE, rotation)
  login.py    Account linking — redeeming an invite code
```

The ledger domain has no MCP dependency and no I/O, which is why its invariants
can be hammered with generated input in milliseconds.

**Settling up** uses the standard greedy max-debtor/max-creditor match. Each
step zeroes at least one person, so it never needs more than `n-1` transfers,
against the `n(n-1)/2` of paying everybody back individually. (Finding the true
minimum is NP-hard; this is optimal unless a strict subset happens to balance
among itself.)

## Running it

```bash
uv sync
uv run pytest                        # 243 tests
uv run python -m tally --stdio       # for the MCP Inspector
```

### Seeing it work without an Echo

Alexa+ add-on publishing is limited to partners in the US, so the two scripts in
`demo/` stand in for the device. Run all three in separate terminals:

```bash
uv run python -m tally --port 8000
uv run python demo/mock_host.py       # http://127.0.0.1:8977
uv run python demo/conversation.py --speak
```

`conversation.py` plays a scripted exchange against the running server: each
line is an utterance, the tool call Alexa+ would route it to, and the server's
own spoken answer — read aloud by the system voice, with per-call latency.
Nothing is stubbed; every reply came over Streamable HTTP.

`mock_host.py` renders the `ui://` app at Echo Show 15 and Echo Show 5 sizes,
performing the same `ui/initialize` handshake a real host would and logging
every message the app sends back. It follows the live server, so the balance
sheet fills in as the conversation runs.

### Speech and screen are rendered differently, on purpose

Alexa reads a tool's text aloud verbatim, so the same amount is written twice:
`42 dollars 50 cents` in the spoken answer, `$42.50` in the structured payload
a screen renders. `"42.50 USD"` would be read out as "forty two point five zero
U S D".

Over HTTP, the transport Alexa+ uses:

```bash
uv run python -m tally --port 8000
```

### Connecting it to Alexa+

Alexa needs a public HTTPS origin and OAuth. Expose the local server and pass
the public URL — that switch turns on discovery metadata, the authorization code
flow with PKCE, and the account-linking page:

```bash
cloudflared tunnel --url http://localhost:8000
uv run python -m tally --port 8000 --public-url https://<your-tunnel>.trycloudflare.com
```

Alexa+ does not register itself, so it needs a client configured up front.
Without `TALLY_CLIENT_ID` and `TALLY_REDIRECT_URI` (the values from the Alexa
developer console) account linking cannot start at all:

```bash
export TALLY_CLIENT_ID=... TALLY_REDIRECT_URI=https://layla.amazon.com/api/skill/link/...
```

Then onboard the add-on:

```bash
alexa-ai configure
alexa-ai new mcp --name "Tally" --locale en-US \
  --mcp-server-url https://<your-tunnel>.trycloudflare.com/mcp
alexa-ai deploy
```

Without `--public-url` the server runs unauthenticated — fine for the Inspector
and the test suite, never for a real device.

## Joining a household

A name is not a secret. Adding someone creates a placeholder member and a
single-use invite code, which the person who set the household up passes on;
linking a device redeems that code. Matching on the name instead would let
anyone who guessed a flatmate's first name attach their own Alexa to somebody
else's ledger.

## Protocol conformance

- **MCP spec 2025-11-25+** over Streamable HTTP.
- **MCP Apps** (`io.modelcontextprotocol/ui`): a `ui://` resource served as
  `text/html;profile=mcp-app`, bound to four tools via `_meta.ui.resourceUri`,
  with a `ui/initialize` handshake and no network access at all in its CSP.
- **OAuth 2.1**: `/.well-known/oauth-authorization-server`, authorization code
  + PKCE (S256), dynamic client registration, refresh-token rotation, and
  RFC 8707 resource validation so a token minted for another server is refused.
- **Tool annotations**: read-only and destructive hints, so a host knows what
  needs confirming before it runs.
- **Resources and prompts**: the ledger is readable at `tally://household` as
  JSON — reading where things stand is not a side effect and should not need a
  tool call — and three prompts give a host somewhere to start.
- **Change notifications**: tools that move the ledger publish a resource
  update, so a listening host can refresh instead of going stale. This reaches
  `subscriptions/listen` hosts (2026-07-28); at the 2025-11-25 version Alexa+
  negotiates there is no push path at all — see FRICTION.md.
- **Structured output**: every ledger tool publishes an `outputSchema` and
  always returns structured content — what a screen changes is how much has to
  be said out loud, not what is sent.
- **Latency**: ~15 ms median per tool call over HTTP, against the 500 ms
  Alexa+ allows.

## Testing

243 tests, in seven layers:

- **Property-based** (Hypothesis) over the money and ledger invariants —
  conservation, fairness bounds, settlement correctness.
- **End-to-end over a real MCP client session** — tool schemas, Apps
  negotiation, structured content, elicitation, error shape, persistence
  across sessions.
- **Hostile input** — amounts that look numeric but are not, blank and enormous
  names, nonsense currencies, and shares that cannot be divided. Everything is
  refused with something a person can act on, and nothing reaches the database
  it cannot survive.
- **Concurrency** — two dozen simultaneous writers: nothing is lost, the ledger
  still sums to zero, and no two entries claim the same place in the history.
- **Two people, one ledger** — the claim the product rests on: a second
  flatmate joins, "I" means a different person for each of them, either can undo
  a misheard entry, and a stranger reaches nothing.
- **OAuth over real HTTP** — the whole authorization code flow, plus the
  attacks it exists to stop: replayed codes and forged PKCE verifiers.

See [FRICTION.md](FRICTION.md) for what the Amazon and MCP tooling got right
and where it cost time.

## Contributing

Where help is useful, and how to run the suite: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
