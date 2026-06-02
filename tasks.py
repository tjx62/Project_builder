"""
Dynamic task builder.

Instead of hard-coded tasks, build_tasks() generates an ordered list of Tasks
based on which specialists were selected. Each specialist's task gets the
prior tasks as context, so outputs flow downstream in handoff order.
"""

from crewai import Task
from tools import build_resource_manifest


def build_wiring_review_task(reviewer, prior_tasks: list) -> Task:
    """Single task for the wiring reviewer: fix hardcoded cross-resource references.

    Runs after all specialists (and the compliance auditor if present) so the
    reviewer sees the complete config. It receives all prior task outputs as
    context and emits only the files it changed.
    """
    return Task(
        description=(
            "Review every Terraform file produced by the specialists and auditor.\n\n"
            "For each resource attribute that references another resource in this config "
            "(ARN, ID, name, endpoint, security-group ID, subnet ID, KMS key ARN, etc.), "
            "check whether it uses a direct Terraform attribute reference "
            "(resource_type.resource_name.attribute) or a hardcoded/reconstructed value.\n\n"
            "Fix every hardcoded or reconstructed value by replacing it with the correct "
            "Terraform attribute reference. Examples:\n"
            "  BAD:  Resource = \"arn:aws:s3:::my-bucket/*\"\n"
            "  GOOD: Resource = \"${aws_s3_bucket.my_bucket.arn}/*\"\n\n"
            "  BAD:  bucket = \"my-bucket-${data.aws_caller_identity.current.account_id}\"\n"
            "  GOOD: bucket = aws_s3_bucket.my_bucket.id\n\n"
            "Do NOT change values that are genuinely external to this config (resources in "
            "other accounts, pre-existing infrastructure, correctly-scoped variables). "
            "Do NOT change compliance controls or IAM policy structure.\n\n"
            "Output a '## Wiring Review' section (one bullet per fix, or 'No wiring issues "
            "found.' if nothing changed), then ONLY the files you changed as "
            "'### File: <filename>' fenced blocks."
        ),
        expected_output=(
            "A '## Wiring Review' section followed by only the files that were changed, "
            "each as '### File: <filename>' with a fenced code block. No unchanged files."
        ),
        agent=reviewer,
        context=list(prior_tasks),
    )


