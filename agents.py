"""
Supporting agents that wrap around the dynamically-selected specialists.

These run on every project regardless of which specialists were chosen:
    - manager:            Orchestrates the pipeline and handles feedback loops.
    - architect:          Designs the overall solution before specialists implement pieces.
    - compliance_auditor: Reviews the final output for FedRAMP compliance.
    - git_specialist:     Writes the result to disk and commits it.
"""

from crewai import Agent
from tools import write_file_tool, git_commit_tool


_DEFAULT_CONTEXT = "No additional organizational context provided."


def _with_context(backstory: str, additional_context: str | None) -> str:
    """Append org guidelines to a backstory so they land in the cacheable system prompt."""
    if not additional_context or additional_context == _DEFAULT_CONTEXT:
        return backstory
    return f"{backstory}\n\n# Organizational Guidelines\n{additional_context}"


class SupportingAgents:
    def __init__(self, llm, additional_context: str | None = None):
        self.llm = llm
        self.additional_context = additional_context

    def manager(self, specialist_agents=None):
        """The hierarchical manager that coordinates the pipeline and handles
        feedback loops between the auditor and specialists.

        Args:
            specialist_agents: Optional list of (specialist_id, Agent) tuples so
                               the manager knows which coworkers are available.
        """
        # Build the coworker list dynamically so the manager knows exactly
        # which role names it can delegate to. Reduces hallucinated role names.
        fixed_coworkers = [
            '"Enterprise Solutions Architect"',
            '"FedRAMP Rev 5 High Compliance Auditor"',
            '"DevSecOps & CI/CD Engineer"',
        ]
        specialist_coworkers = (
            [f'"{agent.role}"' for _, agent in specialist_agents]
            if specialist_agents else []
        )
        all_coworkers = ", ".join(fixed_coworkers + specialist_coworkers)

        return Agent(
            role='Chief Technical Project Manager',
            goal=(
                'Orchestrate the full delivery pipeline in the correct order: '
                'architect first, then specialists in dependency order (networking before compute, '
                'compute before storage, etc.), then auditor, then git committer. '
                'If the auditor flags compliance issues, delegate corrections back to the '
                'relevant specialist before proceeding to commit. '
                'ALWAYS provide all three fields when delegating: '
                '"task" (what to do), "context" (all relevant prior output), '
                f'"coworker" (exact role name). Available coworkers: {all_coworkers}. '
                'When delegating to "DevSecOps & CI/CD Engineer", copy the COMPLETE '
                'auditor output into the context field — every "### File: <filename>" '
                'section and its code block. The git specialist cannot write files without them.'
            ),
            backstory=_with_context(
                'You are a principal engineer who runs tight, well-coordinated delivery pipelines. '
                'You never skip the audit step and always ensure flagged issues are corrected '
                'before the git specialist writes files. You never delegate without providing '
                'full context so coworkers have everything they need.',
                self.additional_context,
            ),
            llm=self.llm,
            allow_delegation=True,
            max_iter=7
        )

    def architect(self):
        return Agent(
            role='Enterprise Solutions Architect',
            goal='Design the overall solution at a high level before specialists implement individual pieces.',
            backstory=_with_context(
                'You produce clear, opinionated architecture briefs that specialists can implement '
                'without ambiguity. You consider security, compliance, scalability, and operational '
                'concerns at design time so the implementation phase has fewer rework cycles.',
                self.additional_context,
            ),
            llm=self.llm,
            allow_delegation=False,
            max_iter=3
        )

    def compliance_auditor(self, framework: str = "FedRAMP Rev 5 High",
                           key_controls: str = "", llm_override=None,
                           report_only: bool = False):
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
            max_iter = 3
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
            max_iter = 4

        return Agent(
            role=role,
            goal=goal,
            backstory=backstory,
            llm=llm_override or self.llm,
            allow_delegation=False,
            max_iter=max_iter,
        )

    def wiring_reviewer(self, llm_override=None):
        """Post-generation reviewer that enforces Terraform attribute references.

        Runs after all specialists and the compliance auditor on every full-generation
        pass. Its only job: find every place a cross-resource value (ARN, ID, name,
        endpoint, etc.) was constructed as a hardcoded string or data-source
        interpolation when a direct Terraform resource attribute reference
        (resource_type.resource_name.attribute) is available in the same config,
        and replace it. Compliance is out of scope — the auditor handles that.
        """
        return Agent(
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
            llm=llm_override or self.llm,
            allow_delegation=False,
            max_iter=3,
        )

    def remediation_engineer(self, framework: str = "FedRAMP Rev 5 High",
                             key_controls: str = "", llm_override=None):
        """A surgical fixer for auto-iterate rounds 2+.

        Given a findings list and the current content of only the affected files,
        it makes the minimal change to satisfy each finding and emits only the
        files it touched — so later rounds stop regenerating the whole design.
        """
        controls_line = (f"Key controls to enforce: {key_controls}."
                         if key_controls else "Apply generally accepted security best practices.")
        return Agent(
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
            llm=llm_override or self.llm,
            allow_delegation=False,
            max_iter=3,
        )

    def terraform_assembler(self):
        return Agent(
            role='Terraform Assembler',
            goal=(
                'Read all Terraform resource files produced by the specialists and generate '
                'a single outputs.tf file that exposes the most useful attributes of every '
                'resource created. Output ONLY the outputs.tf file — do not rewrite or '
                'repeat any resource files. Use this exact format:\n'
                '### File: outputs.tf\n```hcl\n<outputs>\n```\n\n'
                'For each resource include: id, arn, and 1–2 other commonly needed attributes '
                '(e.g. bucket_domain_name for S3, invoke_arn for Lambda, endpoint for RDS).\n'
                'If there are no Terraform resources in the context, output nothing.'
            ),
            backstory=_with_context(
                'You are a Terraform expert who writes clean, minimal outputs.tf files. '
                'You know exactly which attributes are most useful for each AWS resource type. '
                'You never duplicate resource definitions — you only add the outputs layer.',
                self.additional_context,
            ),
            llm=self.llm,
            allow_delegation=False,
            max_iter=2,
        )

    def git_specialist(self):
        return Agent(
            role='DevSecOps & CI/CD Engineer',
            goal='Save the compliant code to the file system and commit it safely.',
            backstory=_with_context(
                'You manage local files and git operations meticulously. You write every file '
                'exactly as specified, never paraphrase code, and you commit with concise '
                'descriptive messages that summarise the change.',
                self.additional_context,
            ),
            llm=self.llm,
            tools=[write_file_tool, git_commit_tool],
            allow_delegation=False,
            max_iter=5  # Git committer just calls tools — few iterations needed.
        )
