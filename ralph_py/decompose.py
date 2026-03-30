"""LLM-driven spec decomposition into components and PRDs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ralph_py.manifest import Component, ComponentStatus, Manifest
from ralph_py.prd import PRD

if TYPE_CHECKING:
    from ralph_py.agents.base import Agent
    from ralph_py.ui.base import UI

DECOMPOSE_PROMPT = """\
You are a senior software architect and product manager. Your job is to decompose
a feature specification into independent, atomic components that can be implemented
in parallel by separate coding agents.

Output ONLY valid JSON (no Markdown, no code fences, no comments, no explanation).

The output must be a JSON object with this exact structure:

{{
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
            "First testable requirement",
            "Second testable requirement",
            "Typecheck passes",
            "Tests pass"
          ],
          "priority": 1,
          "passes": false,
          "notes": ""
        }}
      ]
    }}
  ]
}}

Rules:
1. Component IDs must be kebab-case (lowercase, hyphens only).
2. Each component should be independently implementable and testable.
3. Dependencies reference other component IDs. Foundational components (data models,
   config, shared utilities) should have no dependencies.
4. Order components so foundational ones come first.
5. Each component should have 1-5 user stories. Stories must be small and atomic.
6. User story IDs must be globally unique across all components (e.g., US-001, US-002...).
7. Acceptance criteria must be explicit, testable, and include typecheck/test commands.
8. Priorities must be unique within each component, starting at 1.
9. Set "passes" to false and "notes" to "" for every story.
10. Minimize dependencies between components. Prefer independent components.
11. Do not invent UI elements, endpoints, or files not described in the spec.

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


def _validate_decompose_output(data: Any) -> list[str]:
    """Validate the decomposition output structure."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["Output must be a JSON object"]

    if "components" not in data:
        return ["Output must have a 'components' key"]

    components = data["components"]
    if not isinstance(components, list):
        return ["'components' must be an array"]

    if not components:
        return ["'components' must not be empty"]

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

        full_output = "\n".join(output_lines)

        try:
            data = _extract_json(full_output)
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