def build_tasks(architect, specialist_agents, auditor, project_request,
                compliance_framework: str | None = None, key_controls: str = "",
                existing_context: str | None = None,
                audit_only: bool = False,
                wiring_reviewer=None):
    """Build the full ordered task list for sequential execution.

    Args:
        architect:        The high-level architect Agent, or None to skip the
                          design phase (used for simple 1–2 specialist requests).
        specialist_agents: Ordered list of (specialist_id, Agent) tuples.
        auditor:          The compliance auditor Agent (runs last; produces the deliverable).
        project_request:  The user's original project description.

    Returns:
        A list of Task objects ready to pass to Crew(tasks=...).

    Note: organizational context is injected into each agent's backstory at
    construction time (see agents.py / specialists.py), so it rides in the
    cached system prompt rather than the volatile task description.

    The auditor is the last LLM task; its output (with '### File:' blocks and
    '## Audit Notes') is parsed by tools.commit_audited_output and committed
    by pure Python — no git_committer agent is needed anymore.
    """

    # --- Audit-only path: skip architect/specialists/assembler entirely ---
    if audit_only:
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        focus = f"\n\nAdditional auditor focus: {project_request}" if project_request.strip() else ""
        audit_task = Task(
            description=(
                f"You are performing a standalone compliance audit of existing infrastructure files.\n\n"
                f"{existing_context or 'No files provided — state that the workspace is empty.'}\n\n"
                f"Framework: {compliance_framework or 'General security best practices'}\n"
                f"{controls_line}{focus}\n\n"
                "Review every file above. For each compliance gap produce a finding entry. "
                "Use the structured format defined in your goal. "
                "If no issues are found write a summary stating all controls are satisfied."
            ),
            expected_output=(
                f"A structured markdown audit report titled '# Audit Report — {compliance_framework}' "
                "with a summary line and numbered findings (severity, file, requirement, "
                "current state, required fix). No code blocks."
            ),
            agent=auditor,
        )
        return [audit_task]

    # --- 1. Architect designs the overall solution first (optional) ---
    # Skipped for simple requests (architect is None); the specialists then work
    # directly from the project request, saving a full design-brief generation.
    # Manifest of resources that already exist on disk (incremental / extend runs).
    # Injected into specialist prompts so they reference real addresses.
    existing_manifest = build_resource_manifest(existing_context or "")

    # Coordination mandate: when an architect runs (3+ specialists), it is the
    # single point that sees the whole design, so make it pin down the exact
    # cross-tier wiring the specialists must implement.
    coordination_mandate = (
        "\n\nCOORDINATION (critical): for each component, name the EXACT Terraform "
        "resource addresses (e.g. `aws_s3_bucket.app_logs`) that other tiers will "
        "reference, and explicitly list every glue resource one component needs to "
        "access another — IAM roles/policies, instance profiles, security-group rules, "
        "etc. — and which specialist owns it. Specialists implement exactly these "
        "addresses; do not leave cross-resource access unspecified.\n"
        "DATA SOURCE OWNERSHIP: also specify which specialist owns each shared Terraform "
        "data source (e.g. `data.aws_caller_identity.current`, `data.aws_region.current`, "
        "`data.aws_availability_zones.available`). Only that specialist emits the data "
        "block; all others reference it as `data.<type>.<name>` without re-declaring it."
    )

    all_tasks = []
    prior_tasks = []
    architect_task = None
    if architect is not None:
        if existing_context:
            architect_description = (
                f"Project request:\n{project_request}\n\n"
                f"{existing_context}\n\n"
                "Produce a design brief that EXTENDS the existing infrastructure to satisfy "
                "the project request. Preserve every existing resource — only add or modify "
                "what the request requires. If audit findings are present, address each one "
                "as a hard requirement. Include what changes, what stays the same, and why."
                + coordination_mandate
            )
        else:
            architect_description = (
                f"Project request:\n{project_request}\n\n"
                "Produce a clear high-level design brief that specialists can implement. "
                "Include the components needed, how they connect, and any constraints."
                + coordination_mandate
            )

        architect_task = Task(
            description=architect_description,
            expected_output=(
                "A concise high-level architecture brief (no code) covering components, "
                "data flow, constraints, and the exact resource addresses + glue resources "
                "each specialist must create. Keep it under ~450 words."
            ),
            agent=architect
        )
        all_tasks.append(architect_task)
        prior_tasks.append(architect_task)

    # --- 2. Each specialist runs in order ---
    # Each specialist only receives the architect's output plus its immediate
    # predecessor's output — not the full chain. This keeps context lean while
    # still allowing each specialist to build on the prior one's work.
    last_specialist_task = None
    for specialist_id, specialist in specialist_agents:
        specialist_context = [t for t in (architect_task, last_specialist_task) if t is not None]

        manifest_block = (
            f"\n\nResources already defined in this project — reference these by their EXACT "
            f"address and do NOT redefine them:\n{existing_manifest}\n"
            if existing_manifest else ""
        )

        specialist_task = Task(
            description=(
                f"You are the {specialist.role}. Implement your specific piece of the project.\n"
                f"Original request: {project_request}\n"
                f"{manifest_block}\n"
                "Review the architect's brief and any prior specialist output in your context, "
                "then produce your specific contribution. Build on prior outputs rather than redesigning them.\n\n"
                "WIRING: If your component must access or be accessed by another resource, create the "
                "necessary glue (IAM roles/policies, security-group rules, etc.) and reference other "
                "resources by their EXACT address (as given in the design brief or the manifest above) — "
                "never invent or guess resource names.\n\n"
                "IMPORTANT: Be concise and structured. Output ONLY the essential code and configuration — "
                "no lengthy explanations, no preamble, no commentary. "
                "Label each file clearly as:\n"
                "### File: <filename>\n```<language>\n<code>\n```\n"
                "If your output is Terraform, use ```hcl. If Python, use ```python. Etc.\n"
                f"NAMING: If writing Terraform, name your primary file `{specialist_id}.tf` "
                f"(not `main.tf`) so each specialist's output stays separate and nothing is overwritten.\n"
                "Do NOT emit variables.tf or outputs.tf — those are generated automatically "
                "from your resource files. Only emit your resource file.\n"
                "SHARED DATA SOURCES: if you need data sources that other specialists also "
                "use (e.g. aws_caller_identity, aws_region, aws_availability_zones), define "
                "them ONLY if you are certain no other specialist in this project will also "
                "define them. Prefer referencing them via a variable or output instead of "
                "repeating the data block. When in doubt, leave them out — the architect's "
                "brief will indicate which specialist owns each shared data source."
            ),
            expected_output=(
                f"Concise {specialist_id} implementation with each file labelled as "
                "'### File: <filename>' followed by its code in a fenced block. No prose explanations."
            ),
            agent=specialist,
            context=specialist_context
        )
        all_tasks.append(specialist_task)
        prior_tasks.append(specialist_task)
        last_specialist_task = specialist_task

    # NOTE: outputs.tf and variables.tf are generated in pure Python from the
    # resource files at commit time (see tools.generate_outputs /
    # tools.generate_variables) — there is no Terraform Assembler agent.

    # --- 3. Auditor (optional) ---
    # Skipped entirely when compliance_framework is None. In that case the last
    # specialist's output is the deliverable; the Python commit step collects
    # ### File: blocks from all task outputs.
    if compliance_framework and auditor is not None:
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        audit_task = Task(
            description=(
                f"Review the architect's design and every specialist's output for "
                f"{compliance_framework} compliance and apply any necessary fixes.\n\n"
                f"{controls_line}\n\n"
                "Procedure:\n"
                "1. Scan all prior outputs for compliance gaps.\n"
                "2. For every issue you find, fix it directly in the relevant file.\n"
                "3. Output a brief '## Audit Notes' section first, listing each issue and "
                "the fix you applied (one bullet per change). If nothing needed fixing, "
                "write 'No compliance issues found.'\n"
                "4. Then output ONLY the files you actually changed, in this exact format:\n\n"
                "### File: <filename>\n```hcl\n<content>\n```\n\n"
                "Do NOT re-output files you did not change — unchanged specialist files are "
                "preserved automatically. If you changed nothing, output only the Audit Notes "
                "section and no '### File:' blocks. Never produce a report-only narrative."
            ),
            expected_output=(
                "A response beginning with '## Audit Notes', followed by only the files you "
                "changed, each labelled '### File: <filename>' with its corrected content in a "
                "fenced code block. No unchanged files."
            ),
            agent=auditor,
            context=list(prior_tasks),
        )
        all_tasks.append(audit_task)
        prior_tasks.append(audit_task)

    # --- 4. Wiring reviewer (optional) ---
    # Runs last, after the auditor (or after specialists if no auditor), so it
    # sees the final corrected config. Checks that every cross-resource reference
    # uses a Terraform attribute reference, not a hardcoded/reconstructed value.
    if wiring_reviewer is not None:
        wiring_task = build_wiring_review_task(wiring_reviewer, prior_tasks)
        all_tasks.append(wiring_task)

    return all_tasks


