"""Run Tally over Streamable HTTP, the transport Alexa+ connects to."""

from __future__ import annotations

import argparse
import os

from .server import create_server
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="tally-mcp", description=__doc__)
    parser.add_argument("--host", default=os.environ.get("TALLY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TALLY_PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("TALLY_DB", "tally.db"))
    parser.add_argument(
        "--public-url",
        default=os.environ.get("TALLY_PUBLIC_URL"),
        help="Public https origin (e.g. a cloudflared tunnel). Enables OAuth 2.1.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Serve over stdio instead, for local inspection.",
    )
    args = parser.parse_args()

    server = create_server(Store(args.db), public_url=args.public_url)
    if args.stdio:
        server.run("stdio")
        return

    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path="/mcp",
    )


if __name__ == "__main__":
    main()
