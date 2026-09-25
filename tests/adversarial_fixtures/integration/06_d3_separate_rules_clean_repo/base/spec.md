# Pastebin: snippet bodies and the HTTP server

## Rules

- S4: A snippet body is at most 65536 bytes of UTF-8.
- H3: Header values may carry optional whitespace (spaces and tabs, RFC 9110
  OWS) before and after the value, which the server ignores.
- H9: The server refuses a request whose body is larger than 65536 bytes with
  413, before reading it. H9 is a separate rule from S4: a request body carries
  JSON framing around the snippet, and either limit may change without the
  other.
