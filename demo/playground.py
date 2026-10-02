"""Try Tally in a browser: say a sentence, watch Alexa+'s side of it, hear the answer.

The Alexa+ simulator is limited to partner accounts, so anyone judging this from
outside that program has no way to talk to the server. This page is that way in.
It plays the part of the device and nothing else:

  - the browser turns speech into text (or you type),
  - a language model routes the sentence to one of the server's own tools,
    the way Alexa+ does, using only the tool list the server publishes,
  - the tool runs on a real Tally MCP server over a real MCP client session,
  - the server's own text is what gets spoken, verbatim, as on an Echo,
  - and its structured result is pushed into the same ui:// app an Echo Show renders.

Every visitor gets a sandbox household of their own (a fresh SQLite file), so
nobody can see or break anyone else's ledger. If the model is unreachable a
small pattern router takes over, and the page says which of the two answered.

    uv run python demo/playground.py                # http://127.0.0.1:8978
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this by path puts demo/ on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import contextlib
import json
import os
import re
import secrets
import tempfile
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import anyio
import uvicorn
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from tally.server import create_server
from tally.store import Store
from tally.ui import BALANCE_APP

APPS_EXTENSION = "io.modelcontextprotocol/ui"
PRINCIPAL = "dev-principal"  # what the server calls an unauthenticated caller
SPEAKER = "Sam"
FLATMATES = ("Chris", "Maya")
MAX_SANDBOXES = 300
IDLE_SECONDS = 30 * 60
COOKIE = "tally_sandbox"

# Two free endpoints, both OpenAI-shaped. Gemini answers in about a second when a key is
# present; Public AI (Apertus 70B) needs no card but sometimes holds a request for a minute,
# which is why a slow call is raced against a second copy of itself (see `route`).
ENDPOINTS = (
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "env": "GEMINI_API_KEY",
        "file": "gemini.key",
    },
    {
        "name": "publicai",
        "url": "https://api.publicai.co/v1/chat/completions",
        "model": os.environ.get("PUBLICAI_MODEL", "swiss-ai/apertus-v1.5-70b"),
        "env": "PUBLICAI_API_KEY",
        "file": "publicai.key",
    },
)
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "20"))  # one request
HEDGE_AFTER = float(os.environ.get("LLM_HEDGE_AFTER", "7"))  # send a second copy if the first is this slow
GIVE_UP_AFTER = float(os.environ.get("LLM_GIVE_UP_AFTER", "16"))  # then the pattern router answers


def endpoint() -> dict[str, str] | None:
    """The first endpoint with a key, from the environment or ~/.config."""
    for spec in ENDPOINTS:
        key = os.environ.get(spec["env"], "").strip()
        if not key:
            with contextlib.suppress(OSError):
                key = (Path.home() / ".config" / spec["file"]).read_text().strip()
        if key:
            return {**spec, "key": key}
    return None


# -- one visitor's household ---------------------------------------------------


@dataclass
class Sandbox:
    """A private household and the Tally server that owns it.

    The server lives as long as the sandbox; an MCP session is opened per request.
    In-process that costs a few milliseconds, and it keeps every session inside the
    task that opened it - anyio refuses to close one from another task.
    """

    root: Path
    server: Any = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    used: float = field(default_factory=time.monotonic)

    async def open(self) -> None:
        store = Store(self.root / "tally.db")
        flat = store.create_household("Apartment 4B", "USD", founder=SPEAKER, principal=PRINCIPAL)
        for name in FLATMATES:
            store.add_member(flat, flat.add_member(name))
        self.server = create_server(store)
        async with self.session() as session:
            listed = await session.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "schema": t.input_schema} for t in listed.tools
        ]

    def session(self) -> Any:
        return Client(self.server, extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})])

    async def close(self) -> None:
        self.server = None

    @staticmethod
    async def call(session: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        result = await session.call_tool(name, arguments)
        text = " ".join(c.text for c in result.content if getattr(c, "type", "") == "text").strip()
        return {
            "tool": name,
            "arguments": arguments,
            "text": text,
            "error": bool(result.is_error),
            "structured": result.structured_content,
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @staticmethod
    async def ledger(session: Any) -> dict[str, Any] | None:
        result = await session.call_tool("show_balances", {})
        return None if result.is_error else result.structured_content


class Sandboxes:
    """Sandboxes by cookie, oldest dropped first, idle ones reaped."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.items: OrderedDict[str, Sandbox] = OrderedDict()
        self.lock = anyio.Lock()

    async def get(self, sid: str) -> Sandbox:
        async with self.lock:
            box = self.items.get(sid)
            if box is None:
                await self._reap()
                root = self.base / sid
                root.mkdir(parents=True, exist_ok=True)
                box = Sandbox(root)
                await box.open()
                self.items[sid] = box
            self.items.move_to_end(sid)
            box.used = time.monotonic()
            return box

    async def reset(self, sid: str) -> None:
        async with self.lock:
            box = self.items.pop(sid, None)
        if box is not None:
            await box.close()
            with contextlib.suppress(OSError):
                for f in box.root.iterdir():
                    f.unlink()

    async def _reap(self) -> None:
        now = time.monotonic()
        stale = [k for k, b in self.items.items() if now - b.used > IDLE_SECONDS]
        while len(self.items) - len(stale) >= MAX_SANDBOXES:
            stale.append(next(k for k in self.items if k not in stale))
        for k in stale:
            await self.items.pop(k).close()


