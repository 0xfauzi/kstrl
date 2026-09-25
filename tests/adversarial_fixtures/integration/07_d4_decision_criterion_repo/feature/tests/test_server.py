from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pastebin.server import DEFAULT_ADDR, listen_address, stop


def test_the_default_address_is_local() -> None:
    assert listen_address({}) == DEFAULT_ADDR


def test_pastebin_addr_overrides_the_default() -> None:
    assert listen_address({"PASTEBIN_ADDR": "0.0.0.0:9000"}) == ("0.0.0.0", 9000)


@pytest.mark.parametrize("raw", ["9000", ":9000", "host:", "host:port"])
def test_a_malformed_address_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        listen_address({"PASTEBIN_ADDR": raw})


def test_stop_ends_an_idle_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    assert stop(server, serving) is True
