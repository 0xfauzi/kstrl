# Pastebin: API authentication

## Rules

- H1: Every API request carries `Authorization: Bearer <token>`.
- H2: A request with a missing, malformed or unknown token gets 401.
- H3: Header values may carry optional whitespace (spaces and tabs, RFC 9110
  OWS) before and after the value, which the server ignores.
- H4: `GET /snippets/<id>` returns the snippet body with 200, or 404 when there
  is no such snippet.
