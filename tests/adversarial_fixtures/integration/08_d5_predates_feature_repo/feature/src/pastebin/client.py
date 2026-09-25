"""The command-line client (spec.md C1, C2)."""

from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence

from pastebin.tokens import parse_bearer

DEFAULT_SERVER = "http://127.0.0.1:8080"


class ClientError(Exception):
    """The client cannot start."""


def load_token(env: Mapping[str, str]) -> str:
    """PASTEBIN_TOKEN, checked as a bearer token (C1). Raises ClientError."""
    token = env.get("PASTEBIN_TOKEN", "")
    if not token or parse_bearer(token) is None:
        raise ClientError("PASTEBIN_TOKEN is missing or is not a valid bearer token")
    return token


def build_request(server: str, token: str, snippet_id: str) -> urllib.request.Request:
    """The GET request for snippet ``snippet_id`` (C2), carrying the token (H1)."""
    return urllib.request.Request(
        f"{server}/snippets/{urllib.parse.quote(snippet_id, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    """``pastebin get <id>``: print the snippet body. Returns the exit code."""
    if len(argv) != 2 or argv[0] != "get":
        print("usage: pastebin get <id>", file=sys.stderr)
        return 2
    try:
        token = load_token(env)
        request = build_request(env.get("PASTEBIN_SERVER", DEFAULT_SERVER), token, argv[1])
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.read().decode("utf-8"))
    except (ClientError, OSError, UnicodeDecodeError) as exc:
        print(f"pastebin: {exc}", file=sys.stderr)
        return 1
    return 0
