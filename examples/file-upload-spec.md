# File Upload Service

A small HTTP API for uploading user-supplied files to a server-managed storage area, with retrieval and listing endpoints. Used to validate the adversarial factory on a spec that has real security/test-quality concerns the roles should catch (Phase F of the adversarial hardening roadmap).

## Background

We need a simple file-upload micro-service. Users upload files via a multipart POST, the server stores them on disk under a per-user namespace, and the user can later list and download their files. The service must protect against the usual file-upload risks (path traversal, oversize files, unsafe content types) and must isolate one user's storage from another's.

## Users

- Authenticated end users with a unique `user_id` (UUID string).
- The frontend posts an `Authorization: Bearer <jwt>` header; the service verifies the JWT signature and extracts `user_id` from the `sub` claim.

## Functional requirements

### FR-1 Upload endpoint
- `POST /api/files`
- Multipart body with a single `file` field.
- Server-side validation:
  - The `Content-Type` of the file MUST be one of `image/png`, `image/jpeg`, `application/pdf`, `text/plain`. Any other type is rejected with 415.
  - Size MUST be <= 5 MiB. Larger uploads are rejected with 413 before being fully read into memory.
  - The original filename is preserved as metadata but the on-disk path uses a server-generated UUID; the user's filename never reaches the filesystem.
- On success, returns 201 with JSON `{ "file_id": "<uuid>", "size_bytes": N, "content_type": "..." }`.

### FR-2 List endpoint
- `GET /api/files`
- Returns the files owned by the authenticated user as a JSON array of objects: `{ "file_id", "filename", "size_bytes", "content_type", "uploaded_at" }`.
- Pagination: `?cursor=<opaque>` + `?limit=N` (1 <= N <= 100, default 50). The cursor is server-generated.

### FR-3 Download endpoint
- `GET /api/files/{file_id}`
- Returns the file bytes with the original `Content-Type` and a `Content-Disposition: attachment; filename="<original>"` header.
- 404 if the file doesn't exist OR is owned by another user. The "not found vs forbidden" distinction must not leak across users.

### FR-4 Delete endpoint
- `DELETE /api/files/{file_id}`
- 204 on success. 404 if the file doesn't exist or belongs to another user.
- Soft delete: the row is marked deleted but the bytes are retained for 7 days for recovery. Subsequent `GET /api/files/{file_id}` returns 404.

## Non-functional requirements

- Storage layout under `<storage_root>/<user_id>/<file_id>` where `<storage_root>` is read from `FILE_UPLOAD_STORAGE_ROOT`.
- All disk writes use atomic-rename (write to a temp file in the same directory, then `os.replace`) so a crash mid-upload never leaves a partial file at the canonical path.
- Concurrent uploads from the same user must not corrupt each other (race-free).
- The JWT verification helper is a separate module; tests for it cover signature failure, expiration, missing `sub` claim, and replay (`exp` already past).
- All log lines include the `user_id` and `file_id` for traceability. No raw file bytes in logs.

## Out of scope

- File preview / thumbnails.
- Sharing files between users.
- Versioning (overwriting a file_id creates a new file_id; nothing in the API ever overwrites).
- Bulk operations.

## Tech stack

- Python 3.11+, FastAPI, uvicorn for the server.
- SQLite (file-backed) for the metadata table.
- `PyJWT` for JWT verification.
- `pytest` + `httpx.AsyncClient` for tests; coverage of negative paths is required.
- Typecheck: `uv run mypy src/ --strict`. Lint: `uv run ruff check src/ tests/`.

## Verification commands

- Tests: `uv run pytest tests/ -v`
- Typecheck: `uv run mypy src/ --strict`
- Lint: `uv run ruff check src/ tests/`

## Things the architect / reviewer / security role should naturally surface

(Not part of the spec the implementer sees; this section is a key for grading the Phase F factory run.)

- Path traversal in storage layout if `file_id` is ever derived from user input (CWE-22).
- Content-Type sniff vs declared header: an attacker can upload `application/octet-stream` disguised as `image/png`. The spec asks only that the declared header is checked.
- Size-limit timing: the 413 must fire before the full body is buffered, otherwise unbounded memory consumption on hostile clients.
- JWT verification must check `alg` against an allowlist; `alg: none` bypass is a classic.
- Soft-delete window: 7 days of retained bytes is sensitive data; tests should cover that a deleted file can't be downloaded via the API even though bytes exist on disk.
- Race-free concurrent uploads: TOCTOU when checking-then-writing storage.
- Pagination cursor: if naive (e.g. row offset), the user can probe other users' file_id values by guessing.
- Test quality: negative tests for every 4xx response, plus a test that proves user A cannot see user B's files.
