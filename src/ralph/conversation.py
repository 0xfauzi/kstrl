"""Interactive feature planning conversation.

Manages the back-and-forth between the user and a PM agent that reviews
specs, asks probing questions, and eventually generates a PRD.

Two-phase approach:
1. Conversation phase: free-text back-and-forth until the spec is thorough
2. Generation phase: a separate call with --json-schema that outputs
   a guaranteed-valid PRD JSON (no regex extraction needed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ralph.prd import PRD, validate_prd


@dataclass
class ConversationMessage:
    """A single message in the planning conversation."""

    role: str  # "user" or "assistant"
    content: str


@dataclass
class ConversationState:
    """Tracks the full conversation and whether a PRD has been generated."""

    messages: list[ConversationMessage] = field(default_factory=list)
    prd: PRD | None = None


CONVERSATION_SYSTEM_PROMPT = """\
You are a senior technical reviewer analyzing a feature specification. \
You combine the perspectives of a product manager, a staff engineer, \
and a reliability engineer. Your job is to have a focused conversation \
with the developer to produce a spec thorough enough for an autonomous \
coding agent to implement without human intervention.

## Your perspectives

Ask questions from each angle in a single turn (3-5 questions total \
per turn to minimize round trips):

Product:
- Are the user stories clear and atomic?
- What is in scope vs out of scope?
- Are acceptance criteria testable and unambiguous?

Engineering:
- What are the technical dependencies?
- What existing patterns or code should this integrate with?
- What is the testing and verification strategy?
- Are there architectural decisions that need to be made upfront?

Reliability:
- What are the failure modes and how should they be handled?
- What edge cases could break this?
- Are there security, data integrity, or performance concerns?

## Your behavior

1. REVIEW the provided specification or description carefully.
2. ASK 3-5 numbered questions per turn, drawn from the perspectives above.
3. Do NOT accept vague specs. Push back on hand-wavy requirements.
4. Keep going until the spec is precise enough for autonomous implementation.
5. When satisfied, say exactly:

   READY_TO_GENERATE

   Then provide a brief summary of what will be built and ask the \
developer to confirm.

## Important

- Do NOT generate a PRD yourself. A separate structured process handles that.
- Be direct and efficient. No pleasantries or filler.
- Group your questions by perspective so the developer can address them systematically.
"""

GENERATION_PROMPT_TEMPLATE = """\
You are generating a PRD (Product Requirements Document) from a \
completed specification conversation.

## Conversation transcript

{transcript}

## Instructions

Based on the conversation above, generate the PRD. Follow these rules:

- Stories must be small and atomic (one focused change per story)
- Priority: lower number = higher priority, unique integers starting at 1
- Order stories by dependency (if B depends on A, A gets lower priority)
- Acceptance criteria must be explicit and testable
- Include verification commands (typecheck, tests) as criteria where discussed
- Set "passes" to false and "notes" to "" for every story
- Do not invent features that were not discussed
- Use the branch name discussed, or derive one from the feature name
"""

# JSON Schema for Claude's --json-schema flag. Guarantees the output
# conforms to the PRD structure at the token level.
PRD_JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "branchName": {"type": "string"},
        "userStories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "acceptanceCriteria": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "priority": {"type": "integer"},
                    "passes": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
                "required": [
                    "id", "title", "acceptanceCriteria",
                    "priority", "passes", "notes",
                ],
            },
        },
    },
    "required": ["branchName", "userStories"],
})

# Marker the PM agent outputs when it's satisfied the spec is thorough
READY_MARKER = "READY_TO_GENERATE"


def build_conversation_prompt(
    messages: list[ConversationMessage],
) -> str:
    """Build the full prompt for the conversation phase.

    Each call to claude --print is stateless, so the entire conversation
    history is included in every invocation.
    """
    parts: list[str] = [CONVERSATION_SYSTEM_PROMPT, "\n---\n"]

    if messages:
        parts.append("Conversation so far:\n")
        for msg in messages:
            label = "Developer" if msg.role == "user" else "PM (you)"
            parts.append(f"### {label}:\n{msg.content}\n")

    parts.append(
        "\n---\n"
        "Continue the conversation. Ask your next questions, "
        "or if the spec is thorough enough, output READY_TO_GENERATE "
        "and summarize your understanding.\n"
    )

    return "\n".join(parts)


def build_generation_prompt(
    messages: list[ConversationMessage],
) -> str:
    """Build the prompt for the PRD generation phase.

    This prompt is sent with --output-format json --json-schema so
    Claude returns guaranteed-valid PRD JSON.
    """
    transcript_parts: list[str] = []
    for msg in messages:
        label = "Developer" if msg.role == "user" else "PM"
        transcript_parts.append(f"{label}: {msg.content}")

    transcript = "\n\n".join(transcript_parts)
    return GENERATION_PROMPT_TEMPLATE.format(transcript=transcript)


def response_has_ready_marker(response: str) -> bool:
    """Check if the PM agent's response contains the READY_TO_GENERATE marker."""
    return READY_MARKER in response


def parse_prd_from_json_output(raw_output: str) -> PRD | None:
    """Parse a PRD from Claude's --output-format json response.

    The raw output is Claude's result JSON which contains a
    'structured_output' field with the PRD data.
    """
    try:
        result = json.loads(raw_output)
    except json.JSONDecodeError:
        return None

    # Claude --output-format json wraps the output in a result envelope
    prd_data = result.get("structured_output")
    if prd_data is None:
        # Maybe the output IS the PRD directly (no envelope)
        prd_data = result

    errors = validate_prd(prd_data)
    if errors:
        return None

    return PRD.from_dict(prd_data)
