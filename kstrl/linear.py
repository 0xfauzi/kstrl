"""Linear integration - GraphQL client, decompose sync, progress sink (R7.4).

Design (docs/linear-integration.md has the full rationale):

- All Linear traffic goes through ``LinearClient.execute`` - one HTTP
  entry point over stdlib urllib (no new dependencies), defensive
  parsing, modest client-side throttling, and a dry-run mode that
  records mutations instead of sending them.
- Decompose creates one Linear project per manifest and one issue per
  component (user stories as a markdown checklist inside the issue -
  see docs for why not sub-issues); non-blocker spec_issues are filed
  into the team's Triage inbox.
- Idempotency: every created object's UUID is derived deterministically
  from an external key ``<sync_key>:<component_id>`` (sync_key is the
  run id of the run that first synced). Re-sending a create with the
  same UUID cannot mint a second object; the key is also embedded in
  the object description for auditability. Retries and resumed runs
  reuse the ids persisted in the manifest and therefore UPDATE rather
  than duplicate.
- Status transitions cost zero API calls: branch names carry the issue
  identifier and PR bodies carry "Fixes <ID>", so Linear's GitHub
  integration drives In Progress / Done.
- The Agents API (@-mention delegation) is a Developer Preview and
  deliberately out of scope. The seam for it is ``auth_mode="oauth"``
  plus the ``delegateId`` input on issueCreate; when it reaches GA a
  delegation adapter can extend ``LinearClient`` without touching the
  sink or the decompose hook.

Auth: the token is read from the env var named by
``LinearConfig.token_env`` (default ``KSTRL_LINEAR_TOKEN``) at call
time and never appears in code, config files, logs, or error messages.
Personal API keys use a bare ``Authorization: <key>`` header; OAuth
(app-actor) tokens use ``Authorization: Bearer <token>``;
``auth_mode="auto"`` sniffs the documented ``lin_api_`` key prefix.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kstrl import envcompat
from kstrl.config import _parse_bool, load_toml_section, resolve_config_file

if TYPE_CHECKING:
    from kstrl.decompose import SpecIssue
    from kstrl.manifest import Manifest

_VALID_AUTH_MODES = frozenset({"auto", "api_key", "oauth"})

# Linear reports rate limiting as HTTP 400 with this error code in the
# GraphQL errors array (NOT as HTTP 429) - see docs/linear-integration.md.
_RATELIMITED = "RATELIMITED"

_MAX_ERROR_BODY_CHARS = 500
_MAX_COMMENT_CHARS = 4000
_MAX_TITLE_CHARS = 255


class LinearError(Exception):
    """A Linear API call failed. Messages never contain the token."""

    def __init__(self, message: str, codes: list[str] | None = None) -> None:
        super().__init__(message)
        self.codes: list[str] = codes or []


@dataclass
class LinearConfig:
    """``[linear]`` section - Linear integration knobs.

    Disabled by default; enabling requires ``team_id`` (the UUID of the
    Linear team ralph files projects and issues under). ``dry_run``
    records mutations instead of sending them (the default in tests).
    """

    enabled: bool = False
    team_id: str = ""
    token_env: str = "KSTRL_LINEAR_TOKEN"
    auth_mode: str = "auto"
    api_url: str = "https://api.linear.app/graphql"
    dry_run: bool = False
    timeout_seconds: float = 30.0
    # Modest client-side throttle: minimum spacing between requests.
    # The shared pool is 2,500 req/hr (API key) / 5,000 (OAuth app);
    # a decompose issues ~2 + components + spec_issues calls total.
    min_request_interval: float = 0.5

    def __post_init__(self) -> None:
        if self.auth_mode not in _VALID_AUTH_MODES:
            raise ValueError(
                f"Invalid LinearConfig.auth_mode {self.auth_mode!r}; "
                f"must be one of {sorted(_VALID_AUTH_MODES)}"
            )
        if self.enabled and not self.team_id:
            raise ValueError(
                "LinearConfig.enabled requires team_id (the Linear team "
                "UUID); set [linear] team_id or RALPH_LINEAR_TEAM_ID"
            )
        if not self.token_env:
            raise ValueError("LinearConfig.token_env must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("LinearConfig.timeout_seconds must be positive")
        if self.min_request_interval < 0:
            raise ValueError(
                "LinearConfig.min_request_interval must not be negative"
            )

    @classmethod
    def from_env(cls) -> LinearConfig:
        return cls(
            enabled=_parse_bool(envcompat.get("KSTRL_LINEAR_ENABLED")),
            team_id=envcompat.get("KSTRL_LINEAR_TEAM_ID", ""),
            token_env=envcompat.get("KSTRL_LINEAR_TOKEN_ENV", "KSTRL_LINEAR_TOKEN"
            ),
            auth_mode=envcompat.get("KSTRL_LINEAR_AUTH_MODE", "auto"),
            api_url=envcompat.get("KSTRL_LINEAR_API_URL", "https://api.linear.app/graphql"
            ),
            dry_run=_parse_bool(envcompat.get("KSTRL_LINEAR_DRY_RUN")),
            timeout_seconds=float(envcompat.get("KSTRL_LINEAR_TIMEOUT", "30")),
            min_request_interval=float(
                envcompat.get("KSTRL_LINEAR_MIN_INTERVAL", "0.5")
            ),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> LinearConfig:
        """Load linear config with precedence: env > toml > defaults."""
        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "linear")
        if "enabled" in section:
            config.enabled = bool(section["enabled"])
        if "team_id" in section:
            config.team_id = str(section["team_id"])
        if "token_env" in section:
            config.token_env = str(section["token_env"])
        if "auth_mode" in section:
            config.auth_mode = str(section["auth_mode"])
        if "api_url" in section:
            config.api_url = str(section["api_url"])
        if "dry_run" in section:
            config.dry_run = bool(section["dry_run"])
        if "timeout_seconds" in section:
            config.timeout_seconds = float(section["timeout_seconds"])
        if "min_request_interval" in section:
            config.min_request_interval = float(section["min_request_interval"])
        # Env overrides
        if envcompat.contains("KSTRL_LINEAR_ENABLED"):
            config.enabled = _parse_bool(envcompat.require("KSTRL_LINEAR_ENABLED"))
        if envcompat.contains("KSTRL_LINEAR_TEAM_ID"):
            config.team_id = envcompat.require("KSTRL_LINEAR_TEAM_ID")
        if envcompat.contains("KSTRL_LINEAR_TOKEN_ENV"):
            config.token_env = envcompat.require("KSTRL_LINEAR_TOKEN_ENV")
        if envcompat.contains("KSTRL_LINEAR_AUTH_MODE"):
            config.auth_mode = envcompat.require("KSTRL_LINEAR_AUTH_MODE")
        if envcompat.contains("KSTRL_LINEAR_API_URL"):
            config.api_url = envcompat.require("KSTRL_LINEAR_API_URL")
        if envcompat.contains("KSTRL_LINEAR_DRY_RUN"):
            config.dry_run = _parse_bool(envcompat.require("KSTRL_LINEAR_DRY_RUN"))
        if envcompat.contains("KSTRL_LINEAR_TIMEOUT"):
            config.timeout_seconds = float(envcompat.require("KSTRL_LINEAR_TIMEOUT"))
        if envcompat.contains("KSTRL_LINEAR_MIN_INTERVAL"):
            config.min_request_interval = float(
                envcompat.require("KSTRL_LINEAR_MIN_INTERVAL")
            )
        # Re-validate after assignment - typos in env or TOML must surface
        config.__post_init__()
        return config


def deterministic_uuid(key: str) -> str:
    """Derive a stable UUID (v4 format) from an external key.

    Linear's create mutations accept a client-generated id "in UUID v4
    format"; deriving it from the external key makes every create
    idempotent at the API boundary - re-sending the same logical create
    carries the same id, so it cannot mint a second object. SHA-256 of
    the key with the v4 version/variant bits forced keeps the value
    format-valid while staying deterministic.
    """
    digest = bytearray(hashlib.sha256(key.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40  # version 4
    digest[8] = (digest[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(digest)))


@dataclass(frozen=True)
class IssueRef:
    """A created Linear issue: UUID plus human identifier (e.g. EXC-42)."""

    id: str
    identifier: str


@dataclass
class DecomposeSync:
    """Result of syncing one decompose output to Linear."""

    sync_key: str
    project_id: str
    issues: dict[str, IssueRef] = field(default_factory=dict)


class LinearClient:
    """Minimal Linear GraphQL client.

    Every request funnels through :meth:`execute` (requirement: one
    HTTP entry point). In dry-run mode ``execute`` records the mutation
    in :attr:`recorded` and returns the caller-supplied synthesized
    response instead of touching the network. ``recorded`` is appended
    to in live mode too - it is the in-memory audit trail tests assert
    against.
    """

    def __init__(
        self,
        config: LinearConfig,
        warn: Callable[[str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._warn = warn or (lambda _msg: None)
        # Informational channel for dry-run mutation lines; separate
        # from warn so "would have sent X" never reads as a problem.
        self._log = log or (lambda _msg: None)
        self._last_request: float = 0.0
        self._dry_seq: int = 0
        self.recorded: list[tuple[str, dict[str, Any]]] = []

    # -- auth ---------------------------------------------------------

    def _token(self) -> str:
        token = envcompat.get(self.config.token_env) or ""
        if not token:
            raise LinearError(
                f"linear: env var {self.config.token_env} is unset or "
                "empty; cannot authenticate"
            )
        return token

    def _auth_header(self) -> str:
        token = self._token()
        mode = self.config.auth_mode
        if mode == "auto":
            # Personal API keys are documented with the lin_api_ prefix
            # and a bare Authorization header; anything else is treated
            # as an OAuth (app-actor) bearer token.
            mode = "api_key" if token.startswith("lin_api_") else "oauth"
        if mode == "api_key":
            return token
        return f"Bearer {token}"

    # -- transport ----------------------------------------------------

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.config.min_request_interval - (now - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def execute(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any],
        dry_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST one GraphQL document; return the ``data`` object.

        ``dry_data`` is the response ``data`` synthesized in dry-run
        mode (built by the typed helpers from their client-generated
        ids, so dry-run flows exercise the same code paths).
        """
        self.recorded.append((operation, variables))
        if self.config.dry_run:
            self._log(
                f"linear dry-run: {operation} "
                f"{json.dumps(variables, sort_keys=True, default=str)[:300]}"
            )
            return dry_data if dry_data is not None else {}
        payload = self._post(operation, query, variables)
        # One retry when rate limited: Linear reports RATELIMITED as
        # HTTP 400 with an error code, not 429. Modest backoff only -
        # a persistently exhausted shared pool must surface, not spin.
        if payload is None:
            time.sleep(max(2.0, self.config.min_request_interval * 4))
            payload = self._post(operation, query, variables)
            if payload is None:
                raise LinearError(
                    f"linear: {operation} rate limited twice; giving up",
                    codes=[_RATELIMITED],
                )
        return payload

    def _post(
        self, operation: str, query: str, variables: dict[str, Any]
    ) -> dict[str, Any] | None:
        """One HTTP round trip. None means RATELIMITED (retryable)."""
        self._throttle()
        body = json.dumps({"query": query, "variables": variables})
        request = urllib.request.Request(
            self.config.api_url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": self._auth_header(),
                "User-Agent": "ralph-factory",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            parsed = self._parse_body(raw)
            codes = self._error_codes(parsed)
            if _RATELIMITED in codes:
                return None
            raise LinearError(
                f"linear: {operation} failed with HTTP {exc.code}: "
                f"{self._error_summary(parsed, raw)}",
                codes=codes,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LinearError(
                f"linear: {operation} transport failure: {exc}"
            ) from exc
        parsed = self._parse_body(raw)
        if parsed is None:
            raise LinearError(
                f"linear: {operation} returned a non-JSON response"
            )
        codes = self._error_codes(parsed)
        if _RATELIMITED in codes:
            return None
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            raise LinearError(
                f"linear: {operation} returned errors: "
                f"{self._error_summary(parsed, raw)}",
                codes=codes,
            )
        data = parsed.get("data")
        if not isinstance(data, dict):
            raise LinearError(
                f"linear: {operation} response has no data object"
            )
        return data

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _error_codes(parsed: dict[str, Any] | None) -> list[str]:
        if parsed is None:
            return []
        errors = parsed.get("errors")
        if not isinstance(errors, list):
            return []
        codes: list[str] = []
        for entry in errors:
            if not isinstance(entry, dict):
                continue
            extensions = entry.get("extensions")
            if isinstance(extensions, dict):
                code = extensions.get("code")
                if isinstance(code, str):
                    codes.append(code)
        return codes

    @staticmethod
    def _error_summary(parsed: dict[str, Any] | None, raw: bytes) -> str:
        if parsed is not None and isinstance(parsed.get("errors"), list):
            messages = [
                str(e.get("message", ""))
                for e in parsed["errors"]
                if isinstance(e, dict)
            ]
            summary = "; ".join(m for m in messages if m)
            if summary:
                return summary[:_MAX_ERROR_BODY_CHARS]
        return raw.decode("utf-8", errors="replace")[:_MAX_ERROR_BODY_CHARS]

    # -- typed operations ---------------------------------------------

    def create_project(
        self, name: str, team_id: str, client_id: str, description: str = ""
    ) -> str:
        """Create a project under one team; returns the project UUID."""
        query = (
            "mutation ProjectCreate($input: ProjectCreateInput!) {"
            " projectCreate(input: $input) {"
            " success project { id } } }"
        )
        variables = {
            "input": {
                "id": client_id,
                "name": name[:_MAX_TITLE_CHARS],
                "teamIds": [team_id],
                "description": description,
            }
        }
        dry_data = {"projectCreate": {"success": True, "project": {"id": client_id}}}
        try:
            data = self.execute("projectCreate", query, variables, dry_data)
        except LinearError:
            # Duplicate client id (double-fire of the same logical
            # create) is success: recover the existing project.
            existing = self._get_project(client_id)
            if existing is not None:
                return existing
            raise
        payload = data.get("projectCreate")
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearError("linear: projectCreate did not report success")
        project = payload.get("project")
        if not isinstance(project, dict) or not isinstance(
            project.get("id"), str
        ):
            raise LinearError("linear: projectCreate returned no project id")
        return str(project["id"])

    def _get_project(self, project_id: str) -> str | None:
        query = "query Project($id: String!) { project(id: $id) { id } }"
        try:
            data = self.execute(
                "project",
                query,
                {"id": project_id},
                dry_data={"project": {"id": project_id}},
            )
        except LinearError:
            return None
        project = data.get("project")
        if isinstance(project, dict) and isinstance(project.get("id"), str):
            return str(project["id"])
        return None

    def create_issue(
        self,
        team_id: str,
        title: str,
        description: str,
        client_id: str,
        project_id: str = "",
    ) -> IssueRef:
        """Create an issue; returns its UUID and human identifier.

        Omitting ``stateId`` on a triage-enabled team files the issue
        into Triage automatically (documented behavior) - spec-issue
        callers rely on this by passing no project.
        """
        query = (
            "mutation IssueCreate($input: IssueCreateInput!) {"
            " issueCreate(input: $input) {"
            " success issue { id identifier } } }"
        )
        issue_input: dict[str, Any] = {
            "id": client_id,
            "teamId": team_id,
            "title": title[:_MAX_TITLE_CHARS],
            "description": description,
        }
        if project_id:
            issue_input["projectId"] = project_id
        self._dry_seq += 1
        dry_data = {
            "issueCreate": {
                "success": True,
                "issue": {"id": client_id, "identifier": f"DRY-{self._dry_seq}"},
            }
        }
        try:
            data = self.execute(
                "issueCreate", query, {"input": issue_input}, dry_data
            )
        except LinearError:
            existing = self.get_issue(client_id)
            if existing is not None:
                return existing
            raise
        return self._issue_ref(data, "issueCreate")

    def update_issue(
        self, issue_id: str, title: str, description: str
    ) -> IssueRef:
        """Update an existing issue's title and description."""
        query = (
            "mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {"
            " issueUpdate(id: $id, input: $input) {"
            " success issue { id identifier } } }"
        )
        variables = {
            "id": issue_id,
            "input": {
                "title": title[:_MAX_TITLE_CHARS],
                "description": description,
            },
        }
        dry_data = {
            "issueUpdate": {
                "success": True,
                "issue": {"id": issue_id, "identifier": "DRY-UPDATED"},
            }
        }
        data = self.execute("issueUpdate", query, variables, dry_data)
        return self._issue_ref(data, "issueUpdate")

    def get_issue(self, issue_id: str) -> IssueRef | None:
        """Fetch an issue by UUID; None when absent or on any failure.

        Only used for duplicate-create recovery, so it swallows errors
        - the caller re-raises the original create failure when the
        issue genuinely does not exist.
        """
        query = (
            "query Issue($id: String!) {"
            " issue(id: $id) { id identifier } }"
        )
        try:
            data = self.execute(
                "issue",
                query,
                {"id": issue_id},
                dry_data={"issue": {"id": issue_id, "identifier": "DRY-GET"}},
            )
        except LinearError:
            return None
        issue = data.get("issue")
        if (
            isinstance(issue, dict)
            and isinstance(issue.get("id"), str)
            and isinstance(issue.get("identifier"), str)
        ):
            return IssueRef(id=str(issue["id"]), identifier=str(issue["identifier"]))
        return None

    def create_comment(self, issue_id: str, body: str, client_id: str) -> None:
        """Create a comment on an issue (best-effort audit trail)."""
        query = (
            "mutation CommentCreate($input: CommentCreateInput!) {"
            " commentCreate(input: $input) { success } }"
        )
        variables = {
            "input": {
                "id": client_id,
                "issueId": issue_id,
                "body": body[:_MAX_COMMENT_CHARS],
            }
        }
        dry_data = {"commentCreate": {"success": True}}
        data = self.execute("commentCreate", query, variables, dry_data)
        payload = data.get("commentCreate")
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearError("linear: commentCreate did not report success")

    @staticmethod
    def _issue_ref(data: dict[str, Any], operation: str) -> IssueRef:
        payload = data.get(operation)
        if not isinstance(payload, dict) or not payload.get("success"):
            raise LinearError(f"linear: {operation} did not report success")
        issue = payload.get("issue")
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("id"), str)
            or not isinstance(issue.get("identifier"), str)
        ):
            raise LinearError(
                f"linear: {operation} returned a malformed issue object"
            )
        return IssueRef(id=str(issue["id"]), identifier=str(issue["identifier"]))


