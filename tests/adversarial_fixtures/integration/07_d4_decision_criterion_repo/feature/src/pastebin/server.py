"""The HTTP server's address and its stop (spec.md H10, H11)."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from http.server import ThreadingHTTPServer

DEFAULT_ADDR = ("127.0.0.1", 8080)
SHUTDOWN_GRACE_SECONDS = 2.0


def listen_address(env: Mapping[str, str]) -> tuple[str, int]:
    """PASTEBIN_ADDR as (host, port), or DEFAULT_ADDR when it is unset.

    Raises ValueError when PASTEBIN_ADDR is not host:port with a numeric port.
    """
    raw = env.get("PASTEBIN_ADDR", "")
    if not raw:
        return DEFAULT_ADDR
    host, sep, port = raw.rpartition(":")
    if not sep or not host or not (port.isascii() and port.isdigit()):
        raise ValueError(f"PASTEBIN_ADDR must be host:port, got {raw!r}")
    return host, int(port)


def stop(server: ThreadingHTTPServer, serving: threading.Thread) -> bool:
    """Stop accepting connections, then wait up to SHUTDOWN_GRACE_SECONDS for
    the serving thread to finish. Returns False when it did not finish in time.
    """
    server.shutdown()
    serving.join(SHUTDOWN_GRACE_SECONDS)
    server.server_close()
    return not serving.is_alive()