# -- routing a sentence to a tool ------------------------------------------------

SYSTEM = """You are the routing layer of Alexa+ for one MCP add-on, Tally, a shared household ledger.
The person speaking is {speaker}. The household is "Apartment 4B" with members: {members}.
Turn the sentence into calls to Tally's tools. Rules:
- Call the tool(s) the sentence needs and nothing else. Do not answer in prose when a tool fits.
- Amounts are strings of digits with two decimals: "a hundred and thirty two dollars" -> "132.00".
- "I", "me" or no payer named means the speaker: pass "me" or leave paid_by out.
- "Split with Chris and Maya" includes the speaker: split_between is ["me", "Chris", "Maya"].
  "Just him and me" after "Chris paid" means split_between ["Chris", "me"]. No split named: leave split_between out.
- Only use names the person said. Never invent a name, amount or description.
- If the sentence is not about the household's money, reply with one short sentence instead."""


def _spec(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["schema"]},
        }
        for t in tools
    ]


def route_with_model(sentence: str, tools: list[dict[str, Any]], members: list[str]) -> dict[str, Any]:
    """Ask the model which tools to call. Raises on any failure, so the caller can fall back."""
    spec = endpoint()
    if spec is None:
        raise RuntimeError("no model key")
    body = {
        "model": spec["model"],
        "temperature": 0,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": SYSTEM.format(speaker=SPEAKER, members=", ".join(members))},
            {"role": "user", "content": sentence},
        ],
        # tool_choice is deliberately unset: forcing "auto" makes this model write calls into the text.
        "tools": _spec(tools),
    }
    request = urllib.request.Request(  # noqa: S310 - endpoints are fixed https URLs
        spec["url"],
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {spec['key']}",
            # Cloudflare in front of Public AI refuses urllib's default User-Agent (403, code 1010).
            "user-agent": "tally-playground/1.0 (+https://github.com/bisale24-ops/tally-mcp)",
        },
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as response:  # noqa: S310 - fixed https endpoint
        data = json.load(response)
    message = data["choices"][0]["message"]
    known = {t["name"] for t in tools}
    calls = []
    for call in message.get("tool_calls") or ():
        fn = call.get("function", {})
        if fn.get("name") not in known:
            continue  # a tool the server never published is not called, whatever the model says
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            continue
        if isinstance(args, dict):
            calls.append({"tool": fn["name"], "arguments": args})
    return {
        "router": f"model · {spec['model'].split('/')[-1]}",
        "calls": calls,
        "text": (message.get("content") or "").strip(),
        "ms": round((time.perf_counter() - started) * 1000),
    }


