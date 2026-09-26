"""Hello-world server-sent events latency for one server candidate.

Usage: python sse_bench.py <stdlib|starlette|fastapi|aiohttp>

Starts the candidate on a free loopback port in a thread, streams 50 events
at 5 per second (the TUI's tail polls at 0.2 s, so this is the real cadence),
and measures from the client with httpx: time from request start to the
first event, and for each later event the delay between when the server
scheduled it and when the client read it. Prints one line.
"""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
import json
import socket
import statistics
import sys
import threading
import time

N = 50
PERIOD = 0.2


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def event(i: int) -> bytes:
    return f"data: {json.dumps({'i': i, 't': time.time()})}\n\n".encode()


# ---------------------------------------------------------------- servers


def serve_stdlib(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for i in range(N):
                self.wfile.write(event(i))
                self.wfile.flush()
                time.sleep(PERIOD)

        def log_message(self, *a: object) -> None:
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def serve_starlette(port: int, app_kind: str) -> None:
    import uvicorn

    async def gen():
        for i in range(N):
            yield event(i)
            await asyncio.sleep(PERIOD)

    if app_kind == "fastapi":
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        app = FastAPI()

        @app.get("/")
        async def root():
            return StreamingResponse(gen(), media_type="text/event-stream")

    else:
        from starlette.applications import Starlette
        from starlette.responses import StreamingResponse
        from starlette.routing import Route

        async def root(request):
            return StreamingResponse(gen(), media_type="text/event-stream")

        app = Starlette(routes=[Route("/", root)])

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def serve_aiohttp(port: int) -> None:
    from aiohttp import web

    async def root(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for i in range(N):
            await resp.write(event(i))
            await asyncio.sleep(PERIOD)
        return resp

    app = web.Application()
    app.router.add_get("/", root)
    web.run_app(app, host="127.0.0.1", port=port, print=None, handle_signals=False)


# ---------------------------------------------------------------- client


def measure(port: int) -> tuple[float, list[float]]:
    import httpx

    t0 = time.perf_counter()
    first = None
    delays: list[float] = []
    with httpx.stream("GET", f"http://127.0.0.1:{port}/", timeout=30) as r:
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            now = time.time()
            if first is None:
                first = time.perf_counter() - t0
            payload = json.loads(line[6:])
            delays.append(now - payload["t"])
    return first or float("nan"), delays


def main() -> None:
    kind = sys.argv[1]
    port = free_port()
    target = {
        "stdlib": lambda: serve_stdlib(port),
        "starlette": lambda: serve_starlette(port, "starlette"),
        "fastapi": lambda: serve_starlette(port, "fastapi"),
        "aiohttp": lambda: serve_aiohttp(port),
    }[kind]
    threading.Thread(target=target, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    first, delays = measure(port)
    print(
        f"{kind:<10} first event {first * 1000:6.1f} ms   per-event delay "
        f"p50 {statistics.median(delays) * 1000:5.2f} ms  max {max(delays) * 1000:5.2f} ms  ({len(delays)} events)"
    )


if __name__ == "__main__":
    main()
