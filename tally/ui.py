"""The `ui://` app resource: a balance sheet for screen devices.

MCP Apps (`io.modelcontextprotocol/ui`): JSON-RPC over `postMessage`, a
`ui/initialize` handshake, then `ui/notifications/tool-result` on every run.
"""

from __future__ import annotations

BALANCE_APP = """
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff; --fg: #10151c; --muted: #5c6b7a;
    --line: #e3e8ee; --owed: #0a7d4f; --owes: #b3261e; --chip: #f3f6f9;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #10151c; --fg: #f2f5f8; --muted: #9aa8b6;
      --line: #253040; --owed: #4ade80; --owes: #ff8a80; --chip: #1a2230;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    padding: 20px;
  }
  h1 { font-size: 21px; margin: 0 0 2px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 14px; margin-bottom: 18px; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); font-weight: 600;
    padding: 0 0 8px; border-bottom: 1px solid var(--line);
  }
  td { padding: 11px 0; border-bottom: 1px solid var(--line); }
  td.amt { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
  .owed { color: var(--owed); } .owes { color: var(--owes); } .level { color: var(--muted); }
  h2 {
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--muted); margin: 22px 0 10px; font-weight: 600;
  }
  .pay {
    display: flex; align-items: center; gap: 10px; padding: 10px 12px;
    background: var(--chip); border-radius: 10px; margin-bottom: 8px;
  }
  .pay .who { flex: 1; }
  .pay .amt { font-variant-numeric: tabular-nums; font-weight: 600; }
  .arrow { color: var(--muted); }
  .empty { color: var(--muted); padding: 28px 0; text-align: center; }
  /* An Echo Show is read from across a room, not at arm's length. */
  @media (min-width: 900px) {
    body { padding: 30px 42px; }
    h1 { font-size: 30px; } .sub { font-size: 17px; }
    td { font-size: 21px; padding: 15px 0; } .pay { font-size: 20px; }
  }
</style>

<h1 id="title">Balances</h1>
<div class="sub" id="subtitle">Waiting for the ledger&hellip;</div>
<div id="body"><div class="empty">&hellip;</div></div>

<script>
(function () {
  "use strict";

  // --- JSON-RPC over postMessage -----------------------------------------
  var nextId = 1;
  var pending = {};

  function send(message) {
    window.parent.postMessage(Object.assign({ jsonrpc: "2.0" }, message), "*");
  }

  function request(method, params) {
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      pending[id] = { resolve: resolve, reject: reject };
      send({ id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    send({ method: method, params: params || {} });
  }

  window.addEventListener("message", function (event) {
    var msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;

    if (msg.id !== undefined && pending[msg.id]) {
      var slot = pending[msg.id];
      delete pending[msg.id];
      msg.error ? slot.reject(msg.error) : slot.resolve(msg.result);
      return;
    }
    if (msg.method === "ui/notifications/tool-result") {
      var content = msg.params && msg.params.structuredContent;
      if (content) render(content);
    }
  });

  // --- rendering ----------------------------------------------------------
  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function render(data) {
    document.getElementById("title").textContent = data.household || "Balances";
    document.getElementById("subtitle").textContent = data.summary || "";

    var rows = (data.balances || []).map(function (b) {
      var cls = b.minor > 0 ? "owed" : (b.minor < 0 ? "owes" : "level");
      return "<tr><td>" + esc(b.name) + "</td>" +
             "<td class='amt " + cls + "'>" + esc(b.display) + "</td></tr>";
    }).join("");

    var html = rows
      ? "<table><thead><tr><th>Person</th><th style='text-align:right'>Net</th></tr></thead><tbody>"
        + rows + "</tbody></table>"
      : "<div class='empty'>Nothing recorded yet.</div>";

    if (data.settle && data.settle.length) {
      html += "<h2>Settle up in " + data.settle.length +
              (data.settle.length === 1 ? " payment" : " payments") + "</h2>";
      html += data.settle.map(function (t) {
        return "<div class='pay'><span class='who'>" + esc(t.from) +
               " <span class='arrow'>&rarr;</span> " + esc(t.to) +
               "</span><span class='amt'>" + esc(t.display) + "</span></div>";
      }).join("");
    } else if (rows) {
      html += "<h2>Settled</h2><div class='pay'>Everyone is square.</div>";
    }

    document.getElementById("body").innerHTML = html;
    notify("ui/notifications/size-changed", {
      height: document.documentElement.scrollHeight
    });
  }

  // --- lifecycle ----------------------------------------------------------
  request("ui/initialize", {
    appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] }
  }).then(function () {
    notify("ui/notifications/initialized", {});
  }).catch(function () {
    // A host that skips the handshake still gets a readable page.
    document.getElementById("subtitle").textContent = "Ledger unavailable.";
  });
})();
</script>
"""
