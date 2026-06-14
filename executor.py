"""
Thin sequential executor on the raw Anthropic SDK.

Replaces CrewAI's Agent / Task / Crew(sequential).kickoff() with a plain
for-loop over single-shot LLM calls. Every active agent in this app is
single-shot (no tools, no delegation) and the pipeline is strictly sequential,
so this is all the orchestration the app needs.

The result object (PipelineResult) is a drop-in shim for CrewAI's kickoff()
return value: downstream code in tools.py and app.py reads `.raw`,
`.tasks_output` (a list whose items each expose `.raw`), and — for the planner
only — `.pydantic`. See tools.commit_audited_output / tools.write_findings.

Telemetry: run_step emits the same queue sentinels the CrewAI event-bus
handlers used to emit (__ACTIVE__ / __DONE__ / __STEP__ / __USAGE__), so the
existing main-thread drain loop in app.py is unchanged.
"""

import os
from dataclasses import dataclass, field

import anthropic


# ==========================================
# Result shim — satisfies the kickoff() contract
# ==========================================

@dataclass
class StepResult:
    """One LLM call's output. `.raw` is what tools.py / app.py read."""
    raw: str
    usage: dict = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Drop-in replacement for CrewAI's Crew.kickoff() return object.

    - `.tasks_output` — list[StepResult] in pipeline order. tools.commit_audited_output
      relies on this order: texts[:-1] are specialist files (flow through), texts[-1]
      is the auditor/wiring step that overwrites only the files it changed.
    - `.raw` — the LAST step's text (what write_findings / the auto-iterate audit_text
      path read).
    - `.pydantic` — planner only; None for generation/audit pipelines.
    """
    tasks_output: list
    pydantic: object = None

    @property
    def raw(self) -> str:
        return self.tasks_output[-1].raw if self.tasks_output else ""

    def __str__(self) -> str:  # final-display fallback (app.py pipeline_result)
        return self.raw


# ==========================================
# Call spec — one pipeline step
# ==========================================

@dataclass
class AgentSpec:
    """Lightweight replacement for crewai.Agent — a system-prompt template + tier.

    `model` is resolved by app.py (config model id for the agent's tier) before the
    pipeline runs. The `.system` property builds the system prompt from role/goal/
    backstory; org context is already folded into backstory (see agents._with_context
    / specialists._backstory), so it rides the cached system block.
    """
    role: str
    goal: str
    backstory: str
    tier: str = ""          # "haiku" | "sonnet" | "opus"; set by app.py
    model: str = ""         # resolved model id; set by app.py

    @property
    def system(self) -> str:
        return f"You are the {self.role}.\n\nYour goal: {self.goal}\n\n{self.backstory}"


@dataclass
class StepSpec:
    """A single LLM call to make, with its UI card + context dependencies.

    context_indices references earlier steps in the same pipeline whose outputs
    should be concatenated into this step's user message — this reproduces
    CrewAI's `context=[prior_tasks]` chaining without quadratic growth.
    """
    system: str
    user: str
    model: str              # resolved model id, may carry an "anthropic/"/"bedrock/" prefix
    tier: str               # "haiku" | "sonnet" | "opus" — for cost/telemetry bucketing
    card_idx: int | None    # pipeline-card index to light up, or None for no card
    label: str
    context_indices: tuple = ()


# ==========================================
# Client + model helpers
# ==========================================

def build_client(*, is_bedrock: bool, api_key: str | None = None,
                 aws_region: str | None = None):
    """Build an Anthropic (or Bedrock) client.

    For Bedrock, credentials are already injected into os.environ by
    config.inject_credentials_from_config before the run starts; AnthropicBedrock
    reads the standard AWS_* env vars.
    """
    if is_bedrock:
        return anthropic.AnthropicBedrock(
            aws_region=aws_region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        )
    return anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))


def _strip_prefix(model: str) -> str:
    """'anthropic/claude-opus-4-7' -> 'claude-opus-4-7';
    'bedrock/us.anthropic.claude-opus-4-8' -> 'us.anthropic.claude-opus-4-8'."""
    return model.split("/", 1)[1] if "/" in model else model


def _is_opus(model: str) -> bool:
    # Opus 4.7/4.8 reject temperature/top_p (400). Gate sampling params on this.
    return "opus" in model.lower()


# ==========================================
# Execution
# ==========================================

def run_step(client, spec: StepSpec, *, queue, cache: bool, user_override: str | None = None) -> StepResult:
    """Run one LLM call, emitting the queue sentinels the UI drain loop expects."""
    raw_model = _strip_prefix(spec.model)
    user = user_override if user_override is not None else spec.user

    system_block = [{"type": "text", "text": spec.system}]
    if cache:
        # Native prompt caching on the system block. Haiku 4.5's minimum cacheable
        # prefix is ~4096 tokens; shorter systems silently won't cache (no error).
        system_block[0]["cache_control"] = {"type": "ephemeral"}

    kwargs = dict(
        model=raw_model,
        max_tokens=8192,
        system=system_block,
        messages=[{"role": "user", "content": user}],
    )
    if not _is_opus(raw_model):
        kwargs["temperature"] = 0.2

    if spec.card_idx is not None:
        queue.put(f"__ACTIVE__:{spec.card_idx}:{spec.label}")

    resp = client.messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if b.type == "text")

    u = resp.usage
    usage = {
        "tier": spec.tier,
        "input":        getattr(u, "input_tokens", 0) or 0,
        "output":       getattr(u, "output_tokens", 0) or 0,
        "cache_read":   getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_create": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    queue.put(
        f"__USAGE__:{spec.tier}:{usage['input']}:{usage['output']}:"
        f"{usage['cache_read']}:{usage['cache_create']}"
    )
    queue.put("__STEP__")
    if spec.card_idx is not None:
        queue.put(f"__DONE__:{spec.card_idx}:{spec.label}")

    return StepResult(raw=text, usage=usage)


def run_pipeline(client, steps: list[StepSpec], *, queue, cache: bool) -> PipelineResult:
    """Run steps sequentially, chaining context, returning a kickoff()-shaped result.

    The output order matches the steps order — tools.commit_audited_output depends
    on the final step being the auditor/wiring step that overwrites changed files.
    """
    outputs: list[StepResult] = []
    prior_texts: list[str] = []
    for spec in steps:
        if spec.context_indices:
            ctx = "\n\n".join(prior_texts[i] for i in spec.context_indices)
            user = f"{ctx}\n\n---\n\n{spec.user}" if ctx else spec.user
        else:
            user = spec.user
        res = run_step(client, spec, queue=queue, cache=cache, user_override=user)
        outputs.append(res)
        prior_texts.append(res.raw)
    return PipelineResult(tasks_output=outputs)