# -- decompose hook ----------------------------------------------------


def linear_branch_name(identifier: str, comp_id: str) -> str:
    """Branch name carrying the Linear issue identifier.

    The identifier rides as its own lowercase hyphen-delimited token -
    the format Linear's own "Copy git branch name" emits - so the
    GitHub integration links the PR to the issue with zero API calls.
    """
    return f"ralph/factory/{identifier.lower()}-{comp_id}"


def _story_checklist(comp_data: dict[str, Any]) -> str:
    lines: list[str] = []
    stories = comp_data.get("userStories")
    if isinstance(stories, list) and stories:
        lines.append("## User stories")
        lines.append("")
        for story in stories:
            if not isinstance(story, dict):
                continue
            story_id = str(story.get("id", "")).strip()
            title = str(story.get("title", "")).strip()
            label = f"{story_id}: {title}".strip(": ") or "(untitled)"
            lines.append(f"- [ ] {label}")
    return "\n".join(lines)


def _external_key_footer(key: str) -> str:
    return f"\n\n---\n`ralph external key: {key}`"


def sync_decompose(
    project_name: str,
    components: list[dict[str, Any]],
    spec_issues: list[SpecIssue],
    config: LinearConfig,
    client: LinearClient,
    warn: Callable[[str], None],
    sync_key: str | None = None,
) -> DecomposeSync | None:
    """Create the Linear project, component issues, and triage issues.

    Never raises: any failure warns and degrades (a component without
    an issue keeps its default branch name; a total failure returns
    None and decompose proceeds without Linear). ``sync_key`` defaults
    to a fresh run id; passing one makes the derived client UUIDs
    reproducible, which is what makes double-fires converge on the
    same objects instead of duplicating them.
    """
    if not config.enabled:
        return None
    if sync_key is None:
        from kstrl.knowledge import current_run_id

        sync_key = current_run_id()
    try:
        project_key = f"{sync_key}:project"
        project_id = client.create_project(
            name=project_name,
            team_id=config.team_id,
            client_id=deterministic_uuid(project_key),
            description=(
                f"Ralph factory run for spec '{project_name}'."
                f"{_external_key_footer(project_key)}"
            ),
        )
    except LinearError as exc:
        warn(f"linear: project creation failed, continuing without: {exc}")
        return None

    sync = DecomposeSync(sync_key=sync_key, project_id=project_id)

    for comp_data in components:
        comp_id = str(comp_data.get("id", ""))
        if not comp_id:
            continue
        external_key = f"{sync_key}:{comp_id}"
        description = str(comp_data.get("description", ""))
        checklist = _story_checklist(comp_data)
        if checklist:
            description = f"{description}\n\n{checklist}"
        description += _external_key_footer(external_key)
        try:
            sync.issues[comp_id] = client.create_issue(
                team_id=config.team_id,
                title=str(comp_data.get("title", comp_id)),
                description=description,
                client_id=deterministic_uuid(external_key),
                project_id=project_id,
            )
        except LinearError as exc:
            warn(
                f"linear: issue creation for component '{comp_id}' "
                f"failed, it keeps its default branch: {exc}"
            )

    for index, issue in enumerate(spec_issues):
        # Blockers never reach this hook (decompose halts first);
        # majors/minors land in the team Triage inbox: no stateId on a
        # triage-enabled team routes there automatically, and no
        # projectId - spec problems are about the spec, not a component.
        external_key = f"{sync_key}:spec:{index}"
        body = (
            f"Severity: {issue.severity}\nKind: {issue.kind}\n\n"
            f"{issue.summary}"
        )
        if issue.location:
            body += f"\n\nLocation: {issue.location}"
        if issue.suggestion:
            body += f"\n\nSuggestion: {issue.suggestion}"
        body += _external_key_footer(external_key)
        try:
            client.create_issue(
                team_id=config.team_id,
                title=f"[spec] {issue.summary}",
                description=body,
                client_id=deterministic_uuid(external_key),
            )
        except LinearError as exc:
            warn(f"linear: triage issue for spec finding failed: {exc}")

    return sync


