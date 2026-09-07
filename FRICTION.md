# Friction log

Written while building Tally against the Alexa+ MCP guidance, the MCP Python
SDK 2.2.0, and the MCP Apps extension. Ordered by how much time each cost.

---

### 1. The Alexa+ docs and the hackathon rules contradict each other on access

**Severity: high** — this one nearly stopped the project before it started.

**Attempted:** Work out whether a solo developer outside the US can build for
the Alexa+ track at all.

**Steps:** Read `developer.amazon.com/alexaplus/`, then the MCP Toolkit
overview, then the hackathon track description.

**Expected vs. actual:** The builder page says Alexa+ for Builders is
"currently available to select partners working directly with our team", and
the MCP Toolkit overview says the toolkit "is available in the United States".
Read on their own, both say: you cannot enter this track. The hackathon page
says the opposite — "build a self-hosted MCP server (spec 2025-11-25 or later,
Streamable HTTP)", with no partner status and no US requirement, because a
self-hosted server is judged from your repo and demo rather than from a
published add-on.

**Workaround:** Trusted the hackathon page over the docs and built a
self-hosted server that never needs `alexa-ai deploy` to be demonstrable.

**Suggestion:** Add one line to the MCP Toolkit overview separating *building
and testing a server* (open to anyone) from *publishing an add-on to customers*
(US, partners). Right now the geographic and partner limits read as though they
gate the SDK itself, and that is the difference between entering the track and
skipping it.

---

### 2. Apps tools registered after the server is constructed fail silently

**Severity: high** — wrong behaviour, no error, and the UI simply never appears.

**Attempted:** Bind the balance sheet to `show_balances` and three other tools.

**Steps:** Created `Apps()`, constructed `MCPServer(extensions=[apps])`, then
declared tools with `@mcp.tool(...)` and registered the `ui://` resource.

**Expected vs. actual:** Expected either the tools to pick up the UI binding, or
a complaint that they had not. Got neither: the server started, `tools/list`
returned every tool, `resources/list` returned the `ui://` resource, and
everything looked correct — but no tool carried `_meta.ui.resourceUri`, so no
host would ever render the app. The only symptom is a UI that does not show up.

**Workaround:** Register UI-bound tools with `@apps.tool(resource_uri=...)`
*before* `MCPServer(extensions=[...])` is constructed.

**Suggestion:** `Apps.tools()` already raises a good error for the inverse
mistake (a tool bound to a resource that was never registered). Apply the same
care here: have `Apps` refuse further `tool()` registrations once it has been
consumed by a server, with a message naming the ordering requirement. A silent
no-op on the extension's main entry point is the single easiest way to lose an
afternoon.

---

### 3. `resource_server_url` must be the MCP endpoint, not the origin

**Severity: medium-high** — every authenticated request 401s, after a flow that
otherwise completed perfectly.

**Attempted:** Turn on OAuth with the server's public URL.

**Steps:** Set `issuer_url` and `resource_server_url` both to
`https://<host>`, ran the full authorization code flow, then called `/mcp` with
the resulting bearer token.

**Expected vs. actual:** Expected the token to be accepted — it had just been
issued by this very server, seconds earlier. Got a 401. The client had sent
`resource=https://<host>/mcp` per RFC 8707, which is correct, and the server
compared it against `https://<host>/`, which is also defensible; they simply
disagreed about what "the resource" means.

**Workaround:** `resource_server_url = origin + "/mcp"`.

**Suggestion:** The log line (`Bearer token resource '...' is not
resource_server_url ...`) is genuinely excellent and is what solved it — please
keep it. What would prevent the bug entirely: since `MCPServer` already knows
its `streamable_http_path`, either derive `resource_server_url` from it by
default, or warn at startup when a configured `resource_server_url` has no path
while the transport is mounted under one.

---

### 4. Elicitation raises on transports without a back-channel, and nothing says so

