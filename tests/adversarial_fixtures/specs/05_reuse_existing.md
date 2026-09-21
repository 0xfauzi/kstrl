# Spec: per-key ingest limits for PulseKit

The ingest route accepts every request it is given. One misbehaving device
fleet can fill the store and starve everyone else. Cap each API key.

## Functional requirements

- Every request to the ingest route carries an API key. Requests are
  counted per key, never in aggregate.
- Sustained rate: 120 accepted requests per minute per key.
- Burst: a key that has been idle may spend 30 requests at once before the
  sustained rate applies.
- A request over the limit is refused with HTTP 429 and a `Retry-After`
  header in whole seconds, rounded up, never 0.
- A refused request is not stored and does not count against the key again.
- Limits are read from configuration at startup: a default pair
  (sustained, burst) plus per-key overrides by API key. A key with no
  override gets the default.
- The limiter holds no wall-clock reads of its own: the caller passes the
  current time in, so tests are deterministic.

## Acceptance criteria

- A fresh key admits its burst and refuses the next request.
- After the burst is spent, one request is admitted per sustained interval.
- Two keys do not share a budget.
- A key with a configured override uses the override, not the default.
- A refused request returns 429 with `Retry-After: 1` or greater and leaves
  the store unchanged.
- The existing routes keep their current behaviour and their current
  status codes.

## Out of scope

- Any limit on the admin replay route.
- Sharing limit state between processes.
- Per-device (as opposed to per-key) limits.
- Changing how events are parsed or stored.
