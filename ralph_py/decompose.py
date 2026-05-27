"""LLM-driven spec decomposition into components and PRDs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.ui.base import UI


@dataclass
class SpecIssue:
    """A red-team finding raised by the architect during decomposition."""

    severity: str  # "blocker" | "major" | "minor"
    kind: str
    summary: str
    location: str = ""
    suggestion: str = ""


class SpecBlockerError(Exception):
    """Raised when the architect found blocker-severity spec issues.

    The decompose pipeline halts until the human resolves the spec
    rather than letting a vague spec produce a brittle implementation.
    The blocking issues are attached via ``issues``.
    """

    def __init__(self, issues: list[SpecIssue]):
        self.issues = issues
        summary_lines = [f"- [{i.severity}/{i.kind}] {i.summary}" for i in issues]
        super().__init__(
            "Spec has blocker-severity issues; resolve before re-running:\n"
            + "\n".join(summary_lines),
        )

DECOMPOSE_PROMPT = """\
You are a senior software architect AND a hostile spec auditor. You have
two jobs and you must do BOTH before decomposing:

  1. RED-TEAM the specification. Find every ambiguity, missing detail,
     contradiction, unstated assumption, and unspecified failure mode.
     Most specs are wrong somewhere; your default stance is suspicion.
  2. Decompose the spec into atomic, parallelizable components, but only
     to the extent the spec is concrete enough to decompose safely.

If the spec is too vague to decompose responsibly, return `spec_issues`
with the gaps you found AND an empty `components` array. Do not invent
behavior to fill silence; that is what produces brittle implementations
weeks later.

Output ONLY valid JSON (no Markdown, no code fences, no comments, no
explanation).

The output must be a JSON object with this exact structure:

{{
  "spec_issues": [
    {{
      "severity": "blocker|major|minor",
      "kind": "ambiguity|missing_detail|contradiction|unstated_assumption|undefined_failure_mode|out_of_scope_creep|other",
      "summary": "one-sentence statement of the issue",
      "location": "which part of the spec this is about (quote or paraphrase)",
      "suggestion": "what would resolve it (one sentence)"
    }}
  ],
  "components": [
    {{
      "id": "kebab-case-id",
      "title": "Short title",
      "description": "What this component does and why",
      "dependencies": ["other-component-id"],
      "userStories": [
        {{
          "id": "US-001",
          "title": "Short story title",
          "acceptanceCriteria": [
            "Concrete positive criterion with the actual expected behavior",
            "Concrete negative criterion: what happens on invalid input / failure / boundary",
            "Typecheck passes: <project typecheck command>",
            "Tests pass: <project test command>"
          ],
          "priority": 1,
          "passes": false,
          "notes": ""
        }}
      ]
    }}
  ]
}}

Decomposition rules:
1. Component IDs must be kebab-case (lowercase, hyphens only).
2. Each component should be independently implementable and testable.
3. Dependencies reference other component IDs. Foundational components
   (data models, config, shared utilities) should have no dependencies.
4. Order components so foundational ones come first.
5. Each component should have 1-5 user stories. Stories must be small
   and atomic.
6. User story IDs must be globally unique across all components
   (e.g., US-001, US-002...).
7. Acceptance criteria must be explicit and testable. Each story MUST
   include at least ONE negative criterion (error path, empty input,
   boundary value, unauthorized access, malformed payload - whatever
   applies to that story). Do NOT use placeholder text like "First
   testable requirement"; write the actual criterion.
8. Priorities must be unique within each component, starting at 1.
9. Set "passes" to false and "notes" to "" for every story.
10. Minimize dependencies between components. Prefer independent
    components.
11. Do not invent UI elements, endpoints, or files not described in the
    spec. If the spec is silent on something you would need to invent,
    add a `spec_issues` entry instead.

Red-team rules:
- Look for: ambiguous quantifiers ("fast", "secure", "user-friendly"),
  missing acceptance criteria (no error behavior specified, no empty/null
  handling, no concurrency story), undefined data shapes, missing
  authentication/authorization story, unspecified perf budgets, missing
  rollback / backwards-compat plan, contradictions between sections.
