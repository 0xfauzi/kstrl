# Linear Integration (R7.4)

Ralph mirrors a factory run into Linear: one project per manifest, one
issue per component, spec findings into Triage, and status transitions
driven entirely by Linear's GitHub integration. The integration is
observability only - every Linear failure warns and degrades; nothing
in the pipeline ever fails because Linear did.

API facts in this doc were verified against Linear's current docs on
2026-07-19 (linear.app/developers). Re-verify before relying on them
in new code.

## Enabling

```toml
[linear]
enabled = true
team_id = "540e2302-e91c-42a7-92d7-e2f274bbf298"   # your team UUID
```

```sh
export KSTRL_LINEAR_TOKEN="lin_api_..."   # or an OAuth app-actor token
```

The token env var NAME is configurable (`token_env`); the token value
never appears in code, config files, logs, or error messages. All
knobs: see `docs/env-vars.md` and `kstrl.toml.example`.

## What happens where

| Moment | Linear effect | Mechanism |
|---|---|---|
| `ks decompose` | Project created; one issue per component (stories as a checklist); non-blocker spec_issues filed as Triage issues | `linear.sync_decompose`, called from `decompose_spec` |
| Branch creation | Issue moves to In Progress when the PR opens | Issue identifier rides the branch name (`kstrl/factory/exc-42-<comp>`); zero API calls |
| PR merge | Issue moves to Done | `Fixes EXC-42` trailer in the PR body; zero API calls |
| Component failure / budget halt | Comment on the issue | `LinearSink` on the progress log |
| `ks retry` / resume | Same issues updated, never duplicated | ids persisted in the manifest |

Status transitions therefore cost ralph **zero** API calls; the only
mutations are decompose-time creates and failure comments. Per-team
automation defaults ("In Progress" on PR open, "Done" on merge) are
configurable in Linear under Team Settings > Workflows and
automations > Pull request and commit automations.

## Stories: checklist, not sub-issues

The roadmap sketch said "stories as sub-issues"; the implementation
uses a markdown checklist inside the component issue instead.
Reasons:

1. Stories are not independently schedulable. The engineer implements
   stories inside ONE component branch and ONE component PR. A
   sub-issue would have no branch or PR of its own, so the GitHub
   integration could never transition it - every sub-issue would rot
   as forever-open.
2. Fewer mutations in the shared rate pool: one issueCreate per
   component instead of 1 + stories, and idempotency has one object
   per component to keep straight instead of N.
3. Atomicity: the checklist rides the issue description, so issue +
   stories cannot half-create.

If stories ever become independently schedulable (per-story branches),
revisit: `IssueCreateInput.parentId` accepts a UUID or `LIN-123`
identifier and is the natural upgrade path.

## Idempotency

Every created object's UUID is generated client-side (Linear's create
mutations accept an `id` "in UUID v4 format") and derived
deterministically - SHA-256 with v4 format bits - from an external
key:

- project: `<sync_key>:project`
- component issue: `<sync_key>:<component_id>`
- triage issue: `<sync_key>:spec:<index>`
- failure comment: `<run_id>:<event>:<component>:<data fingerprint>`

`sync_key` is the run id of the run that first synced (persisted as
`linearSyncKey` in the manifest, alongside `linearProjectId` and
per-component `linearIssueId`/`linearIssueIdentifier`). Because the
key is deterministic, a double-fired create carries the SAME UUID and
cannot mint a second object; on a duplicate-id error the client
recovers by fetching the object it already created. Retries and
resumed runs read the persisted ids and UPDATE (`issueUpdate`,
comments on the same issue) rather than create.

Known limitation: if decompose crashes after Linear creates succeed
but before the manifest is saved, the manifest (and sync_key) are
gone and a re-decompose creates a fresh project; the orphaned one is
visible in Linear and safe to delete. Linear's only documented
first-class idempotency key (attachment URL + `attachmentsForURL`)
is the escape hatch if duplicate-id conflict behavior ever proves
different from expected - it is not currently used.

## Client behavior

- One HTTP entry point (`LinearClient.execute`) over stdlib urllib -
  no new dependencies.
