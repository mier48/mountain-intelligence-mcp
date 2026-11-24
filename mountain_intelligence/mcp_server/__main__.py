"""Console entrypoint: ``mountain-intelligence-mcp [stdio|http]``.

- stdio: for local MCP clients that launch the server as a subprocess. stdout carries only the
  protocol; every log line goes to stderr.
- http: Streamable HTTP served by uvicorn (put a TLS reverse proxy in front in production).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _configure_stdio_safety() -> None:
    # Logs always go to stderr; stdout belongs to the JSON-RPC stream.
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "WARNING"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mountain-intelligence-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="transport")
    sub.add_parser("stdio", help="Serve MCP over stdin/stdout (default)")
    http = sub.add_parser("http", help="Serve MCP over Streamable HTTP")
    http.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    http.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")))
    http.add_argument(
        "--proxy-headers",
        action="store_true",
        help="Trust X-Forwarded-* from FORWARDED_ALLOW_IPS (behind a reverse proxy)",
    )
    args = parser.parse_args(argv)
    transport = args.transport or "stdio"

    from mountain_intelligence import setup_django

    setup_django()
    from mountain_intelligence.mcp_server.tools import build_server

    server = build_server()
    if transport == "stdio":
        # The SDK claims fd 1 for the protocol and diverts stray writes to stderr while serving.
        _configure_stdio_safety()
        server.run("stdio")
        return

    import uvicorn

    from mountain_intelligence.mcp_server.http import build_http_app

    app = build_http_app(server)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=None,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_keep_alive=15,
        server_header=False,
    )


if __name__ == "__main__":
    main()