def resync_components(
    manifest: Manifest,
    components: list[dict[str, Any]],
    config: LinearConfig,
    client: LinearClient,
    warn: Callable[[str], None],
) -> None:
    """Push updated titles/stories to issues a manifest already maps.

    The UPDATE half of the idempotency contract: when a manifest
    component already carries a Linear issue id, a re-sync must mutate
    that issue in place, never create a sibling. Never raises.
    """
    by_id = {str(c.get("id", "")): c for c in components}
    for comp in manifest.components:
        if not comp.linear_issue_id:
            continue
        comp_data = by_id.get(comp.id)
        if comp_data is None:
            continue
        external_key = f"{manifest.linear_sync_key}:{comp.id}"
        description = str(comp_data.get("description", comp.description))
        checklist = _story_checklist(comp_data)
        if checklist:
            description = f"{description}\n\n{checklist}"
        description += _external_key_footer(external_key)
        try:
            client.update_issue(
                comp.linear_issue_id,
                title=str(comp_data.get("title", comp.title)),
                description=description,
            )
        except LinearError as exc:
            warn(f"linear: update of issue for '{comp.id}' failed: {exc}")


# -- progress sink -----------------------------------------------------


class LinearSink:
    """ProgressLog sink mapping run events to Linear mutations.

    Deliberately narrow: status transitions ride the GitHub
    integration for free, so the sink only mirrors the events a human
    would want surfaced on the issue - component failures and budget
    halts - as comments. Every comment's UUID derives from the event
    fingerprint, so a double-fired event cannot produce a second
    comment even across process restarts; an in-process seen-set
    short-circuits the common case.
    """

    def __init__(
        self,
        client: LinearClient,
        issue_ids: dict[str, str],
        run_id: str,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._issue_ids = issue_ids
        self._run_id = run_id
        self._warn = warn or (lambda _msg: None)
        self._sent: set[str] = set()

    def handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle(event)
        except Exception as exc:  # noqa: BLE001 - sink must never raise
            self._warn(f"linear sink failed (non-fatal): {exc}")

    def _handle(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event", ""))
        component = event.get("component")
        data = event.get("data") or {}
        if not isinstance(component, str) or not isinstance(data, dict):
            return
        body = self._comment_body(event_type, data)
        if body is None:
            return
        issue_id = self._issue_ids.get(component)
        if not issue_id:
            return
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        dedupe_key = f"{event_type}:{component}:{fingerprint}"
        if dedupe_key in self._sent:
            return
        self._sent.add(dedupe_key)
        self._client.create_comment(
            issue_id=issue_id,
            body=body,
            client_id=deterministic_uuid(f"{self._run_id}:{dedupe_key}"),
        )

    def _comment_body(
        self, event_type: str, data: dict[str, Any]
    ) -> str | None:
        if event_type == "component_failed":
            error = str(data.get("error", ""))[:2000]
            return f"Ralph run `{self._run_id}`: component failed.\n\n{error}"
        if event_type == "budget_exceeded":
            return (
                f"Ralph run `{self._run_id}`: run token budget exceeded "
                f"({data.get('total_tokens')}/{data.get('max_total_tokens')}); "
                "component failed without further adversarial calls."
            )
        return None


def build_linear_sink(
    manifest: Manifest,
    config: LinearConfig,
    run_id: str,
    warn: Callable[[str], None],
) -> LinearSink | None:
    """Construct the sink for a factory run, or None when inactive.

    Inactive when the integration is disabled, when the manifest
    carries no Linear issue ids (decompose ran without the hook), or
    when the token is missing in live mode - each warns rather than
    failing the run.
    """
    if not config.enabled:
        return None
    issue_ids = {
        comp.id: comp.linear_issue_id
        for comp in manifest.components
        if comp.linear_issue_id
    }
    if not issue_ids:
        warn(
            "linear: enabled but the manifest has no Linear issue ids "
            "(decompose ran without the hook?); sink inactive"
        )
        return None
    if not config.dry_run and not (envcompat.get(config.token_env) or ""):
        warn(
            f"linear: env var {config.token_env} is unset; sink inactive"
        )
        return None
    client = LinearClient(config, warn=warn)
    return LinearSink(client, issue_ids, run_id=run_id, warn=warn)
