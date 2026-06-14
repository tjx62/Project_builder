"""
The planner picks which specialists should handle a user request.

Runs a single forced-tool-use call on the raw Anthropic SDK — no CrewAI.
Forced tool-use (tool_choice={"type":"tool","name":"plan"}) guarantees the
model returns a JSON block matching PlannerOutput's schema, so we parse it
directly from block.input without any regex.
"""

import json
from typing import List
from pydantic import BaseModel, Field
from executor import _strip_prefix, _is_opus
from specialists import SPECIALIST_DESCRIPTIONS


class PlannerOutput(BaseModel):
    specialists: List[str] = Field(
        description="Specialist IDs in execution order (handoff sequence). Foundation specialists like VPC come first, dependents like EC2 come after."
    )
    reasoning: str = Field(
        description="Short explanation of why these specialists were chosen and why they're in this order."
    )


def plan_specialists(client, model: str, project_request: str,
                     additional_context: str) -> PlannerOutput:
    """Run the planner and return a PlannerOutput.

    Args:
        client:           anthropic.Anthropic or anthropic.AnthropicBedrock client.
        model:            Model id (may carry an anthropic/ or bedrock/ prefix).
        project_request:  The user's plain-English request.
        additional_context: Org context string (may be the default placeholder).
    """
    available_block = "\n".join(
        f"  - {sid}: {desc}" for sid, desc in SPECIALIST_DESCRIPTIONS.items()
    )

    system = (
        "You are a Technical Project Planner.\n\n"
        "Your goal: Read the user request and select the minimum set of specialists "
        "needed to complete it. Return them in execution order: foundation specialists "
        "(networking, IAM) first, then anything that depends on them (compute, storage, "
        "application code).\n\n"
        "You are a principal engineer who scopes projects to the right team. You never "
        "over-staff. If a request only needs an S3 bucket, you only pick the S3 "
        "specialist — not the entire AWS team."
    )

    user = (
        f"User project request:\n{project_request}\n\n"
        f"Additional organizational context:\n{additional_context}\n\n"
        f"Available specialists:\n{available_block}\n\n"
        "Select only the specialists actually needed. Call the `plan` tool with your answer."
    )

    tool_schema = {
        "name": "plan",
        "description": "Return the ordered specialist list and reasoning.",
        "input_schema": {
            "type": "object",
            "properties": {
                "specialists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specialist IDs in execution order.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why these specialists were chosen and in this order.",
                },
            },
            "required": ["specialists", "reasoning"],
        },
    }

    raw_model = _strip_prefix(model)
    kwargs = dict(
        model=raw_model,
        max_tokens=1024,
        system=system,
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": "plan"},
        messages=[{"role": "user", "content": user}],
    )
    if not _is_opus(raw_model):
        kwargs["temperature"] = 0.2

    resp = client.messages.create(**kwargs)

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "plan":
            data = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return PlannerOutput(**data)

    raise RuntimeError(f"Planner returned no tool_use block. Content: {resp.content}")