def build_remediation_tasks(fixer, project_request, compliance_framework,
                            key_controls, findings_text, affected_context):
    """Build a single targeted remediation task for auto-iterate rounds 2+.

    Unlike build_tasks(), this runs no architect and no specialists. It feeds the
    fixer agent only the audit findings plus the current content of the files
    those findings name, and asks for corrected versions of ONLY those files —
    so later rounds stop regenerating the whole design every time.

    Args:
        fixer:               The remediation agent (see agents.remediation_engineer).
        findings_text:       The full audit report from the previous round.
        affected_context:    ### File: blocks for only the files the findings name.
    """
    controls_line = (f"Key controls to enforce: {key_controls}."
                     if key_controls else "Apply generally accepted security best practices.")
    remediation_task = Task(
        description=(
            f"Remediate {compliance_framework} audit findings on existing files.\n\n"
            f"Original project request (context only — do NOT expand scope):\n{project_request}\n\n"
            f"## Findings to fix\n{findings_text}\n\n"
            f"## Current content of the files named in those findings\n{affected_context}\n\n"
            f"{controls_line}\n\n"
            "Apply ONLY the fixes required to resolve the findings above. Do not redesign, "
            "do not add resources the findings don't ask for, and do not touch files that "
            "have no findings.\n\n"
            "Output a brief '## Audit Notes' section listing each finding and the fix you "
            "applied, then output ONLY the files you changed, each as:\n"
            "### File: <filename>\n```<language>\n<content>\n```\n"
            "Emit a file ONLY if you changed it."
        ),
        expected_output=(
            "A '## Audit Notes' section followed by only the changed files, each labelled "
            "'### File: <filename>' with a fenced code block."
        ),
        agent=fixer,
    )
    return [remediation_task]
