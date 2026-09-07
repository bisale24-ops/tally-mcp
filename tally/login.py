"""The account-linking page: which member of the household is signing in.

Identity here is proved by an invite code, not by typing a name. A name is not a
secret - anyone who guessed a flatmate's first name could otherwise attach their
own Alexa to somebody else's ledger.
"""

from __future__ import annotations

import secrets
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .auth import TallyAuthProvider
from .store import Store

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link Tally to Alexa</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    display: grid; place-items: center; min-height: 100vh; margin: 0;
    background: Canvas; color: CanvasText;
  }}
  form {{ width: min(380px, 90vw); }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  p {{ color: color-mix(in srgb, CanvasText 60%, Canvas); margin: 0 0 22px; }}
  label {{ display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
  input {{
    width: 100%; padding: 11px 13px; font: inherit; border-radius: 9px;
    border: 1px solid color-mix(in srgb, CanvasText 25%, Canvas);
    background: Canvas; color: CanvasText;
  }}
  button {{
    width: 100%; margin-top: 16px; padding: 12px; font: inherit; font-weight: 600;
    border: 0; border-radius: 9px; background: #2f6bff; color: #fff; cursor: pointer;
  }}
  .err {{ color: #b3261e; margin-bottom: 16px; }}
</style></head>
<body>
  <form method="post">
    <h1>Link Tally to Alexa</h1>
    <p>Enter the invite code from whoever set up the household. Expenses you
       record by voice will then be attributed to you.</p>
    {error}
    <input type="hidden" name="pending" value="{pending}">
    <label for="code">Invite code</label>
    <input id="code" name="code" autofocus required autocomplete="off"
           spellcheck="false" placeholder="ABCD-2345">
    <button type="submit">Link account</button>
  </form>
</body></html>
"""


def render(pending: str, error: str = "") -> HTMLResponse:
    """Both values are escaped: `pending` arrives from the request body."""
    block = f'<div class="err">{escape(error)}</div>' if error else ""
    return HTMLResponse(_PAGE.format(pending=escape(pending, quote=True), error=block))


def make_login_route(provider: TallyAuthProvider, store: Store):
    """The GET/POST handler for the sign-in page."""

    async def login(request: Request) -> Response:
        if request.method == "GET":
            pending = request.query_params.get("pending", "")
            if not pending or provider.pending(pending) is None:
                return HTMLResponse("<p>That sign-in link is invalid or has expired.</p>", 400)
            return render(pending)

        form = await request.form()
        pending = str(form.get("pending", ""))
        code = str(form.get("code", "")).strip()

        if provider.pending(pending) is None:
            return HTMLResponse("<p>That sign-in link has expired. Start again from Alexa.</p>", 400)
        if not code:
            return render(pending, "Please enter your invite code.")

        caller = request.client.host if request.client else "unknown"
        if store.too_many_join_attempts(f"ip:{caller}", f"link:{pending}"):
            return render(pending, "Too many attempts. Wait a few minutes and try again.")

        # One identity per code. Claiming first means a used code cannot be
        # replayed even if the OAuth step fails afterwards.
        subject = f"tally:{secrets.token_urlsafe(16)}"
        member_id = store.claim_member(code, subject)
        if member_id is None:
            return render(pending, "That code is not valid, or has already been used.")

        try:
            redirect = provider.complete_login(pending, subject)
        except KeyError:
            store.release_claim(member_id, code)
            return HTMLResponse("<p>That sign-in link has expired.</p>", 400)
        return RedirectResponse(redirect, status_code=302)

    return login
