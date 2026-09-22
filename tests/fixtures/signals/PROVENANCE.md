# Provenance of the captured Bugsink responses

Captured 2026-09-22 by lane 155 (verify-and-plan) from Bugsink 2.6.0 running
locally from the minimal design lane's virtualenv, against a copy of that lane's
SQLite database. No network beyond localhost. Port 9177. The server process was
started by this lane and killed by this lane (pid recorded in server.pid).

| file | request | status |
|---|---|---|
| bugsink-issues-canonical.json | GET /api/canonical/0/issues/?project=1, Bearer token | 200, 3481 bytes |
| bugsink-events-list.json | GET /api/canonical/0/events/?issue=<uuid>, Bearer token | 200, 2561 bytes |
| bugsink-event-detail.json | GET /api/canonical/0/events/<uuid>/, Bearer token | 200, 5699 bytes |
| sentry-project-issues.txt | GET /api/0/projects/demo/demo-product/issues/ | 404, "Unimplemented API endpoint" |
| sentry-org-issues.txt | GET /api/0/organizations/demo/issues/ | 404, "Unimplemented API endpoint" |
| bad-token.txt | same as the first, 40 zeros as the token | 401, {"detail":"Invalid Bearer token."} |
| no-token.txt | same as the first, no Authorization header | 401, {"detail":"Authentication credentials were not provided."} |

The data is synthetic: eight issues raised by the minimal lane's send.py, send5.py
and send5b.py from a demo project. It contains no real user data.
