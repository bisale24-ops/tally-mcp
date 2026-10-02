"""Tally — the cards around the live recording. Narrated by TTS; no presenter.

    PART=intro ~/.venvs/video/bin/python ~/Desktop/KHLab/hack-nation/kit/video/render.py video/script.py --out video/build/intro.mp4
    PART=outro ~/.venvs/video/bin/python ~/Desktop/KHLab/hack-nation/kit/video/render.py video/script.py --out video/build/outro.mp4

The middle of the video is video/build/demo.mp4 from record_demo.py: the live playground,
two synthetic voices, nothing scripted. video/assemble.sh joins the three.
"""

import os

VOICE = "en-US-AndrewNeural"
W, H = 1280, 720
BACKGROUND = "0x0b0f14"
TAIL = 0.6

STYLE = """
 @import url("https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;600&display=swap");
 body { margin:0; width:1280px; height:720px; background:#0b0f14; color:#eef2f6;
   font:22px/1.5 'Instrument Sans', system-ui, -apple-system, sans-serif; display:flex; flex-direction:column;
   justify-content:center; padding:0 80px; box-sizing:border-box; }
 h1 { font-size:46px; font-weight:600; margin:0 0 8px; letter-spacing:-.015em; }
 .sub { color:#9aa6b2; margin:0 0 28px; font-size:23px; max-width:980px; }
 .tag { color:#19d3ff; font:15px ui-monospace,Menlo,monospace; letter-spacing:.08em; text-transform:uppercase; margin-bottom:12px; }
 .grid { display:grid; grid-template-columns:1fr 1fr; gap:18px 28px; }
 .item { background:#11171f; border:1px solid #1f2834; border-radius:14px; padding:16px 20px; }
 .item b { display:block; font-size:21px; margin-bottom:4px; font-weight:600; }
 .item span { color:#9aa6b2; font-size:18px; }
 code { font-family:ui-monospace,Menlo,monospace; color:#c4b5fd; font-size:.92em; }
 .big { font-size:30px; } .ring { color:#19d3ff; }
 .foot { color:#7f8b98; font-size:17px; margin-top:26px; }
"""

INTRO = [
    (
        "card:hook",
        "Splitting a bill is not a maths problem. It is a capture problem: nobody opens an app at the table. "
        "Tally is a shared household ledger you talk to, built as an MCP server for Alexa plus. "
        "The Alexa plus simulator is partner only, so here is a page anyone can open. Everything that follows is live.",
    ),
]

OUTRO = [
    (
        "card:routing",
        "The model only routes. Its reply is constrained by a schema to the tools the server publishes, so it "
        "cannot call anything else. It never sets the retry key, because one it invented would swallow the next "
        "genuine repayment. A slow call is raced by a second copy, and if both fail, a pattern router answers and says so.",
    ),
    (
        "card:server",
        "Underneath is the same server Alexa plus would reach. Money is integers end to end. A name it cannot "
        "resolve is asked about, never guessed. OAuth two point one with PKCE, an MCP Apps screen, and two hundred "
        "and sixty five tests in eight layers.",
    ),
    (
        "card:end",
        "Try it yourself at tally playground dot on render dot com. The friction log in the repository says "
        "what cost time with the toolkit, and what would fix it.",
    ),
]

SCENES = INTRO if os.environ.get("PART", "intro") == "intro" else OUTRO

CARDS = {
    "hook": """<div class=tag>Alexa+ · MCP server</div>
      <h1>Tally</h1>
      <p class=sub>A shared household ledger you talk to. Say who paid for what on the kitchen Echo;
      Tally works out the fewest payments that settle everyone up.</p>
      <div class=grid>
        <div class=item><b>Live, not scripted</b><span>A language model routes each sentence to the server's own tools.</span></div>
        <div class=item><b>Two synthetic voices</b><span>Sam, the flatmate, and the device reading Tally's own words.</span></div>
      </div>""",
    "routing": """<div class=tag>How the routing is kept honest</div>
      <h1>The model picks a tool. That is all it does.</h1>
      <div class=grid style="margin-top:18px">
        <div class=item><b>Schema-constrained</b><span>One branch per published tool, each with its own arguments.</span></div>
        <div class=item><b>No invented retry keys</b><span><code>idempotency_key</code> is withheld from the model.</span></div>
        <div class=item><b>Raced when slow</b><span>A second copy after seven seconds; first answer wins.</span></div>
        <div class=item><b>Labelled fallback</b><span>Without a model, a pattern router answers, and the page says so.</span></div>
      </div>""",
    "server": """<div class=tag>The server Alexa+ would reach</div>
      <h1>Correct before clever</h1>
      <div class=grid style="margin-top:18px">
        <div class=item><b>Integer money</b><span>Odd cents rotate; balances always sum to zero.</span></div>
        <div class=item><b>Never guesses a name</b><span>"An" with Anna and Andrew asks, over MCP elicitation.</span></div>
        <div class=item><b>OAuth 2.1 + PKCE</b><span>Invite codes link a device; replayed codes are refused.</span></div>
        <div class=item><b>265 tests, 8 layers</b><span>Property-based, concurrency, hostile input, real OAuth.</span></div>
      </div>""",
    "end": """<h1 class=big>Try it: <span class=ring>tally-playground.onrender.com</span></h1>
      <p class=sub>Source, tests and the friction log: github.com/bisale24-ops/tally-mcp</p>
      <p class=foot>Built by KHLab for Build, Ship, Shape — the Amazon Developer Hackathon · Alexa+ track</p>""",
}
