"""Container healthchecks: ``python -m mountain_intelligence.ops.healthcheck {mcp|beat}``.

The worker uses ``celery inspect ping`` directly (see compose.yaml).
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path


def check_mcp() -> bool:
    port = os.environ.get("MCP_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as response:
            return response.status == 200
    except OSError:
        return False


def check_beat() -> bool:
    pidfile = Path(os.environ.get("CELERY_BEAT_PIDFILE", "/tmp/celerybeat.pid"))  # noqa: S108
    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False
    return Path(f"/proc/{pid}").exists()


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    ok = {"mcp": check_mcp, "beat": check_beat}[target]()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
