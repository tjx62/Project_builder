# ProjectBuilder — Claude Code Brief

This document gives Claude Code full context on the ProjectBuilder application — what it does, how it's structured, every architectural decision made, known issues, and exactly where development stands.

---

## What This App Does

ProjectBuilder is a Streamlit web app that takes a plain-English coding or infrastructure request, dynamically selects the right AI specialist agents to handle it, runs them through a sequential CrewAI pipeline, and writes the output to disk with a git commit.

Example prompt: "Create an S3 bucket for storing application logs with FedRAMP-compliant encryption."

The app picks the relevant specialists (e.g. S3, IAM), runs them through an (architect →) specialists → auditor → git pipeline, and produces committed Terraform or code files in the project workspace. The architect step is skipped for simple requests, and the git step is pure Python (no agent).

---

## Tech Stack

- **Python 3.11** (must be 3.11 — 3.12 breaks pydantic v1/langsmith compatibility with CrewAI)
- **Streamlit** — UI framework
- **CrewAI** — multi-agent orchestration (`crewai[anthropic]` extra required)
- **Anthropic API** — LLM provider (direct, not via Bedrock currently)
- **Poetry** — dependency management
- **python-dotenv** — `.env` loading

### Key installed extras
```bash
poetry add "crewai[anthropic]"   # required for native Anthropic provider
poetry add python-dotenv boto3
```

### `.env` file
```
ANTHROPIC_API_KEY=sk-ant-...
MODEL=anthropic/claude-haiku-4-5   # CrewAI's internal default model
CREWAI_LLM_PROVIDER=anthropic
OPENAI_API_KEY=not-used            # Suppresses CrewAI's OpenAI fallback
CREWAI_TELEMETRY_OPT_OUT=true
PROJECT_WORKSPACE_DIR=.

# AWS Bedrock (not active yet — waiting on billing resolution)
# AWS_BEARER_TOKEN_BEDROCK=...
# AWS_DEFAULT_REGION=us-east-1
```

---

## File Structure

```
app.py            — Streamlit UI + pipeline orchestration
planner.py        — Lightweight planning agent that picks specialists
specialists.py    — Registry of all specialist agents + descriptions
agents.py         — Supporting agents (architect, auditor, remediation engineer; legacy manager/git/assembler kept but unused)
tasks.py          — Dynamic task builder (full pipeline + targeted remediation)
tools.py          — File write/read/list, git commit, pure-Python variables.tf/outputs.tf generation, resource manifest, terraform validate
CLAUDE.md         — This file
```

---

## Architecture

### Three-Phase UI Flow

**Phase 1 — Input**
User types a project request and clicks "Plan Specialists". The planner agent (Haiku) reads the request and returns a `PlannerOutput` Pydantic model containing an ordered list of specialist IDs and reasoning.

**Phase 2 — Confirm**
The UI shows the planner's proposed specialist list with reasoning. The user can add or remove specialists from a multiselect. Order matters — foundation specialists (VPC) should come before dependents (EC2).

**Phase 3 — Execute**
The crew is assembled dynamically based on confirmed specialists, then kicked off in a background thread. The main thread drains a `queue.Queue` to update the UI.

### Pipeline Order
```
(Architect →) [Specialist 1] → [Specialist 2] → ... → (Auditor →) Wiring Review → Git (pure Python)
```
The **architect is optional**: it only runs when the request has `ARCHITECT_MIN_SPECIALISTS` (3) or more specialists. For 1–2 specialist requests it's skipped and the specialists work straight from the request, saving a full design-brief generation. There is **no Terraform Assembler agent** — `outputs.tf` and `variables.tf` are generated in pure Python at commit time.

### Process Mode
`Process.sequential`. Each task runs exactly once in the order above; there is no manager and no agent-to-agent delegation. The auditor is the last LLM reasoning step — it reviews specialist output, applies compliance fixes, and emits `## Audit Notes` followed by `### File:` blocks **for only the files it changed** (not the whole set). A pure-Python step (`tools.commit_audited_output`) then collects the specialists' files, lets the auditor's changed files overwrite, regenerates `variables.tf`/`outputs.tf` from the on-disk resource files, and runs a single `git commit` with the audit notes in the body. This replaced an earlier hierarchical mode + git_committer agent that could spend 40+ minutes on a simple two-specialist pipeline.

