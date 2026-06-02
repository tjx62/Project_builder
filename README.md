# ProjectBuilder

A Streamlit web app that takes a plain-English coding or infrastructure request, selects the right AI specialist agents to handle it, runs them through a sequential multi-agent pipeline, and commits the output to disk.

---

## What it does

1. **Plan** — A lightweight planner agent reads your request and proposes an ordered list of specialists (e.g. VPC → S3 → IAM).
2. **Confirm** — You review and adjust the proposed list before anything runs.
3. **Execute** — The pipeline runs:
   - *(optional)* An architect produces a design brief (triggered at 3+ specialists)
   - Each specialist implements its piece of the project
   - *(optional)* A compliance auditor applies fixes for the selected framework
   - A wiring reviewer checks that all cross-resource references use Terraform attribute references rather than hardcoded strings
   - `variables.tf` and `outputs.tf` are generated in pure Python and committed

In **auto-iterate** mode, an additional report auditor and `terraform validate` gate run after each commit. Non-compliant or structurally invalid output triggers a targeted remediation pass that rewrites only the files the findings name, then re-audits — until clean or `max_rounds` is hit.

---

## Tech stack

- **Python 3.12**, **Poetry**
- **Streamlit** — UI
- **CrewAI** (`crewai[anthropic]`) — multi-agent orchestration
- **Anthropic API** — LLM provider
- **Terraform** — required on `PATH` for the `terraform validate` gate

---

## Setup

```bash
# Install dependencies
poetry install
poetry add "crewai[anthropic]" python-dotenv boto3

# The workspace must be a git repo for commits to work
git init
```

Create a `.env` file:

```
ANTHROPIC_API_KEY=sk-ant-...
MODEL=anthropic/claude-haiku-4-5
CREWAI_LLM_PROVIDER=anthropic
OPENAI_API_KEY=not-used
CREWAI_TELEMETRY_OPT_OUT=true
PROJECT_WORKSPACE_DIR=.
```

```bash
# Run the app
poetry run streamlit run app.py
```

---

## Model tiers

| Agent | Model |
|---|---|
| Planner | Haiku |
| Architect *(optional, 3+ specialists)* | Opus |
| Specialists | Sonnet |
| Compliance auditor | Opus |
| Wiring reviewer | Sonnet |
| Remediation engineer *(auto-iterate rounds 2+)* | Sonnet |
| `variables.tf` / `outputs.tf` / git | Pure Python |

---

## Available specialists

| ID | Scope |
|---|---|
| `vpc` | AWS networking — VPCs, subnets, route tables, NAT gateways |
| `ec2` | AWS compute — EC2, autoscaling, launch templates |
| `rds` | AWS databases — RDS, Aurora |
| `s3` | AWS object storage — buckets, policies, lifecycle |
| `iam` | AWS access control — roles, policies, trust relationships |
| `lambda` | AWS serverless — Lambda functions, event sources |
| `python` | Python application code |
| `javascript` | JavaScript / TypeScript / Node.js |
| `go` | Go services and CLIs |

To add a specialist: add a factory function to `specialists.py`, add it to `SPECIALISTS` and `SPECIALIST_DESCRIPTIONS`, and set its `SPECIALIST_PRODUCES_TERRAFORM` flag. No other files need changing.

---

## Compliance frameworks

FedRAMP Rev 5 High, SOC 2 Type II, HIPAA, PCI DSS v4, NIST 800-53 Moderate — or none.

---

## Uploading organizational context

Use the **Upload Guidelines File** input (`.md` or `.txt`) to inject org-specific standards — naming conventions, tagging requirements, encryption policies, etc. — into every agent's system prompt. This is also how you push system prompts past the ~2,048-token cache threshold for better cross-round prompt-cache utilisation.

---

## Auto-iterate convergence

Each round:
1. Generate (full pass round 1; targeted remediation rounds 2+)
2. Commit files — auditor/fixer emits only changed files; unchanged specialist files are preserved
3. `terraform validate` — duplicate block and reference errors surface as findings
4. Report audit — compliance check against the selected framework
5. If both clean → converged. If not → extract affected files from findings, feed only those to the remediation engineer.

`variables.tf` and `outputs.tf` are excluded from the remediation scope (always regenerated in Python at commit time).

---

## Key files

```
app.py          — Streamlit UI + pipeline orchestration
planner.py      — Planner agent (picks specialists)
specialists.py  — Specialist registry
agents.py       — Architect, auditors, wiring reviewer, remediation engineer
tasks.py        — Task builders for full pipeline, remediation, and wiring review
tools.py        — File I/O, git commit, Terraform codegen, validate gate
```

---

## Manual patch — 1-hour prompt cache TTL

After `poetry update`, re-apply to `crewai/llms/providers/anthropic/completion.py`:

```bash
PROVIDER=$(poetry env info --path)/lib/python3.12/site-packages/crewai/llms/providers/anthropic/completion.py
sed -i 's/{"type": "ephemeral"}/{"type": "ephemeral", "ttl": "1h"}/g' "$PROVIDER"
```

Then manually add the `extended-cache-ttl-2025-04-11` beta header in `_get_client_params` — see `CLAUDE.md` for the exact block.

> **Note:** Cross-round cache reads stay near zero on this workload regardless of TTL. The patch is a low-cost hedge for when a large org-context file pushes system prompts above the ~2,048-token Haiku cache floor. See `CLAUDE.md` for the full analysis.