async def route(sentence: str, tools: list[dict[str, Any]], members: list[str]) -> dict[str, Any]:
    """The model's answer, hedged: a second identical request if the first is slow, the patterns if both are.

    Public AI usually answers in 3-7 s but holds roughly one request in three for a minute. A copy
    sent after HEDGE_AFTER usually lands on a free worker and comes back first.
    """
    done = anyio.Event()
    outcome: dict[str, Any] = {}
    failures: list[str] = []

    async def attempt() -> None:
        try:
            result = await anyio.to_thread.run_sync(route_with_model, sentence, tools, members, abandon_on_cancel=True)
        except Exception as exc:  # any failure of one copy; the other may still answer
            failures.append(type(exc).__name__)
            if len(failures) >= 2 or "no model key" in str(exc):
                done.set()
            return
        if not done.is_set():
            outcome.update(result)
            done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(attempt)
        with anyio.move_on_after(HEDGE_AFTER):
            await done.wait()
        if not done.is_set():
            tg.start_soon(attempt)
        with anyio.move_on_after(GIVE_UP_AFTER - HEDGE_AFTER):
            await done.wait()
        tg.cancel_scope.cancel()

    if outcome:
        return outcome
    routed = route_with_patterns(sentence)
    routed["why"] = ", ".join(failures) or "slow"
    return routed


_UNIT_WORDS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TEN_WORDS = [
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]
_UNITS = {w: i for i, w in enumerate(_UNIT_WORDS)}
_TENS = {w: 10 * (i + 2) for i, w in enumerate(_TEN_WORDS)}


def _number(words: list[str]) -> int | None:
    total = 0
    for w in words:
        if w == "a":
            total = max(total, 1)
        elif w == "and":
            continue
        elif w in _UNITS:
            total += _UNITS[w]
        elif w in _TENS:
            total += _TENS[w]
        elif w == "hundred":
            total = max(total, 1) * 100
        else:
            return None
    return total


_WORD = r"(?:a|and|hundred|" + "|".join(sorted(set(_UNITS) | set(_TENS), key=len, reverse=True)) + r")"
_RUN = re.compile(rf"\b{_WORD}(?:[\s-]+{_WORD})*\b", re.I)


def words_to_digits(sentence: str) -> str:
    """'a hundred and thirty two' -> '132', 'thirty four fifty' -> '34.50': how amounts are said aloud."""

    def swap(m: re.Match[str]) -> str:
        words = re.split(r"[\s-]+", m[0].lower())
        if not any(w in _UNITS or w in _TENS or w == "hundred" for w in words):
            return m[0]  # a bare "a" or "and"
        lead = ""
        while words and words[0] == "and":
            words, lead = words[1:], lead + "and "
        # "thirty four fifty": dollars, then a two-digit cents group
        cents = words[-1] if len(words) >= 3 and words[-1] in _TENS and words[-2] in _UNITS else None
        whole = _number(words[:-1] if cents else words)
        if whole is None or (whole == 1 and words == ["a"]):
            return m[0]
        return lead + (f"{whole}.{_TENS[cents]:02d}" if cents else str(whole))

    return _RUN.sub(swap, sentence)


_NUMBER = r"(\d+(?:[.,]\d{1,2})?)"
_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    (
        re.compile(rf"^(?:alexa,?\s*)?(\w+) paid (?:me|{SPEAKER}) back \$?{_NUMBER}", re.I),
        lambda m: ("settle_up", {"paid_by": m[1], "to": "me", "amount": m[2].replace(",", ".")}),
    ),
    (
        re.compile(
            rf"^(?:alexa,?\s*)?(\w+) paid \$?{_NUMBER}(?: dollars)? for (.+?)"
            r"(?:,? (?:split (?:with|between) (.+)|just (?:him|her|them) and me))?[.!]?$",
            re.I,
        ),
        lambda m: (
            "record_expense",
            {
                "paid_by": "me" if m[1].lower() in {"i", "me"} else m[1],
                "amount": m[2].replace(",", "."),
                "description": m[3],
                **(
                    {"split_between": [n for n in re.split(r",\s*|\s+and\s+", m[4].rstrip(".")) if n] + ["me"]}
                    if m[4]
                    else {"split_between": [m[1], "me"]}
                    if re.search(r"just (?:him|her|them) and me", m[0], re.I)
                    else {}
                ),
            },
        ),
    ),
    (re.compile(r"who owes|balances|where do we stand", re.I), lambda m: ("show_balances", {})),
    (re.compile(r"cancel|undo|scratch that", re.I), lambda m: ("undo_last", {})),
    (re.compile(r"\badd (\w+)", re.I), lambda m: ("add_person", {"name": m[1]})),
    (re.compile(r"how much do i owe (\w+)", re.I), lambda m: ("what_do_i_owe", {"person": m[1]})),
    (re.compile(r"spen[dt]|lately|recent", re.I), lambda m: ("recent_activity", {"limit": 3})),
]


