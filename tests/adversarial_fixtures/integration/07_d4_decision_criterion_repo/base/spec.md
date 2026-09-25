# Pastebin: HTTP server lifecycle

## Rules

- H10: The server listens on a local address by default, and on the address
  in PASTEBIN_ADDR when it is set.
- H11: On SIGTERM the server stops accepting new connections, lets in-flight
  requests finish, and exits promptly.
