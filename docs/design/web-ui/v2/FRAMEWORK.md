# Which framework serves the web UI: a measured comparison

This document is for the owner, who chooses the server and client stack for `ks web`, and for whoever builds it. It compares the candidates against kstrl's actual needs, measures what can be measured on this laptop, and ends with one recommendation. The measurements are in `framework-bench/out.txt`; `framework-bench/measure.sh` reproduces them.

## What the stack has to do

Seven requirements, from the owner's answers to PLAN.md section 10 and from round 2's brief:

1. A live event stream: the page follows `events.jsonl` of a run at the TUI's polling cadence (0.2 s), and every other file the readers watch. Server-sent events over plain HTTP are enough; nothing needs a socket in both directions.
2. A small JSON API over the existing readers (PLAN.md section 6.2): about twelve endpoints, each serialising a dataclass a reader already returns.
3. Local-only binding with a token: `127.0.0.1` by default, a token for every state-changing request, an `Origin` check (PLAN.md section 6.5).
4. Decisions that call the existing CLI code paths (section 6.3), with a test per action that the web handler calls the same function the CLI command calls.
5. Install weight on the core harness: the harness runs on rich and click and a pinned Textual; the web UI is an optional extra, so its weight lands only on people who ask for it, but a heavy extra is still a slow `uv sync` and a larger surface to keep pinned.
6. Test fit with pytest: handlers testable in-process without a running server, and a test client that can read an event stream.
7. New in round 2: a client that renders views from a JSON spec against a component catalogue, and can gain a component without rebuilding everything.

Requirement 7 is the one that decides the client side, so it is taken first.

## What "views from a spec" demands of the client

A view is a JSON document: data source, filters, grouping, sort, form (list, board, timeline, graph, meter, sparkline, table, number, diff), placement. The client must turn that document into a rendered view at once, and a saved view file must render the same way tomorrow. A new component is code that did not exist when the page shipped: it must be loadable by name, from a file the server hands out, without the page being rebuilt.

That rules out two things. It rules out rendering views purely on the server as HTML fragments, because a made view is live (it follows the event stream) and a fragment per event at 5 events a second is a page that flickers. And it rules out a client whose components exist only at build time: a bundler that tree-shakes the catalogue cannot admit a component it has never seen, so "new component without a rebuild" needs a runtime registry keyed by name, loaded as a separate script.

Any of the small SPA runtimes can do this if the catalogue is a runtime registry rather than an import graph. Web components do it natively: `customElements.define("k-heatmap", ...)` from a script the server serves is exactly the mechanism.

## The candidates

Server side:

- The standard library's `ThreadingHTTPServer`, server-sent events written by hand.
- Starlette on uvicorn: the ASGI toolkit FastAPI is built on, without FastAPI's validation layer.
- FastAPI on uvicorn.
- aiohttp.
- Litestar on uvicorn.
- Flask (measured for weight only; its event streaming needs a threaded server and offers nothing over the standard library here).

Client side:

- Server-rendered HTML with htmx for partial updates.
- A small SPA with a build step, shipped prebuilt inside the wheel: Preact, Solid, Svelte, React.
- No-build web components: Lit, or plain `customElements` with no library.

## Measurements

All on one laptop (macOS, Python 3.12, uv 0.9) on 2026-09-26. Server install weight is the size of `site-packages` in a fresh venv after installing the candidate, and the wall time of that install with a warm uv cache (a cold cache adds the download, which depends on the network and is not reported). SSE latency is a hello-world stream of 50 events at 5 per second, measured by an httpx client on the same machine: time from request to first event, and the delay between the server scheduling an event and the client reading it. Client size is the gzipped bytes of the runtime with its dependencies, bundled by esm.sh, which is what a wheel would ship.

| Server candidate | site-packages added | distributions | install (warm) | first event | per-event delay p50 / max |
|---|---|---|---|---|---|
| standard library | 0 | 0 | 0 | 53 ms | 0.34 / 1.21 ms |
| starlette + uvicorn | 2.6 MB | 7 | 0.25 s | 67 ms | 0.25 / 0.62 ms |
| fastapi + uvicorn | 10.3 MB | 13 | 0.18 s | 55 ms | 0.25 / 0.55 ms |
| aiohttp | 4.1 MB | 10 | 0.32 s | 53 ms | 0.23 / 0.46 ms |
| litestar + uvicorn | 26.5 MB | 23 | 0.53 s | not measured | not measured |
| flask | 2.6 MB | 7 | 0.18 s | not measured | not measured |

The latency column says the server choice does not matter for the event stream: every candidate delivers an event within about a millisecond of scheduling it, and the first-event time is dominated by connection setup. The weight column is where they differ: FastAPI is four times Starlette, and Litestar is ten times.

