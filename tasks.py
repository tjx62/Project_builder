"""
Dynamic task builder.

Returns ordered lists of executor.StepSpec objects ready for run_pipeline().

Each StepSpec carries:
  - system/user:        the full prompt for one LLM call
  - model/tier:         from the AgentSpec
  - card_idx/label:     which UI pipeline card to light up (None = no card)
  - context_indices:    indices into the same list whose .raw outputs are
                        prepended to this step's user message before the call

Context chaining mirrors the old CrewAI Task(context=[...]) chains:
    Specialist 1:  [architect]                          or []
    Specialist N:  [architect, specialist_N-1]
    Terraform:     [architect] + all aws_spec steps
    Verify N:      [aws_spec_step_N, terraform_step]
    Auditor:       all prior steps
    Wiring:        all prior steps (including auditor)

The LAST step in the returned list must always be the auditor or wiring
reviewer — tools.commit_audited_output treats tasks_output[-1] as the step
that overwrites only the files it changed (all others flow through unchanged).
"""

from executor import StepSpec
from tools import build_resource_manifest
from specialists import SPECIALIST_IS_AWS


def _card(agent_spec, role_to_card_idx):
    """Return (card_idx, label) for an agent. Falls back to (None, role)."""
    card_idx = (role_to_card_idx or {}).get(agent_spec.role)
    return card_idx, agent_spec.role


