"""Render the ui:// app the way a screen device would, without a screen device.

Alexa+ add-on publishing is limited to partners in the US, so the balance sheet
cannot be checked on a real Echo Show from here. This stands in for one: it
performs the same `ui/initialize` handshake, pushes tool results into the app,
and logs every message the app sends back.

It follows a live Tally server, so the table fills in while a conversation runs
against it. With no server up it falls back to a fixed sample, which is enough
to check the layout.

    uv run python -m tally --port 8000 &
    uv run python demo/mock_host.py               # http://127.0.0.1:8977
    uv run python demo/conversation.py --speak
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this by path puts demo/ on sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import contextlib
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import uvicorn
from mcp import Client
from mcp.client.extension import advertise
from mcp.server.apps import APP_MIME_TYPE
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from tally.ledger import settle
from tally.money import show_amount
from tally.store import Store
from tally.ui import BALANCE_APP

APPS_EXTENSION = "io.modelcontextprotocol/ui"

HOST_PAGE = """<!doctype html><meta charset="utf-8"><title>Mock Alexa+ host</title>
<style>
 body{margin:0;background:#0b0f14;font:14px system-ui;color:#8b97a4}
 .bar{padding:10px 16px;border-bottom:1px solid #1e2733;display:flex;gap:12px;align-items:center}
 .tag{font-size:11px;padding:2px 8px;border-radius:99px;background:#1a2230;color:#7d8b99}
 .live{background:#12331f;color:#4ade80}
 .stage{padding:24px;display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}
 .dev{background:#000;border-radius:14px;overflow:hidden;border:1px solid #223}
 .cap{padding:6px 10px;font-size:12px;color:#67727f}
 iframe{border:0;display:block;background:#fff}
 #log{padding:10px 16px;font:12px ui-monospace,monospace;color:#5c6b7a;white-space:pre-wrap;
      max-height:160px;overflow:auto}
</style>
<div class="bar">
  <span>Mock MCP Apps host &mdash; ui://tally/balances.html</span>
  <span class="tag" id="status">connecting&hellip;</span>
  <span class="tag">Alexa+ simulator is partner-only; this is a stand-in</span>
</div>
<div class="stage">
  <div><div class="cap">Echo Show 15 &mdash; 1280&times;620</div>
    <div class="dev"><iframe id="show" width="1280" height="620"></iframe></div></div>
  <div><div class="cap">Echo Show 5 &mdash; 480&times;480</div>
    <div class="dev"><iframe id="small" width="480" height="480"></iframe></div></div>
</div>
<div id="log"></div>
<script>
const APP = __APP__;
const frames = ["show", "small"];
const ready = new Set();
let lastSeen = null;

const logBox = document.getElementById("log");
const log = m => { logBox.textContent += m + "\\n"; logBox.scrollTop = logBox.scrollHeight; };

for (const id of frames) document.getElementById(id).srcdoc = APP;

function push(frame, payload) {
  frame.postMessage({jsonrpc: "2.0", method: "ui/notifications/tool-result",
    params: {structuredContent: payload}}, "*");
}

window.addEventListener("message", e => {
  const msg = e.data;
  if (!msg || msg.jsonrpc !== "2.0") return;
  if (msg.method === "ui/initialize") {
    log("<- ui/initialize " + JSON.stringify(msg.params));
    e.source.postMessage({jsonrpc: "2.0", id: msg.id, result: {
      hostContext: {theme: "dark", displayMode: "inline"}, hostCapabilities: {}
    }}, "*");
  } else if (msg.method === "ui/notifications/initialized") {
    ready.add(e.source);
    log("<- ui/notifications/initialized");
    if (lastSeen) push(e.source, lastSeen);
  } else {
    log("<- " + msg.method + " " + JSON.stringify(msg.params || {}));
  }
});

async function poll() {
  try {
    const state = await (await fetch("/state")).json();
    document.getElementById("status").textContent = state.source;
    document.getElementById("status").className = "tag" + (state.source === "live" ? " live" : "");
    const stamp = JSON.stringify(state.ledger);
    if (state.ledger && stamp !== JSON.stringify(lastSeen)) {
      lastSeen = state.ledger;
      log("-> ui/notifications/tool-result  (" + state.ledger.balances.length + " people)");
      for (const f of ready) push(f, state.ledger);
    }
  } catch (err) {
    document.getElementById("status").textContent = "host unreachable";
  }
  setTimeout(poll, 500);
}
poll();
</script>"""


def sample_ledger() -> dict[str, Any]:
    """A fixed ledger for when no server is running - the numbers still come from the domain."""
    store = Store(Path(tempfile.mkdtemp()) / "demo.db")
    flat = store.create_household("Apartment 4B", "USD", founder="Sam", principal="demo")
    for name in ("Chris", "Maya", "Dana"):
        store.add_member(flat, flat.add_member(name))

    sam, chris = flat.members[0].id, flat.members[1].id
    store.add_entry(flat, flat.record_expense(payer_id=sam, total=13200, description="dinner"))
    store.add_entry(
        flat,
        flat.record_expense(payer_id=chris, total=3450, description="the Uber home", participant_ids=[sam, chris]),
    )

    balances = flat.balances()
    transfers = settle(balances)
    return {
        "household": flat.name,
        "summary": f"{len(transfers)} payments settle {flat.name}.",
        "balances": [
            {
                "name": flat.name_of(m.id),
                "minor": balances[m.id],
                "display": show_amount(abs(balances[m.id]), flat.currency),
            }
            for m in flat.members
        ],
        "settle": [
            {
                "from": flat.name_of(t.from_id),
                "to": flat.name_of(t.to_id),
                "minor": t.amount,
                "display": show_amount(t.amount, flat.currency),
            }
            for t in transfers
        ],
    }


def build(tally_url: str) -> Starlette:
    page = HOST_PAGE.replace(
        # The app closes its own <script>; unescaped it would close this one too.
        "__APP__",
        json.dumps(BALANCE_APP).replace("</script>", "<\\/script>"),
    ).encode()
    fallback = sample_ledger()
    state: dict[str, Any] = {"client": None}

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        client = Client(tally_url, extensions=[advertise(APPS_EXTENSION, {"mimeTypes": [APP_MIME_TYPE]})])
        try:
            state["client"] = await client.__aenter__()
        except Exception:
            print(f"No Tally server on {tally_url}; showing the sample ledger.")
            state["client"] = None
        yield
        if state["client"] is not None:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    async def home(_: Request) -> HTMLResponse:
        return HTMLResponse(page)

    async def ledger(_: Request) -> JSONResponse:
        client = state["client"]
        if client is None:
            return JSONResponse({"source": "sample", "ledger": fallback})
        try:
            result = await client.call_tool("show_balances", {})
        except Exception:
            return JSONResponse({"source": "sample", "ledger": fallback})
        if result.is_error or not result.structured_content:
            # No household yet: the app shows its empty state rather than stale numbers.
            return JSONResponse({"source": "live", "ledger": None})
        return JSONResponse({"source": "live", "ledger": result.structured_content})

    return Starlette(
        routes=[Route("/", home), Route("/state", ledger)],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock_host", description=__doc__)
    parser.add_argument("--port", type=int, default=8977)
    parser.add_argument("--tally", default="http://127.0.0.1:8000/mcp")
    args = parser.parse_args()
    uvicorn.run(build(args.tally), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