### Auto-Iterate (incremental remediation)
When the user enables auto-iterate, the pipeline loops generate → audit → fix until the report auditor reports compliance or `max_rounds` is hit:
- **Round 1** is a full pass (architect? → specialists → inline auditor).
- **Rounds 2+** are *targeted*: a single **remediation engineer** agent is fed only the previous round's findings plus the current content of the files those findings name (`tasks.build_remediation_tasks`), and rewrites only those files — instead of regenerating the whole design every round.
- The commit step is given an `allowed_files` set in remediation rounds, so any extra file the fixer regenerated but wasn't asked to touch is discarded (never overwrites an unaffected file).
- If a round's findings don't map to specific files, it falls back to a full pass.
- `variables.tf`/`outputs.tf` are excluded from the fixer's scope (regenerated in Python).

### Integration check — `terraform validate` (`tools.terraform_validate`)
The compliance auditor checks *compliance*, not *functional wiring* (does an IAM policy actually reference a bucket that exists?). Each auto-iterate round runs `terraform validate` after committing; any errors are rendered as findings (`render_validation_findings`, with `**File:**` lines the fixer's `parse_finding_files` picks up) and folded into the audit text, so the next round's fixer repairs broken references alongside compliance gaps. Convergence requires **both** a clean compliance audit and a clean validate. It uses `-backend=false` (never touches cloud state/credentials) and degrades gracefully: if there are no `.tf` files, terraform isn't installed, or init fails, it skips without blocking. The normal single-pass mode runs it informationally (no fix loop) and surfaces errors as a warning.

### Cross-resource coordination (`tasks.py`, `agents.py`)
To reduce broken wiring at generation time: when an architect runs (3+ specialists) it's given a **coordination mandate** — name the exact resource addresses and the glue resources (IAM roles/policies, SG rules) each specialist must create, and assign ownership of shared data sources (`aws_caller_identity`, `aws_region`, etc.) to prevent duplicates. Specialists get a **resource manifest** (`tools.build_resource_manifest` — a compact list of existing `type.name` addresses from the workspace) plus wiring and data-source instructions.

The **wiring reviewer** (`agents.wiring_reviewer`) is the deterministic net for whatever slips through: it runs last on every full-generation pass, checks every cross-resource value (ARN, ID, name, endpoint, etc.) and replaces hardcoded strings or reconstructed interpolations with direct Terraform attribute references. It emits only the files it changed and has full context of all prior task outputs. `terraform validate` (above) then provides the final structural gate.

### Model Tiers
| Agent | Model | Reason |
|---|---|---|
| Planner | Haiku | Classification only |
| Architect (optional) | Opus | High-stakes design; skipped for <3 specialists |
| Specialists | Sonnet | Implementation + instruction-following fidelity |
| Compliance auditor (inline + report) | Opus | Compliance reasoning is the highest-stakes step |
| Wiring reviewer | Sonnet | Cross-resource reference correction (implementation) |
| Remediation engineer | Sonnet | Targeted fixes in auto-iterate rounds 2+ |
| Git / outputs.tf / variables.tf | — | Pure Python — no LLM |

> **Note:** The `manager()`, `git_specialist()`, and `terraform_assembler()` factories in `agents.py` are kept around in case you want to revert, but nothing currently calls them.

### Specialist Registry (`specialists.py`)
A `SPECIALISTS` dict maps short IDs to agent factory functions:
```python
SPECIALISTS = {
    'vpc': vpc_specialist,
    'ec2': ec2_specialist,
    'rds': rds_specialist,
    's3': s3_specialist,
    'iam': iam_specialist,
    'lambda': lambda_specialist,
    'python': python_specialist,
    'javascript': javascript_specialist,
    'go': go_specialist,
}
```
To add a new specialist: write a factory function, add it to `SPECIALISTS`, add a one-line description to `SPECIALIST_DESCRIPTIONS`. No other files need to change.

### Context Chaining (`tasks.py`)
Each specialist only receives the architect's output (when present) plus its immediate predecessor's output — not the full accumulated chain. This prevents quadratic token growth across many specialists. When the architect is skipped, the first specialist works directly from the request.

```
Specialist 1 context: [architect]              (or [] when architect skipped)
Specialist 2 context: [architect, specialist_1]
Specialist 3 context: [architect, specialist_2]   ← NOT specialist_1
Auditor context:      [all prior tasks]
```

The auditor and wiring reviewer both receive all prior tasks. The wiring reviewer always runs last (after the auditor if present) so it sees the fully-corrected config. The git step is pure Python and reads task outputs directly, not via a context list.

### File Output Format
Specialists and the auditor label each emitted file as:
```
### File: main.tf
\`\`\`hcl
<content>
\`\`\`
```
The auditor/fixer emits only the files it **changed**. A pure-Python step (`tools.commit_audited_output`) parses every task's `### File:` blocks, applies the precedence rules, writes the files, regenerates `variables.tf`/`outputs.tf`, and commits. It also guards against truncation: a `### File:` block whose fenced code never closed (response hit `max_tokens`) is skipped rather than written over a complete file, and the skip is surfaced in the return value. The workspace directory is set via `PROJECT_WORKSPACE_DIR` env var.

---

## Token / Time Optimisation (applied)

Output tokens dominate cost (Haiku output $5/M vs input $1/M), so the biggest wins target how much the agents *emit*:

- **Auditor emits only changed files** — it no longer re-emits every file (which paid output-token cost twice per round). Unchanged specialist files flow through the Python commit step untouched.
- **Architect skipped for simple requests** (<3 specialists) — avoids a full design-brief generation when it isn't needed. The wiring reviewer still runs on simple requests.
- **Incremental auto-iterate** — rounds 2+ rewrite only the files the findings name (single remediation agent) instead of regenerating the whole design.
- **`outputs.tf` + `variables.tf` generated in pure Python** (`tools.generate_outputs` / `generate_variables`) — deletes the LLM-backed Terraform Assembler agent entirely.
- `memory=False` on Crew — no cross-session memory embedding
- `max_iter=3` on architect, specialists, and the remediation engineer; `4` on the auditor — bounds the ReAct loop tightly
- `max_tokens=8192` on the Haiku agents (the architect's `expected_output` is also capped to keep its brief short)
- `respect_context_window=True` on Crew — auto-trims context approaching model limit
- Sequential process — no manager overhead, no unbounded feedback loops
- Per-run cost and cache-hit stats render in a summary panel after each pipeline finishes

> **On prompt caching:** CrewAI auto-marks the system + initial-user prompt with `cache_breakpoint`, and org context is injected into each agent's backstory so it rides the cached system prefix. In practice cross-round cache **reads stay near zero** for this workload — see the caching note below.

---

## Known Issues

### 1. Agent activity cards (RESOLVED in v1.14.6)

**How it works:** `crewai.events.event_bus.crewai_event_bus` with `AgentExecutionStartedEvent` / `AgentExecutionCompletedEvent`. Both events carry `event.agent` (the actual agent object), so we map `agent.role` → pipeline index via `role_to_idx`. Handlers put `__ACTIVE__` / `__DONE__` sentinels in the queue; the main thread reads them and calls `render_cards()`.

**Registration pattern:**
```python
crewai_event_bus.register_handler(AgentExecutionStartedEvent, on_agent_started)
crewai_event_bus.register_handler(AgentExecutionCompletedEvent, on_agent_completed)
# ...crew.kickoff()...
# In finally block:
crewai_event_bus.off(AgentExecutionStartedEvent, on_agent_started)
crewai_event_bus.off(AgentExecutionCompletedEvent, on_agent_completed)
```

**Why the previous approaches failed:**
- `sys.stdout` redirect: Rich captures the original terminal fd at init time, not `sys.stdout`.
- `logging.Handler`: CrewAI's verbose output goes through Rich, not Python logging.
- `task_callback`: In hierarchical mode always fires with manager role only.
- `step_callback`: LangChain-era feature; not called by the native Anthropic provider.

### 2. AWS Bedrock access (BLOCKED externally)

Bedrock access is configured but blocked due to an AWS billing issue. The app is currently running against the Anthropic API directly. When Bedrock is unblocked:

- Update `.env` to use `AWS_BEARER_TOKEN_BEDROCK` and `AWS_DEFAULT_REGION`
- Update model strings from `anthropic/claude-haiku-4-5` to `bedrock/us.anthropic.claude-haiku-4-5` etc.
- The `AWS_CONFIG_FILE=~/.aws/config-bedrock` approach was chosen to avoid touching the existing `~/.aws/config`

---

## Session State (Streamlit)

These keys are used across the three-phase flow. `reset()` clears all of them:

```python
st.session_state.phase             # 'input' | 'confirm' | 'execute'
st.session_state.plan              # PlannerOutput Pydantic object
st.session_state.project_request   # str
st.session_state.context_text      # str (from uploaded file)
st.session_state.confirmed_ids     # list[str] specialist IDs
st.session_state.pipeline_done     # bool — prevents re-running on rerender
st.session_state.pipeline_result   # str | None
st.session_state.pipeline_error    # str | None
```

The `pipeline_done` flag is critical. Without it, Streamlit rerenders trigger the execute block again, starting a new crew thread on top of the running one.

---

## Manual Package Patch — 1-Hour Prompt Cache TTL

Two changes to `crewai/llms/providers/anthropic/completion.py`:

**1. Cache TTL in the three `cache_control` blocks (search for `ephemeral`):**
```python
# before
{"type": "ephemeral"}
# after
{"type": "ephemeral", "ttl": "1h"}
```

**2. Beta header in `_get_client_params` (after the `client_params.update` block):**
```python
headers = dict(client_params.get("default_headers") or {})
existing_beta = headers.get("anthropic-beta", "")
if "extended-cache-ttl-2025-04-11" not in existing_beta:
    headers["anthropic-beta"] = (
        f"{existing_beta},extended-cache-ttl-2025-04-11"
        if existing_beta else "extended-cache-ttl-2025-04-11"
    )
client_params["default_headers"] = headers
```

The `ttl: "1h"` field is silently ignored without the `extended-cache-ttl-2025-04-11` beta header.

**Re-apply after `poetry update`:**
```bash
PROVIDER=$(poetry env info --path)/lib/python3.12/site-packages/crewai/llms/providers/anthropic/completion.py
sed -i 's/{"type": "ephemeral"}/{"type": "ephemeral", "ttl": "1h"}/g' "$PROVIDER"
```
Then manually re-add the `_get_client_params` beta header block (see above).

### Caching finding — why cross-round reads stay ~0 (investigated)

Live instrumentation showed cache **writes** happening every round but **reads ≈ 0**, even with the 1h TTL patch active. Root cause is **not** TTL expiry — it's the model's minimum cacheable prefix length:

- `claude-haiku-4-5` requires a prefix of **~2,048 tokens** to create a cacheable segment (the small-Haiku tier; Sonnet/Opus are 1,024). Empirically confirmed: an architect call with a ~1,660-token total prompt wrote **zero** cache.
- The only thing byte-identical across rounds is each agent's **system prompt** (~1,500 tokens) — below the 2,048 floor, so it never gets its own cache entry.
- The only blocks large enough to cache (system + user message with prior outputs) include per-round-regenerated content, so they never match on a later round.

**Implication:** cross-round caching is structurally marginal for this workload — the stable content is too small to cache and the cacheable content isn't stable. The 1h TTL patch is therefore largely inert here; it would only matter once a large (>2,048-token) byte-stable prefix recurs (e.g. a big org-context doc pushed into every system prompt, or a frozen baseline in incremental remediation). The real cost lever is **output tokens**, addressed by the optimisations above, not caching.

## Running the App

```bash
# First time setup
pyenv install 3.11.14
pyenv local 3.11.14
poetry env use python3.11
poetry install
poetry add "crewai[anthropic]" python-dotenv boto3

# Every run
poetry run streamlit run app.py
```

The project workspace directory must be a git repo for the pure-Python commit step to work:
```bash
git init
```

---

## What to Work On Next

1. **Bedrock switch** — straightforward once billing is resolved. One `.env` change and model string updates.
2. **Model tier upgrades** — when ready to move off all-Haiku testing, bump specialists to Sonnet and auditor to Opus. Each is a one-line change in `app.py` Phase 3. (A stronger model that converges in fewer auto-iterate rounds can be cheaper end-to-end than all-Haiku looping.)
3. **Additional specialists** — add to `specialists.py` following the existing pattern. Set its `SPECIALIST_PRODUCES_TERRAFORM` flag; no other files need changing.
4. **Remediation prompt adherence** — the fixer occasionally emits files it wasn't asked to touch; the `allowed_files` commit filter discards them, but tightening the prompt could save tokens.
5. **`generate_outputs` coverage** — it emits `id` only for AWS resource types not in `_OUTPUT_ATTRS`; extend the map for richer outputs on more resource types.
6. **Requirements/Synthesis refactor** — see the full design in "Proposed Architecture" below. High-value planned change; implement in the phased order listed there.

---

## Proposed Architecture: Requirements/Synthesis Split (NOT YET IMPLEMENTED)

Planned refactor to separate "what to build" (AWS domain expertise) from "how to express
it in IaC" (Terraform authoring). Capture of a design discussion — implement in phases,
do not attempt all at once.

### The Core Idea

Currently each AWS specialist does double duty: domain expert AND Terraform author. This
causes inconsistent HCL between specialists (different naming, structure, tagging, module
conventions). The fix is to split these responsibilities cleanly.

**New flow:**
```
Architect (optional, 3+ specialists)
  → AWS Specialists   (output REQUIREMENT SPECS, not HCL)
  → Terraform Specialist  (consumes all specs, authors all .tf files)
  → Verification Loop (specialists verify their requirements are met in the HCL)
  → Compliance Auditor
  → Wiring Reviewer
  → Pure-Python commit
```

### Why This Is Better

1. **Consistency** — one IaC author means uniform naming, tagging, module structure,
   and provider config across all resources. Removes a whole class of cross-specialist
   HCL inconsistencies that the wiring reviewer currently has to clean up.
2. **Swappable IaC layer** — specialists output requirements, not HCL. Swap the Terraform
   specialist for CloudFormation, Pulumi, or CDK without touching any AWS domain specialist.
3. **Simpler specialists** — an IAM specialist outputting "I need a role with these 3
   permissions scoped to this resource" is more reliable than one writing correct HCL
   for it, reducing the burden on the wiring reviewer.

### Specialist Output Format Change

Specialists stop outputting `### File:` code blocks. Instead they output structured
requirement specs:

```
### Resource: S3 Bucket
- Name: app-logs-bucket
- Versioning: enabled
- Encryption: SSE-KMS with customer-managed key
- Lifecycle: transition to Glacier after 90 days
- Access logging: enabled, target = audit-logs-bucket
- Required IAM permissions: s3:GetObject, s3:PutObject scoped to this bucket ARN
```

The Terraform specialist consumes ALL specs and produces one coherent set of `.tf` files
using the `### File:` format the existing commit step already expects. No change needed
to the commit logic.

### Verification Loop

Because each AWS specialist defined its own requirements, it is the right agent to verify
the Terraform specialist actually satisfied them. This mirrors the existing auditor
kick-back pattern but applied one layer earlier and with a hard iteration cap.

**Loop design:**
```
Terraform Specialist drafts HCL
  → Each AWS Specialist verifies its own requirements are met in the HCL
     → PASS: proceed to Auditor
     → FAIL: return specific findings to Terraform Specialist for targeted fixes
  → Repeat up to MAX_VERIFY_ROUNDS (suggested: 2-3)
  → If still failing after cap: surface findings to user and proceed anyway
```

**Key implementation notes:**
- Each specialist must verify against its **own original spec** (not re-derived from
  scratch) — pass the spec back as context during verification. Otherwise specialists
  can drift and reject valid Terraform for the wrong reasons.
- The verification loop has its own token cost: each cycle is another Terraform author
  + N specialist verifier LLM calls. The `max_iter` caps on agents help but the loop
  itself needs a hard ceiling.
- The existing `terraform validate` step still provides the final structural gate after
  the loop. Both clean verification AND clean validate required for convergence.

### How This Relates to Existing Code

The wiring reviewer (`agents.wiring_reviewer`) already handles cross-resource reference
correction post-generation. With this refactor, wiring issues should be rarer because
one author produces the whole HCL. The wiring reviewer stays as a safety net but should
have less to do.

The auto-iterate loop (remediation engineer) stays unchanged — it handles compliance
issues, not IaC authoring consistency.

### Two-Crew Parallelization (FUTURE — lower priority)

Discussed but deliberately deferred. The idea: one crew of AWS specialists drafting
requirements in parallel, a second crew of Terraform specialists parallelizing the
authoring by resource group.

**Why deferred:**
- Adds a second manager agent, significantly increasing orchestration cost.
- Terraform resources often have dependencies (EC2 module references VPC subnet IDs),
  so naive parallelization produces code that doesn't wire together. Needs
  dependency-aware task splitting, which is non-trivial.
- Should not be attempted until the single-pipeline requirements/synthesis version is
  stable and well-tested.

**Middle ground if throughput becomes an issue:** keep one crew but treat it as two
clear internal phases (requirements gathering, then synthesis) rather than spinning up
a second crew.

### Recommended Implementation Order

1. Update `specialists.py` — add a `produces_terraform` flag (already exists as
   `SPECIALIST_PRODUCES_TERRAFORM`); set it to `False` for all AWS specialists.
   Update specialist goals/backstories to output requirement specs instead of HCL.
2. Add a `terraform_specialist` factory to `specialists.py` and register it in
   `SPECIALISTS`. This agent's job is to read all requirement specs and author
   one coherent set of `.tf` files.
3. Update `tasks.py` — add the Terraform specialist task after all AWS specialist
   tasks, before the auditor.
4. Add the verification loop in `tasks.py` with a `MAX_VERIFY_ROUNDS` cap.
5. Verify that the existing wiring reviewer and `terraform validate` steps still work
   correctly with the new flow (they should — the `### File:` format is unchanged).
6. Test on simple single-specialist requests first, then multi-specialist.
7. Only consider two-crew parallelization after this is stable.