- "blocker": cannot safely decompose without resolving this
- "major": will likely cause rework or a fail-class bug if left
- "minor": worth raising but not blocking
- If you genuinely find no issues after reading carefully, return
  "spec_issues": []. Honesty over performance: do not invent issues to
  appear thorough.
- If any issue is "blocker", you MUST return "components": [] so the
  pipeline halts and the human can fix the spec.

Project name: {project_name}

================================================================================
SPECIFICATION
================================================================================

{spec_content}
"""


def _extract_json(text: str) -> Any:
    """Extract JSON from text, handling optional code fences."""
    # Try direct parse first
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Try extracting from code fences
    fence_pattern = r"```(?:json)?\s*\n(.*?)\n```"
    matches = re.findall(fence_pattern, stripped, re.DOTALL)
    for match in matches:
        try:
            return json.loads(match.strip())
        except json.JSONDecodeError:
            continue

    # Try finding JSON object boundaries
    brace_start = stripped.find("{")
    if brace_start >= 0:
        depth = 0
        for i in range(brace_start, len(stripped)):
            if stripped[i] == "{":
                depth += 1
            elif stripped[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[brace_start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError("No valid JSON found in output")


def _select_agent_output(agent: Any, output_lines: list[str]) -> str:
    """Return the best text candidate for JSON extraction from a finished
    agent run.

    Returns :attr:`agent.final_message` if it contains parseable JSON;
    otherwise returns the joined streamed output. This shields callers
    that pass the result through :func:`_extract_json` (e.g. via a
    domain-specific parser) from codex's prompt-echo behavior.
    """
    streamed = "\n".join(output_lines)
    final = getattr(agent, "final_message", None)
    if not final:
        return streamed
    try:
        _extract_json(final)
    except ValueError:
        return streamed
    return final


def _extract_agent_json(agent: Any, output_lines: list[str]) -> Any:
    """Extract JSON from a completed agent run, trying agent.final_message
    first and falling back to the streamed output.

    Codex CLI (and other agents that echo the input prompt back) include
    the JSON schema example inside their stdout, which can trip the
    first-brace heuristic in :func:`_extract_json`. ``agent.final_message``
    is populated by codex via ``--output-last-message`` and by
    ClaudeCodeAgent from its result event, and contains only the model's
    actual reply. Preferring it sidesteps the echoed-prompt problem.

    For CustomAgent (whose final_message is just the last non-empty line
    of streamed output), the multi-line JSON case is handled by the
    streamed-output fallback when final_message fails to parse.

    Raises :class:`ValueError` if neither candidate parses.
    """
    streamed = "\n".join(output_lines)
    final = getattr(agent, "final_message", None)

    candidates: list[str] = []
    if final:
        candidates.append(final)
    if streamed and streamed != final:
        candidates.append(streamed)

    last_error: ValueError | None = None
    for candidate in candidates:
        try:
            return _extract_json(candidate)
        except ValueError as exc:
            last_error = exc

    if last_error is None:
        raise ValueError("No agent output to parse")
    raise last_error


def _validate_decompose_output(data: Any) -> list[str]:
    """Validate the decomposition output structure.

    Empty components is permitted only when spec_issues contains at
    least one blocker - the architect is explicitly halting the pipeline
    until the human resolves the spec.
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Output must be a JSON object"]

    if "components" not in data:
        return ["Output must have a 'components' key"]

    components = data["components"]
    if not isinstance(components, list):
        return ["'components' must be an array"]

    if not components:
        spec_issues = data.get("spec_issues", [])
        if isinstance(spec_issues, list) and any(
            isinstance(s, dict) and s.get("severity") == "blocker"
            for s in spec_issues
        ):
            # Architect explicitly halted - this is a valid outcome.
            return []
        return ["'components' must not be empty (no blocker spec_issues to justify halt)"]

    seen_ids: set[str] = set()
    seen_story_ids: set[str] = set()

    for i, comp in enumerate(components):
        prefix = f"components[{i}]"

        if not isinstance(comp, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        comp_id = comp.get("id")
        if not isinstance(comp_id, str) or not comp_id:
            errors.append(f"{prefix}.id: must be a non-empty string")
        elif comp_id in seen_ids:
            errors.append(f"{prefix}.id: duplicate ID '{comp_id}'")
        else:
            seen_ids.add(comp_id)

        if not isinstance(comp.get("title"), str):
            errors.append(f"{prefix}.title: must be a string")

        if not isinstance(comp.get("description"), str):
            errors.append(f"{prefix}.description: must be a string")

        deps = comp.get("dependencies")
        if not isinstance(deps, list):
            errors.append(f"{prefix}.dependencies: must be an array")
        elif not all(isinstance(d, str) for d in deps):
            errors.append(f"{prefix}.dependencies: all items must be strings")

        stories = comp.get("userStories")
        if not isinstance(stories, list):
            errors.append(f"{prefix}.userStories: must be an array")
            continue

        for j, story in enumerate(stories):
            sp = f"{prefix}.userStories[{j}]"
            if not isinstance(story, dict):
                errors.append(f"{sp}: must be an object")
                continue

            story_id = story.get("id")
            if isinstance(story_id, str) and story_id:
                if story_id in seen_story_ids:
                    errors.append(f"{sp}.id: duplicate story ID '{story_id}'")
                seen_story_ids.add(story_id)

    # Check dependency references
    for comp in components:
        if not isinstance(comp, dict):
            continue
        comp_id = comp.get("id", "?")
        for dep in comp.get("dependencies", []):
            if isinstance(dep, str) and dep not in seen_ids:
                errors.append(
                    f"Component '{comp_id}' depends on unknown component '{dep}'"
                )

    return errors


_VALID_SEVERITIES = frozenset({"blocker", "major", "minor"})
_VALID_KINDS = frozenset({
    "ambiguity",
    "missing_detail",
    "contradiction",
    "unstated_assumption",
    "undefined_failure_mode",
    "out_of_scope_creep",
    "other",
})


def _parse_spec_issues(data: Any) -> list[SpecIssue]:
    """Extract typed SpecIssue entries from raw decompose output.

    Invalid entries (unknown severity, unknown kind, missing summary)
    are skipped rather than crashing decomposition. We surface what the
    LLM produced honestly even if some entries are malformed.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("spec_issues")
    if not isinstance(raw, list):
        return []
    issues: list[SpecIssue] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if severity not in _VALID_SEVERITIES:
            continue
        if kind not in _VALID_KINDS:
            continue
        if not summary:
            continue
        issues.append(SpecIssue(
            severity=severity,
            kind=kind,
            summary=summary,
            location=str(entry.get("location", "")).strip(),
            suggestion=str(entry.get("suggestion", "")).strip(),
        ))
    return issues


def _surface_spec_issues(issues: list[SpecIssue], ui: UI) -> None:
    """Render spec issues to the UI grouped by severity."""
    if not issues:
        ui.ok("Spec audit: no issues raised")
        return
    blockers = [i for i in issues if i.severity == "blocker"]
    majors = [i for i in issues if i.severity == "major"]
    minors = [i for i in issues if i.severity == "minor"]
    ui.section("Spec Audit Findings")
    for label, group, emit in (
        ("Blockers", blockers, ui.err),
        ("Major", majors, ui.warn),
        ("Minor", minors, ui.info),
    ):
        if not group:
            continue
        ui.kv(label, str(len(group)))
        for issue in group:
            emit(f"  [{issue.kind}] {issue.summary}")
            if issue.location:
                ui.info(f"    location: {issue.location}")
            if issue.suggestion:
                ui.info(f"    suggestion: {issue.suggestion}")


def _generate_component_prd(
    comp_data: dict[str, Any],
    root_dir: Path,
    branch_name: str,
) -> Path:
    """Generate a standard PRD file for one component.

    Returns the path to the generated prd.json.
    """
    comp_id: str = comp_data["id"]
    feature_dir: Path = root_dir / "scripts" / "ralph" / "feature" / comp_id
    feature_dir.mkdir(parents=True, exist_ok=True)

    prd_data: dict[str, Any] = {
        "branchName": branch_name,
        "userStories": comp_data["userStories"],
    }

    prd_path = feature_dir / "prd.json"
    with open(prd_path, "w") as f:
        json.dump(prd_data, f, indent=2)
        f.write("\n")

    # Validate the generated PRD
    errors = PRD.validate_schema(prd_data)
    if errors:
        raise ValueError(
            f"Generated PRD for '{comp_id}' has schema errors: {'; '.join(errors)}"
        )

    return prd_path


def decompose_spec(
    spec_path: Path,
    project_name: str,
    base_branch: str,
    single_pr: bool,
    agent: Agent,
    ui: UI,
    root_dir: Path,
    max_retries: int = 3,
) -> Manifest:
    """Decompose a spec into components and generate PRDs.

    Args:
        spec_path: Path to the markdown spec file
        project_name: Name for the project/factory run
        base_branch: Base git branch
        single_pr: Whether to use a single branch for all components
        agent: Agent to use for decomposition
        ui: UI for output
        root_dir: Project root directory
        max_retries: Max attempts for JSON parsing

    Returns:
        Manifest with generated components and PRD files
    """
    ui.section("Spec Decomposition")
    ui.kv("Spec", str(spec_path))
    ui.kv("Project", project_name)

    spec_content = spec_path.read_text()
    prompt = DECOMPOSE_PROMPT.format(
        project_name=project_name,
        spec_content=spec_content,
    )

    data = None
    last_error: str | None = None

    for attempt in range(1, max_retries + 1):
        ui.info(f"Decomposition attempt {attempt}/{max_retries}")

        if last_error:
            retry_prompt = (
                f"{prompt}\n\n"
                f"PREVIOUS ATTEMPT FAILED with error:\n{last_error}\n\n"
                f"Please fix the error and output valid JSON."
            )
        else:
            retry_prompt = prompt

        output_lines: list[str] = []
        for line in agent.run(retry_prompt, cwd=root_dir):
            output_lines.append(line)
            ui.stream_line("AI", line)

        try:
            data = _extract_agent_json(agent, output_lines)
        except ValueError as exc:
            last_error = str(exc)
            ui.warn(f"JSON extraction failed: {last_error}")
            continue

        validation_errors = _validate_decompose_output(data)
        if validation_errors:
            last_error = "; ".join(validation_errors)
            ui.warn(f"Validation failed: {last_error}")
            data = None
            continue

        last_error = None
        break

    if data is None:
        raise ValueError(
            f"Failed to decompose spec after {max_retries} attempts. "
            f"Last error: {last_error}"
        )

    # Surface red-team findings before doing any further work. If any
    # are blockers, halt before generating PRDs - the architect
    # explicitly judged the spec un-decomposable.
    spec_issues = _parse_spec_issues(data)
    _surface_spec_issues(spec_issues, ui)
    blockers = [i for i in spec_issues if i.severity == "blocker"]
    if blockers:
        raise SpecBlockerError(blockers)

    # Generate PRDs and build manifest components
    ui.section("Generating PRDs")
    manifest_components: list[Component] = []

    for comp_data in data["components"]:
        comp_id = comp_data["id"]
        if single_pr:
            branch = f"ralph/factory/{project_name}"
        else:
            branch = f"ralph/factory/{comp_id}"

        prd_path = _generate_component_prd(comp_data, root_dir, branch)
        rel_prd = prd_path.relative_to(root_dir).as_posix()

        manifest_components.append(
            Component(
                id=comp_id,
                title=comp_data["title"],
                description=comp_data["description"],
                dependencies=comp_data.get("dependencies", []),
                prd_path=rel_prd,
                branch_name=branch,
                status=ComponentStatus.PENDING.value,
            )
        )
        ui.ok(f"  {comp_id}: {len(comp_data['userStories'])} stories")

    manifest = Manifest(
        version="1",
        spec_file=spec_path.name,
        project_name=project_name,
        base_branch=base_branch,
        single_pr=single_pr,
        components=manifest_components,
    )

    # Validate DAG
    dag_errors = manifest.validate_dag()
    if dag_errors:
        ui.warn("DAG validation warnings:")
        for err in dag_errors:
            ui.warn(f"  {err}")

    # Save manifest
    manifest_path = root_dir / "scripts" / "ralph" / "manifest.json"
    manifest.save(manifest_path)
    ui.ok(f"Manifest saved: {manifest_path}")

    ui.section("Decomposition Summary")
    ui.kv("Components", str(len(manifest.components)))
    total_stories = sum(
        len(comp_data.get("userStories", []))
        for comp_data in data["components"]
    )
    ui.kv("Total stories", str(total_stories))

    return manifest