| Client candidate | runtime, gzipped | needs a build step | components at runtime |
|---|---|---|---|
| htmx 2 | 18.5 KB | no | no (the server renders) |
| Preact 10 + hooks | 5.6 KB | no with htm (0.7 KB), yes for JSX | yes, if the catalogue is a registry |
| Lit 3 | 5.8 KB | no | yes, custom elements by name |
| Solid 1 | 12.4 KB | yes (its compiler is the point) | possible, awkward without the compiler |
| Svelte 5 runtime | 38.8 KB | yes (compiler) | no: components are compiled |
| React 19 + react-dom | 70.7 KB | yes in practice | yes, if the catalogue is a registry |
| plain custom elements | 0 | no | yes |

## How each candidate meets the seven requirements

Server side.

The standard library meets 1 to 4 and 5 outright: no dependency, a threaded handler per stream, the token check written once. It fails 6 in practice: there is no test client, so every handler test either spins up a real server on a port or fakes `BaseHTTPRequestHandler`, and the codebase already records what port-based tests cost (the process-scoping guard in `tests/helpers/procs.py` exists because of them). Hand-written SSE is thirty lines, not a risk.

Starlette meets all six server-side requirements. Its `TestClient` runs the app in-process and can iterate a streaming response, so a handler test reads events without a port. `StreamingResponse` is the event stream. Middleware is where the token and `Origin` checks live, once. It adds seven distributions and 2.6 MB, all pure Python except uvicorn's optional speedups, and it is already in the lock file as a transitive dependency of the `sdk` extra (starlette 1.3.1, uvicorn 0.51.0, sse-starlette 3.4.5 are installed in the repo's venv today), so it adds no new pins to review.

FastAPI adds Pydantic and a validation layer on top of Starlette. The API here serialises dataclasses the readers already validate; FastAPI's schema generation would be a second definition of every shape, which is the drift the plan warns against. It costs 7.7 MB more than Starlette for that.

aiohttp meets the requirements and is light, but it is a second HTTP framework in the dependency tree beside the one the SDK extra already brings, and its test client and streaming shape differ from Starlette's.

Litestar is the heaviest by far and offers nothing the others do not for twelve endpoints.

Client side.

htmx fails requirement 7: a made view that follows a stream would be a server-rendered fragment per event, and a new component would be a new server template, which is a rebuild of the server, not a runtime addition. It is the right tool for a page that is mostly forms; this page is mostly live state.

Svelte and Solid fail the "without a rebuild" half of 7 because their components are compiler output; a component built by a factory run would have to be compiled by the same toolchain before it could load. That is a Node toolchain inside the wheel's build and inside every component request.

React meets 7 with a runtime registry but at 70 KB and with a build step that every contributor and every generated component would need.

Preact with htm meets 7 without a build: htm is tagged templates, the catalogue is a `Map` from form name to a function, and a new component is a script that registers a function. It is 6.3 KB. Its weakness is that a generated component would be written against Preact's API, which is small but is a framework a factory run must know.

Lit, or plain custom elements, meets 7 most directly: a component is a class registered under a tag name; the page renders a made view by creating the element for the form and setting its `spec` and `rows` properties; a new component is a `<script type="module">` the server serves from `.kstrl/components/`. The spec-to-DOM contract is then the platform's, not a library's, and the factory run that builds a component needs to know only the element contract in a one-page document. Lit adds 5.8 KB for templating and reactive properties; plain custom elements add nothing and cost more hand-written DOM code.

## Recommendation

Server: Starlette on uvicorn, as the `web` extra. It is the lightest candidate that passes the test-fit requirement, its event stream and test client are measured to work, and every pin it needs is already in the lock file through the `sdk` extra. FastAPI is rejected for adding a second definition of every shape; the standard library is rejected for the port-based tests it would force.

Client: no-build custom elements, with Lit for templating, shipped as static files in the wheel; no Node toolchain anywhere in the repository. The catalogue is a directory of element definitions, one per form; a made view is the element for its form with the spec as a property; a new component built by a factory run is one more module in that directory, served and registered by name, admitted only after the gates. The prototype in `prototype/` is plain DOM with no library, which is the same contract without the templating help; the rendering functions in `prototype/app.js` map one to one onto elements.

What this recommendation does not settle: whether Lit's 5.8 KB is worth having over plain custom elements can only be judged by writing two of the nine forms both ways, which is a first-slice task. And the measurements are single runs on one machine; the weight and size numbers are stable across runs, the latency numbers would need repetition before anyone builds a budget on them.