def build_tasks(architect, specialist_agents, auditor, project_request,
                compliance_framework=None, key_controls="",
                existing_context=None, audit_only=False,
                wiring_reviewer=None, terraform_specialist_agent=None,
                role_to_card_idx=None):
    """Build the full ordered StepSpec list for run_pipeline().

    Args:
        architect:                AgentSpec for the architect, or None to skip.
        specialist_agents:        Ordered list of (specialist_id, AgentSpec) tuples.
        auditor:                  AgentSpec for the compliance auditor, or None.
        project_request:          The user's original project description.
        compliance_framework:     Framework name string, or None to skip auditor.
        key_controls:             Extra controls string injected into the auditor.
        existing_context:         ### File: blocks of existing workspace files.
        audit_only:               If True, return only the single audit step.
        wiring_reviewer:          AgentSpec for the wiring reviewer, or None.
        terraform_specialist_agent: AgentSpec for the Terraform IaC specialist, or None.
        role_to_card_idx:         Dict mapping agent.role -> pipeline card index for UI.

    Returns:
        Ordered list[StepSpec]. The final element is always the auditor or
        wiring reviewer (whichever runs last) -- this ordering is load-bearing
        for tools.commit_audited_output.
    """

    # --- Audit-only: single auditor step, no generation ---
    if audit_only:
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        focus = f"\n\nAdditional auditor focus: {project_request}" if project_request.strip() else ""
        card_idx, label = _card(auditor, role_to_card_idx)
        return [StepSpec(
            system=auditor.system,
            user=(
                f"You are performing a standalone compliance audit of existing infrastructure files.\n\n"
                f"{existing_context or 'No files provided — state that the workspace is empty.'}\n\n"
                f"Framework: {compliance_framework or 'General security best practices'}\n"
                f"{controls_line}{focus}\n\n"
                "Review every file above. For each compliance gap produce a finding entry. "
                "Use the structured format defined in your goal. "
                "If no issues are found write a summary stating all controls are satisfied."
            ),
            model=auditor.model,
            tier=auditor.tier,
            card_idx=card_idx,
            label=label,
            context_indices=(),
        )]

    # --- Shared setup ---
    existing_manifest = build_resource_manifest(existing_context or "")

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

    manifest_block = (
        f"\n\nResources already defined in this project — reference these by their EXACT "
        f"address and do NOT redefine them:\n{existing_manifest}\n"
        if existing_manifest else ""
    )

    all_steps = []

    # --- 1. Architect (optional) ---
    architect_idx = None
    if architect is not None:
        if existing_context:
            arch_user = (
                f"Project request:\n{project_request}\n\n"
                f"{existing_context}\n\n"
                "Produce a design brief that EXTENDS the existing infrastructure to satisfy "
                "the project request. Preserve every existing resource — only add or modify "
                "what the request requires. If audit findings are present, address each one "
                "as a hard requirement. Include what changes, what stays the same, and why."
                + coordination_mandate
            )
        else:
            arch_user = (
                f"Project request:\n{project_request}\n\n"
                "Produce a clear high-level design brief that specialists can implement. "
                "Include the components needed, how they connect, and any constraints."
                + coordination_mandate
            )
        card_idx, label = _card(architect, role_to_card_idx)
        architect_idx = len(all_steps)
        all_steps.append(StepSpec(
            system=architect.system,
            user=arch_user,
            model=architect.model,
            tier=architect.tier,
            card_idx=card_idx,
            label=label,
            context_indices=(),
        ))

    # --- 2. Specialists ---
    # AWS specialists output requirement specs (no HCL).
    # Language specialists output code files directly.
    # Context chain: [architect, last_specialist] — lean; prevents quadratic growth.
    aws_spec_indices = []
    aws_specialist_info = []   # parallel to aws_spec_indices: (sid, AgentSpec)
    last_spec_idx = None

    for specialist_id, specialist in specialist_agents:
        ctx = tuple(i for i in [architect_idx, last_spec_idx] if i is not None)
        is_aws = SPECIALIST_IS_AWS.get(specialist_id, False)
        curr_idx = len(all_steps)
        card_idx, label = _card(specialist, role_to_card_idx)

        if is_aws:
            user = (
                f"You are the {specialist.role}. Define the requirements for your part of the project.\n"
                f"Original request: {project_request}\n"
                f"{manifest_block}\n"
                "Review the architect's brief and any prior specialist output in your context, "
                "then produce structured requirement specifications for your AWS resources.\n\n"
                "IMPORTANT: Do NOT write Terraform or any code. Output requirement specs only — "
                "describe WHAT to build and its properties. A dedicated Terraform specialist will "
                "author the HCL from your specs.\n\n"
                "Format each resource as:\n"
                "### Spec: <Resource Category>\n"
                "- Resource type: <aws_resource_type>\n"
                "- Logical name: <terraform_resource_name>\n"
                "- <property>: <value>\n"
                "...\n\n"
                "Include all required properties, security settings, cross-resource dependencies "
                "(name the other resource's logical name/type), and any IAM permissions required. "
                "Be precise — the Terraform specialist implements exactly what you specify."
            )
            aws_spec_indices.append(curr_idx)
            aws_specialist_info.append((specialist_id, specialist))
        else:
            user = (
                f"You are the {specialist.role}. Implement your specific piece of the project.\n"
                f"Original request: {project_request}\n"
                f"{manifest_block}\n"
                "Review the architect's brief and any prior specialist output in your context, "
                "then produce your specific contribution. Build on prior outputs rather than redesigning them.\n\n"
                "IMPORTANT: Be concise and structured. Output ONLY the essential code — "
                "no lengthy explanations, no preamble, no commentary. "
                "Label each file clearly as:\n"
                "### File: <filename>\n```<language>\n<code>\n```\n"
                f"Name your primary file after its purpose (e.g. `{specialist_id}_handler.py`, "
                "`index.ts`, etc.) — not `main` — so files stay distinct.\n"
            )

        all_steps.append(StepSpec(
            system=specialist.system,
            user=user,
            model=specialist.model,
            tier=specialist.tier,
            card_idx=card_idx,
            label=label,
            context_indices=ctx,
        ))
        last_spec_idx = curr_idx

    # --- 2.5. Terraform specialist ---
    # Runs only when there are AWS specialists. Receives [architect + all aws spec
    # steps] so it sees every requirement in one pass and can wire resources across
    # service boundaries. outputs.tf / variables.tf are generated in pure Python.
    terraform_idx = None
    if aws_spec_indices and terraform_specialist_agent is not None:
        tf_ctx = tuple(
            ([architect_idx] if architect_idx is not None else []) + aws_spec_indices
        )
        card_idx, label = _card(terraform_specialist_agent, role_to_card_idx)
        terraform_idx = len(all_steps)
        all_steps.append(StepSpec(
            system=terraform_specialist_agent.system,
            user=(
                f"Original request: {project_request}\n\n"
                "Read every '### Spec:' block from the AWS specialists in your context and "
                "author a complete, coherent set of Terraform (.tf) files that implements "
                "ALL specified requirements.\n\n"
                "Rules:\n"
                "- One file per service domain: vpc.tf, ec2.tf, s3.tf, iam.tf, lambda.tf, rds.tf, etc.\n"
                "- ALL cross-resource references MUST use Terraform attribute expressions "
                "(e.g. aws_s3_bucket.app_logs.arn) — never hardcoded strings\n"
                "- Define shared data sources (aws_caller_identity, aws_region, "
                "aws_availability_zones) ONCE across all files — no duplicates\n"
                "- Do NOT emit variables.tf or outputs.tf — generated automatically\n"
                "- Every resource in every spec must appear in the output\n"
                "- Use the logical names from the specs as your Terraform resource labels\n\n"
                + (
                    f"Resources already on disk (do NOT redefine these):\n{existing_manifest}\n\n"
                    if existing_manifest else ""
                )
                + "Output each file as:\n"
                "### File: <filename>\n```hcl\n<content>\n```"
            ),
            model=terraform_specialist_agent.model,
            tier=terraform_specialist_agent.tier,
            card_idx=card_idx,
            label=label,
            context_indices=tf_ctx,
        ))

    # --- 2.6. Per-specialist verification ---
    # Each AWS specialist re-checks that its own requirement spec was correctly
    # implemented in the HCL. They reuse the specialist's card so the UI shows
    # the same card lighting up again during the verify pass.
    if terraform_idx is not None and aws_specialist_info:
        for (sid, specialist), spec_idx in zip(aws_specialist_info, aws_spec_indices):
            card_idx, _ = _card(specialist, role_to_card_idx)
            all_steps.append(StepSpec(
                system=specialist.system,
                user=(
                    f"You are the {specialist.role}. Verify that the Terraform files correctly "
                    f"implement YOUR requirements from the '{sid}' spec.\n\n"
                    "Your original requirement spec is in your context. "
                    "The Terraform files are also in your context.\n\n"
                    "For each requirement bullet in your spec:\n"
                    "- ✅ if it is correctly implemented in the HCL\n"
                    "- ❌ if it is missing, incorrect, or incomplete — then fix it directly "
                    "in the relevant Terraform file\n\n"
                    "Output:\n"
                    f"1. '## Verification: {sid}' section — one ✅/❌ line per requirement\n"
                    "2. ONLY the Terraform files you changed, as '### File: <filename>' "
                    "fenced HCL blocks\n"
                    "If all your requirements were satisfied, output only the verification "
                    "section with no file blocks. Do NOT re-emit unchanged files."
                ),
                model=specialist.model,
                tier=specialist.tier,
                card_idx=card_idx,
                label=f"{sid.upper()} Verify",
                context_indices=(spec_idx, terraform_idx),
            ))

    # --- 3. Compliance auditor (optional) ---
    if compliance_framework and auditor is not None:
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        all_prior = tuple(range(len(all_steps)))
        card_idx, label = _card(auditor, role_to_card_idx)
        all_steps.append(StepSpec(
            system=auditor.system,
            user=(
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
            model=auditor.model,
            tier=auditor.tier,
            card_idx=card_idx,
            label=label,
            context_indices=all_prior,
        ))

    # --- 4. Wiring reviewer (always runs last on full-generation passes) ---
    # Runs after the auditor (if present) so it sees the fully-corrected config.
    # Its output is tasks_output[-1] — the step that overwrites only changed files.
    if wiring_reviewer is not None:
        all_prior = tuple(range(len(all_steps)))
        card_idx, label = _card(wiring_reviewer, role_to_card_idx)
        all_steps.append(StepSpec(
            system=wiring_reviewer.system,
            user=(
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
            model=wiring_reviewer.model,
            tier=wiring_reviewer.tier,
            card_idx=card_idx,
            label=label,
            context_indices=all_prior,
        ))

    return all_steps


def build_remediation_tasks(fixer, project_request, compliance_framework,
                            key_controls, findings_text, affected_context,
                            role_to_card_idx=None):
    """Build a single targeted remediation StepSpec for auto-iterate rounds 2+.

    Unlike build_tasks(), this runs no architect and no specialists. It feeds the
    fixer agent only the audit findings plus the current content of the files
    those findings name, and asks for corrected versions of ONLY those files.
    """
    controls_line = (f"Key controls to enforce: {key_controls}."
                     if key_controls else "Apply generally accepted security best practices.")
    card_idx, label = _card(fixer, role_to_card_idx)
    return [StepSpec(
        system=fixer.system,
        user=(
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
        model=fixer.model,
        tier=fixer.tier,
        card_idx=card_idx,
        label=label,
        context_indices=(),
    )]
