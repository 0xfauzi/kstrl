# Pastebin: command-line client

## Rules

- H1: Every API request carries `Authorization: Bearer <token>`.
- C1: The client reads its API token from the PASTEBIN_TOKEN environment
  variable and refuses to start when the token is missing or is not a valid
  bearer token.
- C2: `pastebin get <id>` prints the snippet body.
