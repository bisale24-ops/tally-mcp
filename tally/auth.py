"""OAuth 2.1 authorization server: storage plus account linking.

The SDK ships the endpoints and PKCE checking. What it cannot ship is deciding
which human is on the other end - on a shared device that is the whole problem.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthToken,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

CODE_TTL = 300  # Long enough to sign in, short enough to be useless if leaked.
ACCESS_TTL = 3600
REFRESH_TTL = 60 * 60 * 24 * 30
REFRESH_GRACE = 60
"""How long a rotated refresh token keeps answering with what it produced.

Rotation that deletes the old token first turns a resent request - or two
racing ones - into a dead session on a device that had a perfectly good token."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    payload   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    id         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token        TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    replaced_with TEXT
);
"""


class TallyAuthProvider:
    """An OAuth 2.1 authorization server sharing the ledger's SQLite file."""

    def __init__(
        self,
        db_path: str,
        *,
        login_path: str = "/login",
        static_client: OAuthClientInformationFull | None = None,
    ) -> None:
        self.db_path = db_path
        self.login_path = login_path
        # Alexa+ does not register itself. Without a client configured up front
        # there is no way for it to start the flow at all.
        self.static_client = static_client
        with self._db() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- clients ---------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if self.static_client is not None and hmac.compare_digest(client_id, self.static_client.client_id):
            return self.static_client
        with self._db() as conn:
            row = conn.execute("SELECT payload FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["payload"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, payload) VALUES (?, ?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    # -- authorization ---------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the user to the sign-in page.

        No code is minted here - we know which client is asking, not which person.
        """
        pending_id = secrets.token_urlsafe(24)
        payload = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": params.scopes or [],
            "code_challenge": params.code_challenge,
            "resource": params.resource,
        }
        with self._db() as conn:
            self._sweep(conn)
            conn.execute(
                "INSERT INTO oauth_pending (id, payload, expires_at) VALUES (?, ?, ?)",
                (pending_id, json.dumps(payload), time.time() + CODE_TTL),
            )
        return f"{self.login_path}?{urlencode({'pending': pending_id})}"

    def _sweep(self, conn: sqlite3.Connection) -> None:
        """Drop rows whose deadline has passed. Nothing here outlives its TTL,
        so without this the tables only ever grow."""
        now = time.time()
        conn.execute("DELETE FROM oauth_pending WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ?", (now,))
        conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,))

    def pending(self, pending_id: str) -> dict[str, Any] | None:
        """The parked authorization request, if it has not expired."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT payload FROM oauth_pending WHERE id = ? AND expires_at > ?",
                (pending_id, time.time()),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def complete_login(self, pending_id: str, subject: str) -> str:
        """Bind a signed-in person to the parked request, returning the redirect.

        Raises:
            KeyError: If the request is unknown or expired.
        """
        request = self.pending(pending_id)
        if request is None:
            raise KeyError("that sign-in link has expired")

        code = secrets.token_urlsafe(32)
        record = {**request, "code": code, "subject": subject}
        with self._db() as conn:
            conn.execute("DELETE FROM oauth_pending WHERE id = ?", (pending_id,))
            conn.execute(
                "INSERT INTO oauth_codes (code, payload, expires_at) VALUES (?, ?, ?)",
                (code, json.dumps(record), time.time() + CODE_TTL),
            )

        query = {"code": code}
        if request.get("state"):
            query["state"] = request["state"]
        separator = "&" if "?" in request["redirect_uri"] else "?"
        return f"{request['redirect_uri']}{separator}{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT payload FROM oauth_codes WHERE code = ? AND expires_at > ?",
                (authorization_code, time.time()),
            ).fetchone()
        if row is None:
            return None
        record = json.loads(row["payload"])
        if record["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=record["code"],
            scopes=record["scopes"],
            expires_at=time.time() + CODE_TTL,
            client_id=record["client_id"],
            code_challenge=record["code_challenge"],
            redirect_uri=AnyHttpUrl(record["redirect_uri"]),
            redirect_uri_provided_explicitly=record["redirect_uri_provided_explicitly"],
            resource=record.get("resource"),
            subject=record["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> Any:
        """Trade a one-time code for tokens, burning the code."""
        with self._db() as conn:
            conn.execute("DELETE FROM oauth_codes WHERE code = ?", (authorization_code.code,))

        subject = authorization_code.subject or "unknown"
        access = self._mint(
            "access",
            subject,
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            ACCESS_TTL,
        )
        refresh = self._mint(
            "refresh",
            subject,
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            REFRESH_TTL,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(authorization_code.scopes),
            refresh_token=refresh,
        )

    # -- tokens ----------------------------------------------------------

    def _mint(self, kind: str, subject: str, client_id: str, scopes: list[str], resource: str | None, ttl: int) -> str:
        with self._db() as conn:
            return self._issue(conn, kind, subject, client_id, scopes, resource, ttl)

    def _issue(
        self,
        conn: sqlite3.Connection,
        kind: str,
        subject: str,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        ttl: int,
    ) -> str:
        token = secrets.token_urlsafe(32)
        payload = {"subject": subject, "client_id": client_id, "scopes": scopes, "resource": resource}
        conn.execute(
            "INSERT INTO oauth_tokens (token, kind, payload, expires_at) VALUES (?, ?, ?, ?)",
            (token, kind, json.dumps(payload), time.time() + ttl),
        )
        return token

    def _load(self, token: str, kind: str) -> tuple[dict[str, Any], float] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT payload, expires_at FROM oauth_tokens WHERE token = ? AND kind = ? AND expires_at > ?",
                (token, kind, time.time()),
            ).fetchone()
        return (json.loads(row["payload"]), row["expires_at"]) if row else None

    async def load_access_token(self, token: str) -> AccessToken | None:
        loaded = self._load(token, "access")
        if loaded is None:
            return None
        payload, expires_at = loaded
        return AccessToken(
            token=token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(expires_at),
            resource=payload.get("resource"),
            subject=payload["subject"],
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        loaded = self._load(refresh_token, "refresh")
        if loaded is None:
            return None
        payload, expires_at = loaded
        if payload["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=payload["client_id"],
            scopes=payload["scopes"],
            expires_at=int(expires_at),
            resource=payload.get("resource"),
            subject=payload["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> Any:
        """Rotate the refresh token, answering a repeat with what it already made.

        OAuth 2.1 wants one use per token. It does not want a retransmission to
        lock the device out, so the spent token keeps returning its replacement
        for `REFRESH_GRACE` seconds instead of vanishing.
        """
        granted = scopes or refresh_token.scopes
        subject = refresh_token.subject or "unknown"

        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT replaced_with FROM oauth_tokens WHERE token = ?", (refresh_token.token,)
                ).fetchone()
                if row is not None and row["replaced_with"]:
                    conn.execute("COMMIT")
                    return OAuthToken.model_validate_json(row["replaced_with"])

                where = refresh_token.resource
                access = self._issue(conn, "access", subject, client.client_id, granted, where, ACCESS_TTL)
                rotated = self._issue(conn, "refresh", subject, client.client_id, granted, where, REFRESH_TTL)
                issued = OAuthToken(
                    access_token=access,
                    token_type="Bearer",
                    expires_in=ACCESS_TTL,
                    scope=" ".join(granted),
                    refresh_token=rotated,
                )
                conn.execute(
                    "UPDATE oauth_tokens SET replaced_with = ?, expires_at = ? WHERE token = ?",
                    (issued.model_dump_json(), time.time() + REFRESH_GRACE, refresh_token.token),
                )
            except Exception:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return issued

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM oauth_tokens WHERE token = ?", (token.token,))
