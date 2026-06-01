"""
The planner agent looks at a user request and picks which specialists
should handle it, in execution order.

It runs as its own mini-crew before the main pipeline kicks off, so it doesn't
interfere with the main execution loop and uses minimal tokens.
"""

from typing import List
from pydantic import BaseModel, Field
from crewai import Agent, Task, Crew, Process
from specialists import SPECIALIST_DESCRIPTIONS


class PlannerOutput(BaseModel):
    """Structured output the planner produces.

    Using a Pydantic model means CrewAI will force the LLM to return valid JSON
    matching this schema, so we don't have to regex-parse free-form prose.
    """
    specialists: List[str] = Field(
        description="Specialist IDs in execution order (handoff sequence). Foundation specialists like VPC come first, dependents like EC2 come after."
    )
    reasoning: str = Field(
        description="Short explanation of why these specialists were chosen and why they're in this order."
    )


def plan_specialists(llm, project_request: str, additional_context: str) -> PlannerOutput:
    """Runs the planner and returns the structured list of specialists."""

    # Build a description block listing every available specialist for the LLM.
    available_block = "\n".join(
        f"  - {sid}: {desc}" for sid, desc in SPECIALIST_DESCRIPTIONS.items()
    )

    planner_agent = Agent(
        role='Technical Project Planner',
        goal=(
            'Read the user request and select the minimum set of specialists needed to complete it. '
            'Return them in execution order: foundation specialists (networking, IAM) first, '
            'then anything that depends on them (compute, storage, application code).'
        ),
        backstory=(
            'You are a principal engineer who scopes projects to the right team. You never over-staff. '
            'If a request only needs an S3 bucket, you only pick the S3 specialist — not the entire AWS team.'
        ),
        llm=llm,
        allow_delegation=False
    )

    planning_task = Task(
        description=(
            f"User project request:\n{project_request}\n\n"
            f"Additional organizational context:\n{additional_context}\n\n"
            f"Available specialists:\n{available_block}\n\n"
            "Select only the specialists actually needed. Return them in handoff order."
        ),
        expected_output="A PlannerOutput with the ordered specialist list and your reasoning.",
        agent=planner_agent,
        output_pydantic=PlannerOutput
    )

    planning_crew = Crew(
        agents=[planner_agent],
        tasks=[planning_task],
        process=Process.sequential,
        verbose=False  # Quiet — the planner runs in the background before the main UI logs start.
    )

    result = planning_crew.kickoff()
    # In recent CrewAI versions, .pydantic gives us the parsed PlannerOutput directly.
    return result.pydantic
