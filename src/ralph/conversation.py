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
You are a thorough product manager and requirements reviewer. Your job is \
to have a conversation with the developer to produce a complete, \
unambiguous specification that can be turned into implementable user stories.

## Your behavior

1. REVIEW the provided specification or description carefully.
2. ASK probing questions about:
   - Edge cases and error handling
   - Failure modes and recovery
   - Dependencies on existing code or external systems
   - Missing acceptance criteria
   - Vague or ambiguous requirements
   - Security, performance, and scalability
   - What is explicitly OUT of scope
3. Do NOT accept vague specs. Push back on hand-wavy requirements.
4. Number your questions so the developer can reference them.
5. Keep asking until you are confident the spec is thorough.
6. When you believe the specification is complete, say exactly:

   READY_TO_GENERATE

   Then summarize your understanding and ask the developer to confirm.

## Important

- Do NOT generate a PRD yourself. When you are satisfied, output the \
marker READY_TO_GENERATE and wait for confirmation. A separate process \
will handle the actual PRD generation.
- Be conversational but focused.
- Be direct - do not pad with pleasantries.
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
