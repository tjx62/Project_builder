"""
Supporting agents that wrap around the dynamically-selected specialists.

These run on every project regardless of which specialists were chosen:
    - architect:          Designs the overall solution before specialists implement pieces.
    - compliance_auditor: Reviews the final output for FedRAMP compliance.
    - wiring_reviewer:    Fixes cross-resource references to use Terraform attributes.
    - terraform_specialist: Authors all HCL from the AWS specialists' requirement specs.
    - remediation_engineer: Surgical fixer for auto-iterate rounds 2+.

Each factory returns an executor.AgentSpec (a system-prompt template bound to a model
tier). The git step and outputs.tf/variables.tf generation are pure Python (tools.py).
"""

from executor import AgentSpec


_DEFAULT_CONTEXT = "No additional organizational context provided."


def _with_context(backstory: str, additional_context: str | None) -> str:
    """Append org guidelines to a backstory so they land in the cacheable system prompt."""
    if not additional_context or additional_context == _DEFAULT_CONTEXT:
        return backstory
    return f"{backstory}\n\n# Organizational Guidelines\n{additional_context}"


class SupportingAgents:
    """Factory for the non-specialist agents, bound to one model tier/id."""

    def __init__(self, tier: str, model: str, additional_context: str | None = None):
        self.tier = tier
        self.model = model
        self.additional_context = additional_context

    def _spec(self, role: str, goal: str, backstory: str) -> AgentSpec:
        return AgentSpec(role=role, goal=goal, backstory=backstory,
                         tier=self.tier, model=self.model)

    def architect(self) -> AgentSpec:
        return self._spec(
            role='Enterprise Solutions Architect',
            goal='Design the overall solution at a high level before specialists implement individual pieces.',
            backstory=_with_context(
                'You produce clear, opinionated architecture briefs that specialists can implement '
                'without ambiguity. You consider security, compliance, scalability, and operational '
                'concerns at design time so the implementation phase has fewer rework cycles.',
                self.additional_context,
            ),
        )

    def compliance_auditor(self, framework: str = "FedRAMP Rev 5 High",
                           key_controls: str = "", report_only: bool = False) -> AgentSpec:
        role = f"{framework} Compliance Auditor"
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")

        if report_only:
            goal = (
                f'Review the provided infrastructure files against {framework} requirements '
                f'and produce a structured findings report. {controls_line}\n\n'
                'Format your report exactly as:\n\n'
                f'# Audit Report — {framework}\n'
                '## Summary\nN findings: X Critical, Y High, Z Medium, W Low\n\n'
                '## Findings\n\n'
                '### Finding N — <Severity>: <Short title>\n'
                '**File:** <filename>  \n'
                '**Requirement:** <control ID / standard clause>  \n'
                '**Current state:** <what the code does now>  \n'
                '**Required fix:** <what must change — describe, do not write code>  \n\n'
                'If no issues are found, write "## Summary\nNo findings — all controls satisfied."\n\n'
                'Do NOT apply fixes. Do NOT emit code blocks. Findings report only.'
            )
            backstory = _with_context(
                f'You are a strict {framework} auditor who produces clear, actionable findings '
                'reports. You identify compliance gaps precisely, citing the exact resource and '
                'the specific control violated. You never write or fix code — you report what '
                'is wrong and what must change so developers can act on your findings.',
                self.additional_context,
            )
        else:
            goal = (
                f'Review the assembled output against {framework} requirements and apply any '
                f'compliance fixes required. {controls_line} '
                'Begin your output with a brief "## Audit Notes" section listing '
                'each issue you found and the fix you applied (one bullet per change). Then '
                'output ONLY the files you changed, as:\n'
                '### File: <filename>\n```<language>\n<content>\n```\n'
                'Do NOT re-output files you did not change — unchanged specialist files are '
                'preserved automatically. If you find no issues, output only the "## Audit Notes" '
                'section stating "No compliance issues found." and no file blocks. Do not delegate, '
                'do not request rework — you are the last reasoning step before the file writer.'
            )
            backstory = _with_context(
                f'You are a strict {framework} auditor with the authority to enforce compliance '
                'in-place. You read every specialist\'s output, identify compliance gaps, and '
                'produce the final corrected deliverable yourself. You never request rework '
                'from other agents — you apply the fix and emit the result.',
                self.additional_context,
            )

        return self._spec(role=role, goal=goal, backstory=backstory)

    def wiring_reviewer(self) -> AgentSpec:
        """Post-generation reviewer that enforces Terraform attribute references.

        Runs after all specialists and the compliance auditor on every full-generation
        pass. Its only job: find every place a cross-resource value (ARN, ID, name,
        endpoint, etc.) was constructed as a hardcoded string or data-source
        interpolation when a direct Terraform resource attribute reference
        (resource_type.resource_name.attribute) is available in the same config,
        and replace it. Compliance is out of scope — the auditor handles that.
        """
        return self._spec(
            role="Terraform Wiring Reviewer",
            goal=(
                "Review all Terraform files and fix every cross-resource reference that "
                "uses a hardcoded string or reconstructed value instead of a direct "
                "Terraform attribute reference.\n\n"
                "What to fix:\n"
                "- Hardcoded ARNs like 'arn:aws:s3:::my-bucket' when aws_s3_bucket.name.arn exists\n"
                "- Constructed names like 'my-bucket-${var.env}' when aws_s3_bucket.name.id exists\n"
                "- String interpolations for IDs, endpoints, DNS names, security-group IDs, "
                "subnet IDs, VPC IDs, KMS key ARNs, role ARNs — wherever the value is produced "
                "by another resource in the same config\n\n"
                "What NOT to change:\n"
                "- Values that are genuinely external (a bucket in another account/region, a "
                "pre-existing resource not managed here)\n"
                "- Variables (var.*) that are correct by design\n"
                "- Compliance controls, encryption settings, or IAM policy structure\n\n"
                "Output a brief '## Wiring Review' section listing each reference you fixed "
                "(one bullet: file, what it was, what it became). If nothing needed fixing, "
                "write 'No wiring issues found.' Then output ONLY the files you changed as "
                "'### File: <filename>' fenced blocks. Do NOT re-emit unchanged files."
            ),
            backstory=_with_context(
                "You are a Terraform expert who specialises in correctness of resource wiring. "
                "You know that hardcoded ARNs and reconstructed resource names are a common "
                "source of drift and misconfiguration — the resource name the developer intended "
                "and the name that actually gets created can diverge silently. You replace every "
                "such reference with the direct Terraform attribute that Terraform itself resolves "
                "at plan time, making dependencies explicit and plan-verifiable.",
                self.additional_context,
            ),
        )

    def terraform_specialist(self) -> AgentSpec:
        """Authors all Terraform HCL from AWS specialist requirement specs.

        Runs after all AWS specialists have produced their structured requirement
        specs. Its job is to translate those specs into one coherent set of .tf
        files, ensuring consistent naming, correct attribute references, and no
        duplicate data sources.
        """
        return self._spec(
            role="Terraform IaC Specialist",
            goal=(
                "Read all AWS specialist requirement specs and author a complete, coherent set "
                "of Terraform (.tf) files that implements every specified requirement.\n\n"
                "Rules:\n"
                "- One file per service domain: vpc.tf, ec2.tf, s3.tf, iam.tf, lambda.tf, rds.tf, etc.\n"
                "- ALL cross-resource references MUST use Terraform attribute expressions "
                "(e.g. aws_s3_bucket.app_logs.arn), never hardcoded strings or reconstructed values\n"
                "- Define shared data sources (aws_caller_identity, aws_region, "
                "aws_availability_zones) ONCE across all files — no duplicates\n"
                "- Do NOT emit variables.tf or outputs.tf — those are generated automatically\n"
                "- Every resource named in the specs must appear in the output\n"
                "- Be explicit: include all required arguments, no placeholders\n\n"
                "Output each file as:\n"
                "### File: <filename>\n```hcl\n<content>\n```"
            ),
            backstory=_with_context(
                "You are a Terraform expert who specialises in translating architecture "
                "requirement specs into clean, correct HCL. You know exactly how to wire "
                "AWS resources together — IAM roles to instance profiles, security group "
                "rules between tiers, KMS keys to encrypted resources — and you always use "
                "Terraform attribute references rather than hardcoded values so that Terraform "
                "can plan and validate the dependency graph correctly.",
                self.additional_context,
            ),
        )

    def remediation_engineer(self, framework: str = "FedRAMP Rev 5 High",
                             key_controls: str = "") -> AgentSpec:
        """A surgical fixer for auto-iterate rounds 2+.

        Given a findings list and the current content of only the affected files,
        it makes the minimal change to satisfy each finding and emits only the
        files it touched — so later rounds stop regenerating the whole design.
        """
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        return self._spec(
            role=f"{framework} Remediation Engineer",
            goal=(
                f'Apply specific {framework} audit fixes to existing files. {controls_line} '
                'You touch ONLY the files named in the findings and change ONLY what each '
                'finding requires. You never redesign, never add unrequested resources, and '
                'never re-emit files that have no findings. Begin with a "## Audit Notes" '
                'section (one bullet per fix), then output only the corrected files as '
                '### File: <filename> with a fenced code block.'
            ),
            backstory=_with_context(
                'You are a surgical remediation specialist. Given a findings list and the '
                'current file contents, you make the minimal diff that satisfies each finding '
                'and emit only the files you touched.',
                self.additional_context,
            ),
        )
