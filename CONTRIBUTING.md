# Contributing

Tally is small on purpose: one MCP server, a ledger, and a settlement algorithm. That makes it
easy to read in an evening and easy to break in ways the tests will catch.

## Running it

```bash
uv run pytest -q          # 246 tests, no network, no Echo needed
uv run tally serve        # the MCP server on stdio
uv run tally demo         # the transcript in the README, without a device
```

Python 3.11+, `uv` for everything. No other tooling.

## What the tests are for

They are not decoration. `test_hostile.py` feeds the server the inputs a real household produces
— the same expense recorded twice, a name that is a prefix of another name, a settlement asked
for mid-edit — and `test_alexa_compat.py` pins the shapes Alexa+ actually accepts. If a change
makes one of those fail, the change is wrong until proven otherwise.

Money is integer cents everywhere (`tally/money.py`). A float in a monetary path is a bug even
when the test passes.

## Sending a change

1. A failing test first, in the file that covers the area.
2. The smallest change that makes it pass.
3. `uv run pytest -q` green before you open the PR.

Keep commit messages about the behaviour, not the file list.

## Where help is genuinely useful

- **Locales.** Amounts and dates are parsed and spoken in English only. The seam is
  `tally/speech.py`; another language needs a parser and a renderer, not changes elsewhere.
- **Settlement.** `tally/settle.py` minimises the number of payments greedily. A provably minimal
  version with the same output shape would be a real improvement, with a test that compares the
  two on random ledgers.
- **Screen rendering.** `tally/render.py` builds the card for devices that have a display. It
  handles one layout; a narrow layout for small screens is wanted.

Open an issue before a large change, so we can agree on the shape first.
