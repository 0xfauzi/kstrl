# Codebase Map (Brownfield Notes)

PulseKit is a telemetry ingest service. One package, `src/pulsekit/`, six
source modules and one test module. Everything below was read off the
source; where a claim is about behaviour, the test that pins it is named.

## Quick Facts

- **Language / framework**: Python, no web framework. `src/pulsekit/api.py`
  returns a `Response` dataclass and a server adapter turns it into HTTP.
- **How to test**: `pytest tests/`
- **Primary entrypoints**: `IngestApi.post_events` and `IngestApi.get_replay`
  in `src/pulsekit/api.py`.
- **Data store**: in-process only. `EventStore` in `src/pulsekit/storage.py`
  is a list; nothing is persisted between runs.

## Repo topology

- `src/pulsekit/__init__.py` holds `VERSION` and nothing else.
- `src/pulsekit/envelope.py` is the wire format.
- `src/pulsekit/storage.py` is the log.
- `src/pulsekit/throttle/__init__.py` is empty (a docstring only).
- `src/pulsekit/throttle/token_bucket.py` is the rate-limiting primitive.
- `src/pulsekit/api.py` wires the three together into two routes.
- `tests/test_token_bucket.py` is the only test module.

## Module notes

### src/pulsekit/envelope.py

`parse_envelope(raw: bytes) -> EventEnvelope` decodes one request body and
returns a frozen `EventEnvelope` (device_id, sent_at, kind, body). Every
rejection is an `EnvelopeError`, which is a `ValueError`: bad UTF-8, bad
JSON, a non-object payload, a missing `deviceId` / `sentAt` / `kind`, or one
of those three with the wrong type. A `body` that is not an object is
replaced by `{}` rather than refused.

### src/pulsekit/storage.py

`EventStore` is an append-only list of envelopes. `append` returns the new
event's offset, `since(offset)` yields everything from that offset on, and
`count_for_device` counts one device's events. No eviction, no index.

### src/pulsekit/throttle/token_bucket.py

This module is the throttling primitive the service already has.

`TokenBucket(capacity, refill_per_second)` starts full. `take(now, cost)`
refills to `now` and spends `cost` tokens, returning False when short.
`refill(now)` is idempotent and ignores a clock that goes backwards.
`retry_after(now, cost)` returns the seconds until that many tokens exist,
0.0 when they already do. Capacity is the burst; `refill_per_second` is the
sustained rate. Nothing in the module reads a clock: the caller passes
`now`, which is why the tests are deterministic.

`BucketRegistry(capacity, refill_per_second)` holds one `TokenBucket` per
key and creates it on first use. `bucket_for(key)` is the accessor,
`forget(key)` drops a key so its next request starts at full burst.

Pinned by `tests/test_token_bucket.py`: full burst on a fresh bucket,
refill capped at capacity, `retry_after` arithmetic, and per-key isolation
in the registry.

### src/pulsekit/api.py

`IngestApi` holds an `EventStore` and a `BucketRegistry` called
`admin_limits`, and returns a `Response` (status, body, headers) from each
route.

- `post_events(raw)` parses the envelope, returns 400 with the
  `EnvelopeError` message on a bad one, otherwise appends and returns 202
  with the offset. **It applies no per-caller limit of any kind.**
- `get_replay(operator, offset, now)` takes a token from
  `admin_limits.bucket_for(operator)` first, returns 429 with a
  `Retry-After` header built from `retry_after` when the bucket is empty,
  and otherwise replays from the offset.

So the 429 shape, the `Retry-After` header and the per-key registry already
exist in this repo and are already exercised by a route. The ingest route
is the one surface that does not use them.

## Known gaps

- Limits are hard-coded where `BucketRegistry` is constructed; there is no
  per-key configuration and no store of limits.
- `EventStore` grows without bound.
