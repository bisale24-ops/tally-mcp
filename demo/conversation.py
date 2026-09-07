"""Drive a scripted conversation against a running Tally server.

The Alexa+ web simulator is limited to partner accounts, so this stands in for
the voice loop: each line is an utterance a person would say, it is turned into
the tool call Alexa+ would make, and the server's own spoken answer is printed -
and, with --speak, read aloud by the system voice.

Nothing here is a mock. Every reply is what the MCP server actually returned
over Streamable HTTP.

    uv run python -m tally --port 8000 &
    uv run python demo/conversation.py --speak
"""

from __future__ import annotations

import argparse
import functools
import shutil
import subprocess
import sys
import time

import anyio
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE

APPS_EXTENSION = "io.modelcontextprotocol/ui"

# (what a person says, the tool Alexa+ would route it to, the arguments)
SCRIPT: list[tuple[str, str, dict]] = [
    (
        "Alexa, start a household called Apartment 4B.",
        "start_household",
        {"name": "Apartment 4B", "currency": "USD", "your_name": "Sam"},
    ),
    ("Add Chris to the apartment.", "add_person", {"name": "Chris"}),
    ("Add Maya.", "add_person", {"name": "Maya"}),
    ("Add Dana.", "add_person", {"name": "Dana"}),
    (
        "I paid a hundred and thirty two dollars for dinner.",
        "record_expense",
        {"amount": "132.00", "description": "dinner"},
    ),
    (
        "Chris paid thirty four fifty for the Uber home, just me and him.",
        "record_expense",
        {
            "amount": "34.50",
            "description": "the Uber home",
            "paid_by": "Chris",
            "split_between": ["Sam", "Chris"],
        },
    ),
    ("Who owes what?", "show_balances", {}),
    ("Maya paid me back thirty three dollars.", "settle_up", {"amount": "33.00", "to": "Sam", "paid_by": "Maya"}),
    ("No, cancel that.", "undo_last", {}),
    ("What did we spend lately?", "recent_activity", {"limit": 3}),
]

RESET, DIM, BOLD, BLUE, GREEN = "\033[0m", "\033[2m", "\033[1m", "\033[34m", "\033[32m"


# Best first. The Premium voices are free but downloaded on demand, so the
# recording machine may not have them; fall back rather than fail silently.
VOICES = ("Ava (Premium)", "Zoe (Premium)", "Allison", "Samantha")


def installed_voices() -> set[str]:
    say = shutil.which("say")
    if not say:
        return set()
    listed = subprocess.run([say, "-v", "?"], capture_output=True, text=True, check=False)  # noqa: S603
    return {line.split("  ")[0].strip() for line in listed.stdout.splitlines() if line.strip()}


def pick_voice(preferred: str | None) -> str | None:
    """The requested voice if it exists, else the best one that does."""
    available = installed_voices()
    if not available:
        return None
    if preferred and preferred in available:
        return preferred
    if preferred:
        print(f"{DIM}     ({preferred!r} is not installed; falling back){RESET}", file=sys.stderr)
    return next((v for v in VOICES if v in available), None)


def speak(text: str, voice: str | None) -> None:
    """Read a reply aloud with the system voice, when there is one."""
    say = shutil.which("say")
    if say and voice:
        subprocess.run([say, "-v", voice, text], check=False)  # noqa: S603 - fixed argv, no shell


async def run(url: str, *, out_loud: bool, voice: str | None, pause: float) -> None:
    async with Client(url, extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})]) as client:
        for utterance, tool, args in SCRIPT:
            print(f"\n{BOLD}{BLUE}you  {RESET} {utterance}")
            print(f"{DIM}     -> {tool}({', '.join(f'{k}={v!r}' for k, v in args.items())}){RESET}")

            started = time.perf_counter()
            result = await client.call_tool(tool, args)
            elapsed = (time.perf_counter() - started) * 1000

            said = " ".join(b.text for b in result.content if b.type == "text")
            marker = "!!" if result.is_error else "  "
            print(f"{BOLD}{GREEN}alexa{RESET}{marker} {said}")
            payload = result.structured_content or {}
            shown = " | balance sheet pushed to the screen" if "balances" in payload else ""
            print(f"{DIM}     {elapsed:.0f} ms{shown}{RESET}")

            if out_loud:
                speak(said, voice)
            await anyio.sleep(pause)


def main() -> None:
    parser = argparse.ArgumentParser(prog="conversation", description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--speak", action="store_true", help="Read the answers aloud.")
    parser.add_argument("--voice", default=None, help=f"System voice; defaults to the best of {', '.join(VOICES)}.")
    parser.add_argument("--pause", type=float, default=1.2, help="Seconds between turns.")
    args = parser.parse_args()
    chosen = pick_voice(args.voice) if args.speak else None
    if args.speak:
        print(f"{DIM}voice: {chosen or 'none available - printing only'}{RESET}")

    try:
        anyio.run(functools.partial(run, args.url, out_loud=args.speak, voice=args.voice, pause=args.pause))
    except Exception as exc:  # the server is not up, or the URL is wrong
        print(f"Could not talk to {args.url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