- Client-side throttle: `min_request_interval` (default 0.5s) between
  requests. Shared pools are 2,500 req/hr (personal API key) and
  5,000 req/hr (OAuth app); a decompose issues roughly
  2 + components + spec_issues requests.
- Linear reports rate limiting as HTTP 400 with a `RATELIMITED` error
  code (not 429). The client retries once after a short sleep, then
  fails loudly (which the callers degrade to warnings).
- Defensive parsing: every response field is type-checked before use;
  malformed responses raise `LinearError` with a truncated summary.
- Dry run (`dry_run = true`, the default in tests): `execute` records
  the mutation in `client.recorded` and returns a synthesized
  response; nothing touches the network.
- Auth: `Authorization: <key>` (bare) for personal API keys,
  `Authorization: Bearer <token>` for OAuth; `auth_mode = "auto"`
  picks by the documented `lin_api_` prefix.

## Progress sink

`LinearSink` implements the `ProgressSink` protocol
(`observability.py`); `ProgressLog.emit` fans events out to attached
sinks AFTER the JSONL journal write, isolating every sink exception
behind a warning. The sink maps:

- `component_failed` -> comment with the error on the component issue
- `budget_exceeded` -> comment noting the token-budget halt

Everything else (start/complete/verification/review) is deliberately
NOT mirrored: those transitions ride the GitHub integration or add no
information a human would act on from Linear. The sink requires
`progress_log_enabled` (the default) - a disabled progress log emits
nothing to fan out.

## Modes and edge cases

- `single_pr = true`: all components share one branch, so identifiers
  cannot ride branch names and per-component `Fixes` trailers do not
  apply. Issues and the failure sink still work; decompose warns once.
- Linear enabled but decompose ran without the hook (older manifest):
  the factory warns "sink inactive" and runs normally.
- Token unset in live mode: warn + sink inactive; decompose hook warns
  per failed call and components keep default branch names.

## Agents API seam (out of scope)

Linear's Agents API (@-mention delegation, agent sessions) is a
Developer Preview and intentionally NOT integrated. The seam when it
reaches GA: `auth_mode = "oauth"` already carries app-actor tokens;
`IssueCreateInput.delegateId` assigns an issue to an agent; webhook
handling ("Agent session events") would land as a new adapter beside
`LinearClient`, not inside it.

## App-actor OAuth setup (manual, one-time)

The interim setup uses a personal API key (Linear Settings > Security &
access > Personal API keys; a key created by you acts as you). To make
ralph act as its own app identity instead:

1. In Linear, go to Settings > API > OAuth applications > "Create new"
   (`linear.app/settings/api/applications/new`). Linear recommends a
   dedicated workspace for OAuth app management because every admin of
   that workspace can manage the app.
2. Name it (e.g. "Ralph Factory"), set any callback URL you control
   (for a local flow, `http://localhost:8484/callback` works); note
   the client id and secret.
3. Build the authorize URL - the `actor=app` parameter is what makes
   mutations run as the app rather than as a user:
   `https://linear.app/oauth/authorize?client_id=<CLIENT_ID>&redirect_uri=<CALLBACK>&response_type=code&scope=read,write&actor=app`
4. Open that URL as a workspace ADMIN of the target workspace and
   approve the installation (admin approval is required for
   workspace-scoped app installs; this is roadmap user decision 3).
5. Exchange the returned `code` for a token:
   `curl -X POST https://api.linear.app/oauth/token -d "grant_type=authorization_code" -d "code=<CODE>" -d "redirect_uri=<CALLBACK>" -d "client_id=<CLIENT_ID>" -d "client_secret=<CLIENT_SECRET>"`
6. Put the resulting access token in `KSTRL_LINEAR_TOKEN` and set
   `auth_mode = "oauth"` (or leave `auto`). No ralph code changes are
   needed - the client already sends OAuth tokens as
   `Authorization: Bearer`.

Notes: app-actor mode cannot request the `admin` scope; `read,write`
covers projects, issues, and comments. Workspace-level app-actor apps
also get dynamically increased rate limits.
