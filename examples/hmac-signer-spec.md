# Spec: hmac-signer

A tiny security-relevant utility that signs and verifies messages with HMAC-SHA256.
Single self-contained Python module, single component. Used as a cheap end-to-end
validation target for the kstrl factory pipeline.

## Functional requirements

1. **`sign(message: str, secret: str) -> str`** — returns a hex-encoded HMAC-SHA256
   signature of `message` keyed by `secret`. Both inputs are UTF-8 strings.

2. **`verify(message: str, secret: str, signature: str) -> bool`** — returns `True`
   when the signature matches what `sign(message, secret)` would produce, otherwise
   `False`. Comparison MUST use `hmac.compare_digest` (constant-time) to avoid
   leaking the signature through a timing side channel.

3. **Empty inputs.** Empty message and empty secret are both legal; the function
   must not raise. The signature itself is still well-defined under HMAC.

## Non-functional requirements

- **No timing oracle.** A naive `==` comparison would allow an attacker to extract
  the expected signature byte-by-byte. The spec mandates `hmac.compare_digest`.
- **No string-key smuggling.** The function must reject `bytes` or `bytearray`
  inputs explicitly with `TypeError` (no implicit decode). This prevents the
  trivial "user passed `b'secret'` and Python silently coerced" footgun.
- **No global state.** Sign and verify must be pure functions. No module-level
  caches, no logging side effects on the happy path.

## Acceptance criteria

1. `sign("hello", "k")` returns a 64-character lowercase hex string.
2. `verify("hello", "k", sign("hello", "k"))` is `True`.
3. `verify("hello", "k", "0" * 64)` is `False` (wrong signature).
4. `verify("hello", "wrong", sign("hello", "k"))` is `False` (wrong key).
5. `verify("hello-tampered", "k", sign("hello", "k"))` is `False` (tampered message).
6. `sign("", "")` returns a valid 64-character hex string and `verify("", "", that)` is `True`.
7. `sign(b"hello", "k")` raises `TypeError`.
8. `verify("hello", b"k", sign("hello", "k"))` raises `TypeError`.

## Out of scope

- Async variants.
- Key rotation / multiple keys.
- Anything beyond a single sign + verify pair.
- Wire format / serialization.