def route_with_patterns(sentence: str) -> dict[str, Any]:
    """The fallback: a handful of fixed shapes. Honest about being one."""
    said = words_to_digits(sentence.strip())
    for pattern, build in _PATTERNS:
        m = pattern.search(said)
        if m:
            tool, args = build(m)
            return {"router": "pattern fallback", "calls": [{"tool": tool, "arguments": args}], "text": "", "ms": 0}
    return {
        "router": "pattern fallback",
        "calls": [],
        "text": "The language model is unreachable right now, and I only know a few fixed phrases without it. "
        "Try one of the examples.",
        "ms": 0,
    }


# -- the web app -------------------------------------------------------------------


def build(base: Path | None = None) -> Starlette:
    boxes = Sandboxes(base or Path(tempfile.mkdtemp(prefix="tally-play-")))
    page = (Path(__file__).with_name("playground.html").read_text(encoding="utf-8")).replace(
        "__APP__", json.dumps(BALANCE_APP).replace("</script>", "<\\/script>")
    )
    fresh = {"Cache-Control": "no-store"}

    def sandbox_id(request: Request) -> tuple[str, bool]:
        sid = request.cookies.get(COOKIE, "")
        if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", sid):
            return sid, False
        return secrets.token_urlsafe(18), True

    def with_cookie(response: JSONResponse | HTMLResponse, sid: str, new: bool) -> Any:
        if new:
            response.set_cookie(COOKIE, sid, max_age=IDLE_SECONDS, httponly=True, samesite="lax", secure=False)
        return response

    async def home(request: Request) -> HTMLResponse:
        sid, new = sandbox_id(request)
        return with_cookie(HTMLResponse(page, headers=fresh), sid, new)

    async def state(request: Request) -> JSONResponse:
        sid, new = sandbox_id(request)
        box = await boxes.get(sid)
        async with box.lock, box.session() as session:
            ledger = await box.ledger(session)
        return with_cookie(JSONResponse({"ledger": ledger, "speaker": SPEAKER}, headers=fresh), sid, new)

    async def say(request: Request) -> JSONResponse:
        sid, new = sandbox_id(request)
        payload = await request.json()
        sentence = str(payload.get("text", "")).strip()[:300]
        if not sentence:
            return JSONResponse({"error": "Say something first."}, status_code=400)
        box = await boxes.get(sid)
        async with box.lock, box.session() as session:
            members = [row["name"] for row in ((await box.ledger(session)) or {}).get("balances", [])] or [SPEAKER]
            routed = await route(sentence, box.tools, members)
            results = [await box.call(session, c["tool"], c["arguments"]) for c in routed["calls"][:4]]
            ledger = await box.ledger(session)
        spoken = " ".join(r["text"] for r in results) or routed["text"] or "Sorry, I didn't catch that."
        return with_cookie(
            JSONResponse({"heard": sentence, "routed": routed, "results": results, "spoken": spoken, "ledger": ledger}),
            sid,
            new,
        )

    async def reset(request: Request) -> JSONResponse:
        sid, new = sandbox_id(request)
        await boxes.reset(sid)
        return with_cookie(JSONResponse({"ok": True}), sid, new)

    async def health(_: Request) -> JSONResponse:
        spec = endpoint()
        return JSONResponse({"ok": True, "sandboxes": len(boxes.items), "model": spec and spec["model"]})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):  # type: ignore[no-untyped-def]
        yield
        for box in list(boxes.items.values()):
            await box.close()

    return Starlette(
        routes=[
            Route("/", home),
            Route("/state", state),
            Route("/say", say, methods=["POST"]),
            Route("/reset", reset, methods=["POST"]),
            Route("/healthz", health),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="playground", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8978")))
    args = parser.parse_args()
    uvicorn.run(build(), host=args.host, port=args.port, log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