**Severity: medium** — turns a graceful "which of them did you mean?" into a
crashed tool call.

**Attempted:** Ask the user to disambiguate two flatmates whose names both
match what was heard.

**Steps:** Called `ctx.elicit(...)` from inside a tool.

**Expected vs. actual:** Expected a declined or cancelled `ElicitationResult`
when the host cannot ask. Got `NoBackChannelError` raised out of the tool body,
which the server then reported as an unexpected crash. The default in-process
client mode has no back-channel either, so the first symptom appeared in the
test suite rather than against a real host.

**Workaround:** Wrap every `ctx.elicit` in `except (NoBackChannelError,
MCPError)` and degrade to putting the question in the error message. In tests,
`Client(server, mode="legacy")` provides a back-channel.

**Suggestion:** Document on `Context.elicit` that it raises on back-channel-less
transports, and say which client modes have one. Better still, expose something
like `ctx.can_elicit` so a tool can branch before composing a question it cannot
ask — the fallback text is usually worded differently from the interactive one.

---

### 5. MCP Apps is specified in a second repository, on its own version line

**Severity: low-medium** — costs orientation time rather than debugging time.

**Attempted:** Find the authoritative field names for `_meta.ui`.

**Steps:** Searched the main specification, then found the real contract in
`modelcontextprotocol/ext-apps` under `specification/2026-01-26/apps.mdx`.

**Expected vs. actual:** Alexa+ asks for "spec 2025-11-25 or later". MCP Apps
carries `2026-01-26`. Two date-shaped version strings from two repositories,
and no statement of how they relate — it is not obvious whether an Apps-using
server still satisfies a 2025-11-25 requirement.

**Workaround:** Treated them as independent: core protocol version from the
core spec, extension shape from `ext-apps`.

**Suggestion:** State the relationship in one sentence at the top of the Apps
spec — extensions version independently of the core protocol, and here is the
minimum core version this extension needs.

---

### 6. Live updates are unreachable at the version Alexa+ negotiates

**Severity: medium-high** — a shared display silently goes stale, and the
feature that would fix it cannot be served.

**Attempted:** Notify a host when another flatmate records an expense, so the
balance sheet on an Echo Show does not sit there wrong.

**Steps:** Called `ctx.notify_resource_updated(...)` from the tools that change
the ledger, then subscribed from a client and waited.

**Expected vs. actual:** Nothing arrived. `notify_resource_updated` publishes
only to `subscriptions/listen` streams, which exist from protocol 2026-07-28.
Alexa+ requires 2025-11-25, where the mechanism is `resources/subscribe` — and
`MCPServer` does not serve it: the client gets `Method not found`. The lowlevel
server does accept `on_subscribe_resource` handlers, but the high-level server
never wires them, and `_lowlevel_server` is private. So on the version the
target host actually speaks, there is no supported way to push an update.

**Workaround:** Notify on the modern channel (verified working with a client
that negotiates 2026-07-28), and have our own screen harness poll instead.

**Suggestion:** Either surface `on_subscribe_resource` on `MCPServer` for
pre-2026-07-28 clients, or make `notify_resource_updated` fan out to both
mechanisms. Failing that, say plainly in its docstring that servers targeting a
2025-11-25 host have no push path — right now the method looks like it works
everywhere, and the failure is silent.

---

### 7. Credit where it is due

Three things in the Python SDK were better than they had to be, and each saved
real time:

- The `mcp.server.fastmcp` import error does not merely fail — it names the new
  class, gives the new import path, links the migration guide, *and* tells you
  the pin that keeps v1 code working. Every renamed module should fail like this.
- `Apps.tools()` validating that each bound `resourceUri` actually resolves
  turns a silent 404-at-render into a startup error.
- Shipping the OAuth authorization server — discovery, PKCE verification,
  registration, revocation — meant the only part I had to write was the part
  only I could write: deciding which human is on the other end.
