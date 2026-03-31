"""Interactive feature planning conversation.

Manages the back-and-forth between the user and a PM agent that reviews
specs, asks probing questions, and eventually generates a PRD.

The conversation is stateless per Claude invocation - the full history
is serialized into each prompt.
"""

from __future__ import annotations

import json
import re
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
6. When you believe the specification is complete, tell the developer \
you are ready to generate the PRD, summarize your understanding, and \
ask for confirmation.
7. Once confirmed, generate the PRD as a JSON code block.

## PRD format

When generating, output ONLY a JSON code block with this exact schema:

```json
{
  "branchName": "ralph/feature-name",
  "userStories": [
    {
      "id": "US-001",
      "title": "Short imperative description",
      "acceptanceCriteria": [
        "First testable requirement",
        "Second testable requirement"
      ],
      "priority": 1,
      "passes": false,
      "notes": ""
    }
  ]
}
```

## PRD rules

- Stories must be small and atomic (one focused change per story)
- Priority: lower number = higher priority, unique integers starting at 1
- Order stories by dependency (if B depends on A, A gets lower priority)
- Acceptance criteria must be explicit and testable
- Include verification commands (typecheck, tests) as criteria
- Set "passes" to false and "notes" to "" for every story
- Do not invent features that were not discussed

## Style

- Be conversational but focused
- Be direct - do not pad with pleasantries
- Summarize your understanding before generating the PRD
"""


def build_conversation_prompt(
    messages: list[ConversationMessage],
) -> str:
    """Build the full prompt for Claude with conversation history.

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
        "or if the spec is thorough enough, propose generating the PRD "
        "and ask for confirmation.\n"
    )

    return "\n".join(parts)


def try_extract_prd_from_response(response: str) -> PRD | None:
    """Extract a valid PRD JSON from an assistant response.

    Looks for JSON inside code blocks (```json ... ``` or ``` ... ```).
    Returns a PRD if valid JSON is found and passes schema validation,
    otherwise returns None (conversation should continue).
    """
    fence_pattern = r"```\w*\s*\n([\s\S]*?)\n```"
    matches = re.findall(fence_pattern, response)

    for match in matches:
        try:
            data = json.loads(match.strip())
        except json.JSONDecodeError:
            continue

        errors = validate_prd(data)
        if not errors:
            return PRD.from_dict(data)

    return None
