import ctypes
import subprocess
import streamlit as st
import sys
import re
import os
import queue
import threading
import logging
import time
from dotenv import load_dotenv
from crewai import Crew, Process, LLM
from crewai.events.event_bus import crewai_event_bus
from crewai.events.types.agent_events import (
    AgentExecutionStartedEvent,
    AgentExecutionCompletedEvent,
)
from crewai.events.types.llm_events import LLMCallCompletedEvent

from specialists import SPECIALISTS, SPECIALIST_DESCRIPTIONS
from planner import plan_specialists
from agents import SupportingAgents
from tasks import build_tasks, build_remediation_tasks, build_wiring_review_task
from tools import (
    commit_audited_output, read_workspace_context, write_findings,
    parse_finding_files, read_specific_files,
    terraform_validate, render_validation_findings,
)

# Architect runs only for requests with more than this many specialists; simple
# 1–2 specialist requests skip the design phase to save a full generation.
ARCHITECT_MIN_SPECIALISTS = 3

load_dotenv()

# ==========================================
# 1. STREAMLIT CONFIGURATION
# ==========================================
st.set_page_config(page_title="Adaptive Code Builder", page_icon="🏗️", layout="wide")
st.title("🏗️ Adaptive AI Code Builder")
st.markdown("Specialists chosen on the fly based on your project request.")


# --- Queue-based log capture ---
# CrewAI routes verbose output through Python logging (not print), so we
# need both a stdout redirector and a logging handler to capture everything.

class QueueLogHandler(logging.Handler):
    """Intercepts Python logging records and puts them in the queue.
    This is what actually captures CrewAI's verbose output in newer versions."""
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            clean = re.sub(r'\x1b\[[0-9;]*m', '', msg)
            if clean.strip():
                self.log_queue.put(clean)
        except Exception:
            pass


class QueueCapture:
    def __init__(self, log_queue):
        self.log_queue = log_queue

    def write(self, text):
        clean_text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        if clean_text.strip():
            self.log_queue.put(clean_text)

    def flush(self):
        pass


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _pick_directory(initial_dir: str = ".") -> str | None:
    """Open a native folder-picker dialog. Returns chosen path or None if cancelled."""
    abs_dir = os.path.abspath(initial_dir)

    if _is_wsl():
        # WSL2 — delegate to Windows Explorer via PowerShell.
        try:
            win_init = subprocess.run(
                ["wslpath", "-w", abs_dir], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            win_init = ""
        init_clause = f"$d.SelectedPath = '{win_init}'; " if win_init else ""
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Select project directory'; "
            "$d.ShowNewFolderButton = $true; "
            + init_clause
            + "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=120,
            )
            win_path = result.stdout.strip()
            if not win_path:
                return None
            return subprocess.run(
                ["wslpath", win_path], capture_output=True, text=True, timeout=5
            ).stdout.strip() or None
        except Exception:
            return None

    if sys.platform == "darwin":
        # macOS — AppleScript is more reliable than tkinter on modern macOS.
        script = (
            f'POSIX path of (choose folder with prompt "Select project directory" '
            f'default location POSIX file "{abs_dir}")'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=120,
            )
            path = result.stdout.strip().rstrip("/")
            if path:
                return path
        except Exception:
            pass  # fall through to tkinter

    # Windows or Linux with a display — tkinter ships with Python on both.
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        if sys.platform == "win32":
            root.wm_attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=abs_dir, title="Select project directory")
        root.destroy()
        return chosen or None
    except Exception:
        return None


# ==========================================
# COMPLIANCE FRAMEWORKS
# ==========================================
# None → skip the auditor entirely.
# Otherwise the value is the key_controls string injected into the auditor's
# goal and task so the LLM knows what to look for.
COMPLIANCE_FRAMEWORKS: dict[str, str | None] = {
    "None":                None,
    "FedRAMP Rev 5 High":  "FIPS 140-3 encryption, KMS key management, least-privilege IAM (AC-6), audit logging (AU-2), encryption at rest (SC-28), TLS in transit (SC-8), FedRAMP High baselines",
    "SOC 2 Type II":       "encryption at rest and in transit, logical access controls and least privilege, audit and activity logging, availability monitoring, change management controls",
    "HIPAA":               "PHI encryption at rest and in transit, access controls for ePHI, audit controls (164.312(b)), integrity controls, transmission security, minimum-necessary access",
    "PCI DSS v4":          "TLS 1.2+ only, encryption of cardholder data at rest, least-privilege access, audit logging of all access, network segmentation, no hard-coded credentials",
    "NIST 800-53 Moderate":"access control (AC), audit and accountability (AU), identification and authentication (IA), system and communications protection (SC), system and information integrity (SI)",
}


# ==========================================
# PRICING + COST / DURATION HELPERS
# ==========================================
# Anthropic published rates, per million tokens. Cache reads bill at 10% of
# the input rate; ephemeral (5-minute) cache writes bill at 125%.
PRICING = {
    "haiku":  {"input": 1.0, "output": 5.0,  "cache_read": 0.10, "cache_create": 1.25},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
}


def _model_tier(model: str | None) -> str:
    """Map an Anthropic model string to a pricing tier ('haiku' or 'sonnet')."""
    return "sonnet" if "sonnet" in (model or "").lower() else "haiku"


def _empty_tokens() -> dict:
    return {tier: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
            for tier in PRICING}


def _compute_cost(tokens: dict, *, with_cache: bool) -> float:
    """Compute cost across both tiers. with_cache=False bills cache tokens as fresh input."""
    total = 0.0
    for tier, p in PRICING.items():
        t = tokens.get(tier, {})
        i  = t.get("input",        0)
        o  = t.get("output",       0)
        cr = t.get("cache_read",   0)
        cc = t.get("cache_create", 0)
        if with_cache:
            total += (i  * p["input"]
                      + o  * p["output"]
                      + cr * p["cache_read"]
                      + cc * p["cache_create"]) / 1_000_000
        else:
            total += ((i + cr + cc) * p["input"]
                      + o * p["output"]) / 1_000_000
    return total


_NO_FINDINGS_RE  = re.compile(
    r'no findings|no compliance issues|0\s+findings|zero findings|all controls satisfied',
    re.IGNORECASE,
)
_FINDING_COUNT_RE = re.compile(r'(\d+)\s+finding', re.IGNORECASE)


def _is_compliant(audit_text: str) -> bool:
    return bool(_NO_FINDINGS_RE.search(audit_text))


def _count_findings(audit_text: str) -> int:
    m = _FINDING_COUNT_RE.search(audit_text)
    return int(m.group(1)) if m else 0


def _file_tree(files: list[str], base_dir: str) -> str:
    """Format a list of relative file paths as a Unicode directory tree."""
    from pathlib import Path

    tree: dict = {}
    for f in sorted(files):
        node = tree
        for part in Path(f).parts[:-1]:
            node = node.setdefault(part, {})
        node[Path(f).name] = None

    lines = [f"📁 {base_dir}"]

    def _render(node: dict, prefix: str = "") -> None:
        items = list(node.items())
        for i, (name, children) in enumerate(items):
            is_last = i == len(items) - 1
            branch = "└── " if is_last else "├── "
            pad    = "    " if is_last else "│   "
            if children is None:
                lines.append(f"{prefix}{branch}{name}")
            else:
                lines.append(f"{prefix}{branch}📁 {name}/")
                _render(children, prefix + pad)

    _render(tree)
    return "\n".join(lines)


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ==========================================
# 2. SESSION STATE — tracks the multi-phase flow
# ==========================================
# The UI has three phases: 'input' (user types request),
# 'confirm' (user reviews proposed specialists),
# 'execute' (crew runs). We track the current phase plus any data
# carried between phases.
if "phase" not in st.session_state:
    st.session_state.phase = "input"
if "plan" not in st.session_state:
    st.session_state.plan = None
if "project_request" not in st.session_state:
    st.session_state.project_request = ""
if "context_text" not in st.session_state:
    st.session_state.context_text = "No additional organizational context provided."
if "confirmed_ids" not in st.session_state:
    st.session_state.confirmed_ids = []
if "pipeline_done" not in st.session_state:
    st.session_state.pipeline_done = False
if "pipeline_result" not in st.session_state:
    st.session_state.pipeline_result = None
if "pipeline_error" not in st.session_state:
    st.session_state.pipeline_error = None
if "pipeline_running" not in st.session_state:
    st.session_state.pipeline_running = False
if "project_path" not in st.session_state:
    st.session_state.project_path = "."
if "compliance_framework" not in st.session_state:
    st.session_state.compliance_framework = "None"
if "audit_only" not in st.session_state:
    st.session_state.audit_only = False
if "auto_iterate" not in st.session_state:
    st.session_state.auto_iterate = False
if "max_rounds" not in st.session_state:
    st.session_state.max_rounds = 3


def _interrupt_pipeline_thread():
    """Raise SystemExit in the pipeline thread if it's still alive."""
    t = st.session_state.get("pipeline_thread")
    if t and t.is_alive():
        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(t.ident),
            ctypes.py_object(SystemExit),
        )


def reset():
    """Wipe all pipeline state and return to phase 1."""
    _interrupt_pipeline_thread()
    st.session_state.phase = "input"
    st.session_state.plan = None
    st.session_state.project_request = ""
    st.session_state.confirmed_ids = []
    st.session_state.pipeline_done = False
    st.session_state.pipeline_running = False
    st.session_state.pipeline_result = None
    st.session_state.pipeline_error = None
    for key in (
        "pipeline_thread", "pipeline_queue", "pipeline_result_holder",
        "pipeline_labels", "active_index", "pipeline_completed",
        "activity_log", "step_count",
        "tokens", "pipeline_start_time", "pipeline_end_time",
        "git_result", "_existing_file_count", "findings_result", "auto_iterate_result",
    ):
        st.session_state.pop(key, None)


# ==========================================
# 3. SIDEBAR — global config (always visible)
# ==========================================
with st.sidebar:
    st.header("⚙️ Configuration")

    typed = st.text_input(
        "Project Directory",
        value=st.session_state.project_path,
        help="Where the git committer will write files.",
    )
    if typed != st.session_state.project_path:
        st.session_state.project_path = typed

    if st.button("📁 Browse...", use_container_width=True):
        chosen = _pick_directory(st.session_state.project_path)
        if chosen:
            st.session_state.project_path = chosen
            st.rerun()

    mode_col1, mode_col2 = st.columns(2)
    with mode_col1:
        st.session_state.audit_only = st.toggle(
            "Audit Only",
            value=st.session_state.audit_only,
            disabled=st.session_state.auto_iterate,
            help="Skip code generation — audit existing files only.",
        )
    with mode_col2:
        st.session_state.auto_iterate = st.toggle(
            "Auto-iterate",
            value=st.session_state.auto_iterate,
            disabled=st.session_state.audit_only,
            help="Generate → audit → fix, repeated until compliant or max rounds reached.",
        )

    # Both modes require a real framework — auto-bump from None if needed.
    if (st.session_state.audit_only or st.session_state.auto_iterate) \
            and st.session_state.compliance_framework == "None":
        st.session_state.compliance_framework = "FedRAMP Rev 5 High"

    if st.session_state.auto_iterate:
        st.session_state.max_rounds = st.slider(
            "Max rounds", min_value=1, max_value=5,
            value=st.session_state.max_rounds,
            help="Pipeline stops early if the audit reports no findings.",
        )

    fw_options = [k for k in COMPLIANCE_FRAMEWORKS if k != "None"] \
        if st.session_state.audit_only else list(COMPLIANCE_FRAMEWORKS.keys())
    st.session_state.compliance_framework = st.selectbox(
        "Compliance Framework",
        options=fw_options,
        index=fw_options.index(st.session_state.compliance_framework)
               if st.session_state.compliance_framework in fw_options else 0,
        help="Auditor reviews output against this framework. 'None' skips the audit step entirely.",
    )

    project_path = st.session_state.project_path

    st.header("📄 Organizational Context")
    uploaded_file = st.file_uploader("Upload Guidelines File", type=["md", "txt"])
    if uploaded_file is not None:
        st.session_state.context_text = uploaded_file.getvalue().decode("utf-8")
        st.success("Context loaded.")

    st.divider()
    if st.button("🔄 Start Over"):
        reset()
        st.rerun()


# ==========================================
# LLM TIERS — three models, each matched to the complexity of its role.
#
#   Haiku   → planner (classification only, fast and cheap)
#   Sonnet  → specialists + wiring reviewer (implementation, instruction-following)
#   Opus    → architect + auditors (high-stakes reasoning and compliance review)
#
# To change which tier a role uses, swap the llm argument when that agent
# is instantiated in Phase 3 below. No other files need to change.
# ==========================================
api_key = os.environ.get("ANTHROPIC_API_KEY")

haiku_llm = LLM(
    model="anthropic/claude-haiku-4-5",
    api_key=api_key,
    temperature=0.2,
    max_tokens=8192
)

sonnet_llm = LLM(
    model="anthropic/claude-sonnet-4-6",
    api_key=api_key,
    temperature=0.2,
    max_tokens=8192
)

opus_llm = LLM(
    model="anthropic/claude-opus-4-8",
    api_key=api_key,
    temperature=0.2,
    max_tokens=8192
)


# ==========================================
# 4. PHASE 1 — Input
# ==========================================
if st.session_state.phase == "input":
    _audit_mode = st.session_state.audit_only
    st.session_state.project_request = st.text_area(
        "Auditor focus (optional)" if _audit_mode else "Describe your project",
        height=150,
        placeholder=(
            "e.g., Focus on encryption at rest and IAM least-privilege."
            if _audit_mode else
            "e.g., Build a Python Lambda function that processes S3 uploads and writes results to RDS."
        ),
        value=st.session_state.project_request,
    )

    if _audit_mode:
        if st.button("🔍 Run Audit", type="primary"):
            if not api_key:
                st.error("ANTHROPIC_API_KEY is missing from your .env file.")
            else:
                _ctx = read_workspace_context(st.session_state.project_path)
                if not _ctx:
                    st.error("Audit-only mode requires existing files in the workspace directory.")
                else:
                    st.session_state.confirmed_ids = []
                    st.session_state.phase = "execute"
                    st.rerun()
    else:
        if st.button("🧭 Plan Specialists", type="primary"):
            if not st.session_state.project_request.strip():
                st.error("Please describe the project first.")
            elif not api_key:
                st.error("ANTHROPIC_API_KEY is missing from your .env file.")
            else:
                with st.spinner("Planner is analyzing the request..."):
                    try:
                        st.session_state.plan = plan_specialists(
                            llm=haiku_llm,
                            project_request=st.session_state.project_request,
                            additional_context=st.session_state.context_text
                        )
                        st.session_state.phase = "confirm"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Planner failed: {e}")


# ==========================================
# 5. PHASE 2 — Confirmation
# ==========================================
elif st.session_state.phase == "confirm":
    plan = st.session_state.plan
    st.subheader("📋 Proposed Specialist Team")
    st.markdown(f"**Planner's reasoning:** {plan.reasoning}")

    # Filter the planner's output to only IDs that actually exist in the registry,
    # in case the LLM hallucinated a specialist that isn't real.
    valid_proposed = [sid for sid in plan.specialists if sid in SPECIALISTS]
    invalid_proposed = [sid for sid in plan.specialists if sid not in SPECIALISTS]
    if invalid_proposed:
        st.warning(f"Planner suggested unknown specialists (ignored): {invalid_proposed}")

    # Let the user adjust the list. Default is the planner's filtered selection;
    # they can add or remove any specialist from the full registry.
    confirmed_ids = st.multiselect(
        "Adjust the specialist list (order matters — foundation specialists like VPC should come before dependents like EC2):",
        options=list(SPECIALISTS.keys()),
        default=valid_proposed,
        help="Specialists run in the order listed. Add or remove as needed before executing."
    )

    # Show the user what each specialist does for reference.
    with st.expander("ℹ️ Specialist reference"):
        for sid, desc in SPECIALIST_DESCRIPTIONS.items():
            st.markdown(f"- **{sid}**: {desc}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🚀 Execute Pipeline", type="primary", disabled=len(confirmed_ids) == 0):
            st.session_state.confirmed_ids = confirmed_ids
            st.session_state.phase = "execute"
            st.rerun()
    with col2:
        if st.button("← Back"):
            st.session_state.phase = "input"
            st.rerun()


# ==========================================
# 6. PHASE 3 — Execution
# ==========================================
elif st.session_state.phase == "execute":
    os.environ["PROJECT_WORKSPACE_DIR"] = project_path

    # --- Result view (pipeline already finished or stopped) ---
    if st.session_state.pipeline_done:
        if st.session_state.pipeline_error:
            st.error(f"Pipeline error: {st.session_state.pipeline_error}")
        else:
            st.success("Pipeline Completed!")

        # --- Run summary: duration + cost + cache savings ---
        st.markdown("### 🎯 Run Summary")

        start = st.session_state.get("pipeline_start_time")
        end   = st.session_state.get("pipeline_end_time") or time.time()
        duration = (end - start) if start else 0.0

        tokens        = st.session_state.get("tokens", _empty_tokens())
        actual_cost   = _compute_cost(tokens, with_cache=True)
        no_cache_cost = _compute_cost(tokens, with_cache=False)
        saved         = no_cache_cost - actual_cost
        saved_pct     = (saved / no_cache_cost * 100) if no_cache_cost > 0 else 0.0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Duration",     _fmt_duration(duration))
        m2.metric("Actual cost",  f"${actual_cost:.4f}")
        m3.metric("Without cache", f"${no_cache_cost:.4f}")
        m4.metric("Saved",        f"${saved:.4f}", f"{saved_pct:.0f}%")

        cr_total = sum(t["cache_read"]   for t in tokens.values())
        cc_total = sum(t["cache_create"] for t in tokens.values())
        fresh    = sum(t["input"]        for t in tokens.values())
        out      = sum(t["output"]       for t in tokens.values())
        total_in = cr_total + cc_total + fresh
        hit_rate = (cr_total / total_in * 100) if total_in else 0.0
        st.caption(
            f"💾 cache read **{cr_total:,}** · cache write **{cc_total:,}** · "
            f"fresh input **{fresh:,}** · output **{out:,}** · "
            f"hit rate **{hit_rate:.0f}%**"
        )

        with st.expander("📊 Per-model breakdown"):
            for tier, t in tokens.items():
                if any(t.values()):
                    st.markdown(
                        f"**{tier.title()}** — input {t['input']:,} · "
                        f"output {t['output']:,} · "
                        f"cache read {t['cache_read']:,} · "
                        f"cache write {t['cache_create']:,}"
                    )

        ai_r       = st.session_state.get("auto_iterate_result")
        findings_r = st.session_state.get("findings_result")
        git_r      = st.session_state.get("git_result") or {}

        if ai_r:
            # Auto-iterate result
            rounds, max_r, converged = ai_r["rounds"], ai_r["max_rounds"], ai_r["converged"]
            if converged:
                st.success(f"✅ Converged — compliant after {rounds} / {max_r} round(s).")
            else:
                st.warning(
                    f"⚠️ Max rounds reached ({rounds}/{max_r}). "
                    "See `audit_findings.md` for remaining issues."
                )
            committed = git_r.get("files") or []
            if committed:
                st.markdown("### Generated Files")
                st.code(_file_tree(committed, project_path), language=None)

        elif findings_r:
            # Audit-only result
            if findings_r.get("error"):
                st.warning(f"Findings written but git failed: {findings_r['error']}")
            else:
                st.success("📋 Audit findings written to `audit_findings.md` and committed.")
            st.markdown("### Audit Report")
            st.markdown(st.session_state.pipeline_result)

        else:
            # Normal generate result
            committed = git_r.get("files") or []
            git_error = git_r.get("error")
            if git_error and not committed:
                st.warning(f"Git step issue: {git_error}")
            elif git_error:
                st.warning(f"Wrote {len(committed)} file(s) but git commit failed: {git_error}")
            if committed:
                st.markdown("### Generated Files")
                st.code(_file_tree(committed, project_path), language=None)
            elif not git_error:
                st.info("No files were written — the output contained no '### File:' blocks.")

        if st.button("🔄 Start a New Project"):
            reset()
            st.rerun()
        st.stop()

    # --- First entry: build and launch the crew in a background thread ---
    # pipeline_running stays False until the thread is started, so this block
    # runs exactly once per pipeline execution even as Streamlit reruns the
    # script every polling tick.
    if not st.session_state.pipeline_running:
        context_text     = st.session_state.context_text
        support_opus     = SupportingAgents(opus_llm,   additional_context=context_text)
        support_sonnet   = SupportingAgents(sonnet_llm, additional_context=context_text)
        framework_name   = st.session_state.compliance_framework
        key_controls     = COMPLIANCE_FRAMEWORKS.get(framework_name) or ""
        is_audit_only    = st.session_state.audit_only
        existing_context = read_workspace_context(project_path)
        existing_file_count = existing_context.count("### File:") if existing_context else 0
        st.session_state._existing_file_count = existing_file_count

        # Auditors run on Opus (compliance reasoning is the highest-stakes step).
        # The remediation fixer and wiring reviewer run on Sonnet (implementation work).
        auditor_llm  = opus_llm
        reviewer_llm = sonnet_llm

        is_auto_iterate = st.session_state.auto_iterate
        max_rounds      = st.session_state.max_rounds

        if is_audit_only:
            # ── Audit-only: Auditor → Write Findings ───────────────────────
            auditor = support_opus.compliance_auditor(
                framework=framework_name, key_controls=key_controls,
                llm_override=auditor_llm, report_only=True,
            )
            pipeline     = [("Auditor", "Opus", auditor), ("Write Findings", "Python", None)]
            findings_idx = 1
            crew_agents  = [auditor]
            tasks = build_tasks(
                architect=None, specialist_agents=[], auditor=auditor,
                project_request=st.session_state.project_request,
                compliance_framework=framework_name, key_controls=key_controls,
                existing_context=existing_context, audit_only=True,
            )

        elif is_auto_iterate:
            # ── Auto-iterate: crew + tasks built fresh each round inside thread ──
            specialist_agents = [
                (sid, SPECIALISTS[sid](sonnet_llm, additional_context=context_text))
                for sid in st.session_state.confirmed_ids
            ]
            use_architect = len(st.session_state.confirmed_ids) >= ARCHITECT_MIN_SPECIALISTS
            architect    = support_opus.architect() if use_architect else None
            # Inline auditor applies compliance fixes during round-1 full pass.
            # Report auditor verifies compliance after each round.
            # Fixer applies targeted fixes in rounds 2+.
            # Wiring reviewer checks cross-resource references after generation.
            inline_auditor = support_opus.compliance_auditor(
                framework=framework_name, key_controls=key_controls,
                llm_override=auditor_llm, report_only=False,
            )
            report_auditor = support_opus.compliance_auditor(
                framework=framework_name, key_controls=key_controls,
                llm_override=auditor_llm, report_only=True,
            )
            fixer = support_sonnet.remediation_engineer(
                framework=framework_name, key_controls=key_controls,
                llm_override=reviewer_llm,
            )
            wiring_reviewer = support_sonnet.wiring_reviewer(llm_override=reviewer_llm)
            generate_crew_agents = (
                ([architect] if architect else [])
                + [agent for _, agent in specialist_agents]
                + [inline_auditor]
                + [wiring_reviewer]
            )
            pipeline = (
                ([("Architect", "Opus", architect)] if architect else [])
                + [(sid.upper(), "Sonnet", agent) for sid, agent in specialist_agents]
                + [("Auditor", "Opus", inline_auditor)]
                + [("Wiring Review", "Sonnet", wiring_reviewer)]
                + [("Git", "Python", None)]
            )
            findings_idx = None
            crew_agents  = generate_crew_agents   # for role_to_idx only
            tasks        = []  # built inside run_crew each round

        else:
            # ── Normal: (Architect) → Specialists → (Auditor) → Wiring Review → Git ──
            specialist_agents = [
                (sid, SPECIALISTS[sid](sonnet_llm, additional_context=context_text))
                for sid in st.session_state.confirmed_ids
            ]
            use_architect = len(st.session_state.confirmed_ids) >= ARCHITECT_MIN_SPECIALISTS
            architect   = support_opus.architect() if use_architect else None
            has_auditor = bool(framework_name and framework_name != "None")
            auditor     = (
                support_opus.compliance_auditor(
                    framework=framework_name, key_controls=key_controls,
                    llm_override=auditor_llm,
                ) if has_auditor else None
            )
            wiring_reviewer = support_sonnet.wiring_reviewer(llm_override=reviewer_llm)
            pipeline  = (
                ([("Architect",     "Opus",   architect)]       if architect   else [])
                + [(sid.upper(),    "Sonnet", agent)            for sid, agent in specialist_agents]
                + ([("Auditor",     "Opus",   auditor)]         if has_auditor else [])
                + [("Wiring Review","Sonnet", wiring_reviewer)]
                + [("Git", "Python", None)]
            )
            findings_idx = None
            crew_agents  = (
                ([architect] if architect else [])
                + [agent for _, agent in specialist_agents]
                + ([auditor]   if has_auditor    else [])
                + [wiring_reviewer]
            )
            tasks = build_tasks(
                architect=architect, specialist_agents=specialist_agents, auditor=auditor,
                project_request=st.session_state.project_request,
                compliance_framework=framework_name if has_auditor else None,
                key_controls=key_controls,
                existing_context=existing_context,
                wiring_reviewer=wiring_reviewer,
            )

        git_step_idx = len(pipeline) - 1

        # Capture session state values that the background thread needs.
        # st.session_state cannot be accessed from threads other than the main one.
        project_request = st.session_state.project_request

        log_queue     = queue.Queue()
        result_holder = {}

        role_to_idx = {
            agent_obj.role: i
            for i, (_label, _tier, agent_obj) in enumerate(pipeline)
            if agent_obj is not None
        }
        if is_auto_iterate:
            # The remediation fixer (rounds 2+) isn't in the pipeline list; map its
            # role onto the Auditor card so its activity still lights up there.
            auditor_idx = next(
                i for i, (label, _t, _a) in enumerate(pipeline) if label == "Auditor"
            )
            role_to_idx[fixer.role] = auditor_idx

        def on_agent_started(source, event):
            idx = role_to_idx.get(event.agent.role)
            if idx is not None:
                log_queue.put(f"__ACTIVE__:{idx}:{pipeline[idx][0]}")

        def on_agent_completed(source, event):
            idx = role_to_idx.get(event.agent.role)
            if idx is not None:
                log_queue.put(f"__DONE__:{idx}:{pipeline[idx][0]}")

        def on_llm_completed(source, event):
            # CrewAI's Anthropic provider normalises usage keys to
            # 'cached_prompt_tokens' and 'cache_creation_tokens' (see
            # crewai/llms/providers/anthropic/completion.py:1829).
            usage = getattr(event, "usage", None) or {}
            tier  = _model_tier(getattr(event, "model", None))
            log_queue.put(
                f"__USAGE__:{tier}:"
                f"{int(usage.get('input_tokens', 0) or 0)}:"
                f"{int(usage.get('output_tokens', 0) or 0)}:"
                f"{int(usage.get('cached_prompt_tokens', 0) or 0)}:"
                f"{int(usage.get('cache_creation_tokens', 0) or 0)}"
            )

        crewai_event_bus.register_handler(AgentExecutionStartedEvent,   on_agent_started)
        crewai_event_bus.register_handler(AgentExecutionCompletedEvent, on_agent_completed)
        crewai_event_bus.register_handler(LLMCallCompletedEvent,        on_llm_completed)

        def on_task_complete(task_output):
            log_queue.put("__STEP__")

        # Single-run modes pre-build a Crew; auto-iterate builds one per round.
        crew = (
            None if is_auto_iterate else
            Crew(
                agents=crew_agents, tasks=tasks,
                process=Process.sequential,
                task_callback=on_task_complete,
                respect_context_window=True,
                memory=False, verbose=True,
            )
        )

        def _make_crew(agents, tasks_list):
            return Crew(
                agents=agents, tasks=tasks_list,
                process=Process.sequential,
                task_callback=on_task_complete,
                respect_context_window=True,
                memory=False, verbose=True,
            )

        def run_crew():
            original_stdout = sys.stdout
            sys.stdout = QueueCapture(log_queue)
            log_handler = QueueLogHandler(log_queue)
            log_handler.setFormatter(logging.Formatter("%(message)s"))
            root_logger = logging.getLogger()
            original_level = root_logger.level
            root_logger.setLevel(logging.DEBUG)
            root_logger.addHandler(log_handler)
            try:
                if is_auto_iterate:
                    # ── Auto-iterate loop ─────────────────────────────────────
                    gen_result  = None
                    audit_text  = ""
                    converged   = False
                    round_num   = 0
                    for round_num in range(1, max_rounds + 1):
                        log_queue.put(f"__ROUND__:{round_num}:{max_rounds}")

                        # Generate phase. Round 1 is a full pass (architect →
                        # specialists → inline auditor). Rounds 2+ are targeted
                        # remediation: a single fixer rewrites only the files the
                        # previous round's findings named, instead of regenerating
                        # the whole design.
                        # variables.tf / outputs.tf are regenerated in pure Python
                        # at commit time, so never send them to the fixer.
                        affected = (parse_finding_files(audit_text) - {"variables.tf", "outputs.tf"}
                                    if round_num > 1 else set())
                        affected_ctx = (
                            read_specific_files(project_path, affected) if affected else None
                        )

                        if round_num > 1 and affected_ctx:
                            round_tasks = build_remediation_tasks(
                                fixer=fixer,
                                project_request=project_request,
                                compliance_framework=framework_name,
                                key_controls=key_controls,
                                findings_text=audit_text,
                                affected_context=affected_ctx,
                            )
                            gen_crew = _make_crew([fixer], round_tasks)
                            # Only commit the files the fixer was asked to touch;
                            # discard anything extra it regenerated from scratch.
                            allowed = affected
                        else:
                            # Round 1, or a later round whose findings didn't map to
                            # specific files → fall back to a full generation pass.
                            round_ctx   = read_workspace_context(project_path)
                            round_tasks = build_tasks(
                                architect=architect,
                                specialist_agents=specialist_agents,
                                auditor=inline_auditor,
                                project_request=project_request,
                                compliance_framework=framework_name,
                                key_controls=key_controls,
                                existing_context=round_ctx,
                                wiring_reviewer=wiring_reviewer,
                            )
                            gen_crew = _make_crew(generate_crew_agents, round_tasks)
                            allowed = None

                        gen_result  = gen_crew.kickoff()

                        # Commit the generated files. has_auditor=True: the final
                        # task (inline auditor or fixer) overwrites only the files
                        # it changed; unchanged specialist files are preserved.
                        log_queue.put(f"__ACTIVE__:{git_step_idx}:Git")
                        commit_audited_output(gen_result, has_auditor=True, allowed_files=allowed)
                        log_queue.put(f"__DONE__:{git_step_idx}:Git")

                        # Audit phase — re-read workspace (now includes committed files).
                        log_queue.put(f"__INFO__:Auditing (round {round_num}/{max_rounds})...")
                        audit_ctx   = read_workspace_context(project_path)
                        audit_tasks = build_tasks(
                            architect=None, specialist_agents=[],
                            auditor=report_auditor,
                            project_request=project_request,
                            compliance_framework=framework_name,
                            key_controls=key_controls,
                            existing_context=audit_ctx,
                            audit_only=True,
                        )
                        audit_crew   = _make_crew([report_auditor], audit_tasks)
                        audit_result = audit_crew.kickoff()
                        audit_text   = str(getattr(audit_result, "raw", "") or audit_result)

                        # Deterministic integration check: terraform validate catches
                        # broken cross-resource references (e.g. an IAM policy pointing at
                        # a bucket that doesn't exist) that the compliance audit misses.
                        # Its errors are folded into the findings so the next round's
                        # fixer repairs them alongside compliance gaps.
                        tf = terraform_validate(project_path)
                        tf_clean = (not tf["ran"]) or tf["ok"]
                        if not tf_clean:
                            audit_text += "\n\n" + render_validation_findings(tf["errors"])
                            log_queue.put(
                                f"__INFO__:terraform validate found {len(tf['errors'])} "
                                f"error(s) (round {round_num})"
                            )

                        write_findings(audit_result, project_path, framework_name)

                        if _is_compliant(audit_text) and tf_clean:
                            log_queue.put(f"__COMPLIANT__:{round_num}")
                            converged = True
                            break
                        else:
                            count = _count_findings(audit_text) + len(tf["errors"])
                            log_queue.put(f"__FINDINGS__:{round_num}:{count}")

                    result_holder["result"]          = gen_result
                    result_holder["auto_iterate_result"] = {
                        "rounds": round_num,
                        "max_rounds": max_rounds,
                        "converged": converged,
                        "last_audit": audit_text,
                    }

                elif is_audit_only:
                    # ── Audit-only single pass ────────────────────────────────
                    crew_result = crew.kickoff()
                    result_holder["result"] = crew_result
                    log_queue.put(f"__ACTIVE__:{findings_idx}:Write Findings")
                    result_holder["findings_result"] = write_findings(
                        crew_result, project_path, framework_name
                    )
                    log_queue.put(f"__DONE__:{findings_idx}:Write Findings")

                else:
                    # ── Normal single pass ────────────────────────────────────
                    crew_result = crew.kickoff()
                    result_holder["result"] = crew_result
                    log_queue.put(f"__ACTIVE__:{git_step_idx}:Git")
                    result_holder["git_result"] = commit_audited_output(
                        crew_result, has_auditor=has_auditor
                    )
                    # Single pass has no fix loop, so terraform validate is
                    # informational here — surface any wiring errors to the user.
                    tf = terraform_validate(project_path)
                    if tf["ran"] and not tf["ok"]:
                        files = ", ".join(sorted({e["file"] for e in tf["errors"] if e["file"]}))
                        log_queue.put(
                            f"__INFO__:⚠️ terraform validate found {len(tf['errors'])} "
                            f"error(s) in {files or 'the config'} — consider auto-iterate to fix."
                        )
                        result_holder["validation"] = tf["errors"]
                    log_queue.put(f"__DONE__:{git_step_idx}:Git")

            except SystemExit:
                result_holder["stopped"] = True
            except Exception as e:
                result_holder["error"] = str(e)
            finally:
                sys.stdout = original_stdout
                root_logger.removeHandler(log_handler)
                root_logger.setLevel(original_level)
                crewai_event_bus.off(AgentExecutionStartedEvent,   on_agent_started)
                crewai_event_bus.off(AgentExecutionCompletedEvent, on_agent_completed)
                crewai_event_bus.off(LLMCallCompletedEvent,         on_llm_completed)
                log_queue.put(None)

        thread = threading.Thread(target=run_crew, daemon=True)
        thread.start()

        st.session_state.pipeline_running       = True
        st.session_state.pipeline_thread        = thread
        st.session_state.pipeline_queue         = log_queue
        st.session_state.pipeline_result_holder = result_holder
        st.session_state.pipeline_labels        = [label for label, _tier, _agent in pipeline]
        st.session_state.active_index           = -1
        st.session_state.pipeline_completed     = set()
        st.session_state.activity_log           = [
            f"Auditing {existing_file_count} file(s) against {framework_name}..."
            if is_audit_only else
            f"Auto-iterating up to {max_rounds} round(s) against {framework_name}..."
            if is_auto_iterate else
            (f"Extending {existing_file_count} existing file(s) in workspace..."
             if existing_file_count else "New project — generating from scratch...")
        ]
        st.session_state.step_count             = 0
        st.session_state.tokens                 = _empty_tokens()
        st.session_state.pipeline_start_time    = time.time()
        st.session_state.pipeline_end_time      = None

    # --- Recover running-pipeline references from session state ---
    log_queue     = st.session_state.pipeline_queue
    result_holder = st.session_state.pipeline_result_holder
    labels        = st.session_state.pipeline_labels

    # --- Pipeline card display ---
    st.markdown("### Pipeline")
    _fw = st.session_state.compliance_framework
    _fw_label  = f"Auditor enforces **{_fw}**." if _fw and _fw != "None" else "No compliance audit."
    _ctx_count = st.session_state.get("_existing_file_count", 0)
    _ctx_label = f"📂 Extending **{_ctx_count}** existing file(s)." if _ctx_count else "✨ New project."
    st.caption(f"🟢 Agents run in sequence on Haiku. {_fw_label} {_ctx_label}")

    MODEL_COLOR = "#10B981"
    MODEL_LABEL = "anthropic/claude-haiku-4-5"

    card_cols = st.columns(len(labels))
    card_placeholders = [(col.empty(), label) for col, label in zip(card_cols, labels)]

    def render_cards(active_idx, completed_set):
        for i, (ph, label) in enumerate(card_placeholders):
            if i in completed_set:
                bg, border, icon, opacity = "#0d1f0d", "#22c55e", "✅", "0.9"
            elif i == active_idx:
                bg, border, icon, opacity = "#0d0d1f", MODEL_COLOR, "⚡", "1.0"
            else:
                bg, border, icon, opacity = "#111", "#2a2a2a", "·", "0.45"
            ph.markdown(
                f"""<div style="background:{bg};border:2px solid {border};border-radius:10px;
                    padding:12px 6px;text-align:center;opacity:{opacity};">
                    <div style="font-size:1.3em;margin-bottom:4px">{icon}</div>
                    <div style="font-weight:700;font-size:0.8em;color:#fff;margin-bottom:2px">{label}</div>
                    <div style="font-size:0.68em;color:{MODEL_COLOR};font-weight:600">🟢 Haiku</div>
                    <div style="font-size:0.6em;color:#555;margin-top:2px">{MODEL_LABEL}</div>
                </div>""",
                unsafe_allow_html=True,
            )

    # --- Stop button ---
    if st.button("⏹ Stop Pipeline", type="secondary"):
        _interrupt_pipeline_thread()
        st.session_state.pipeline_error    = "Pipeline stopped by user."
        st.session_state.pipeline_done     = True
        st.session_state.pipeline_running  = False
        st.session_state.pipeline_end_time = time.time()
        st.rerun()

    # --- Activity feed ---
    st.markdown("### Activity")
    activity_ph = st.empty()

    def render_activity():
        lines = "<br>".join(f"› {m}" for m in st.session_state.activity_log[-6:])
        activity_ph.markdown(
            f"""<div style="background:#0d0d0d;border:1px solid #222;border-radius:8px;
                padding:14px 16px;font-family:monospace;font-size:0.82em;
                color:#aaa;min-height:130px;line-height:1.8">{lines}</div>""",
            unsafe_allow_html=True,
        )

    # --- Drain all queued messages into session state (non-blocking) ---
    pipeline_finished = False
    try:
        while True:
            msg = log_queue.get_nowait()
            if msg is None:
                pipeline_finished = True
                break
            if msg.startswith("__ACTIVE__:"):
                _, idx_str, label = msg.split(":", 2)
                idx = int(idx_str)
                if idx != st.session_state.active_index or idx in st.session_state.pipeline_completed:
                    st.session_state.pipeline_completed.discard(idx)
                    st.session_state.active_index = idx
                    st.session_state.activity_log.append(f"{label} is working...")
            elif msg.startswith("__DONE__:"):
                _, idx_str, label = msg.split(":", 2)
                idx = int(idx_str)
                st.session_state.pipeline_completed.add(idx)
                if st.session_state.active_index == idx:
                    st.session_state.active_index = -1
                st.session_state.activity_log.append(f"{label} complete.")
            elif msg == "__STEP__":
                st.session_state.step_count += 1
                st.session_state.activity_log.append(f"Task {st.session_state.step_count} complete...")
            elif msg.startswith("__ROUND__:"):
                _, rn, rm = msg.split(":", 2)
                st.session_state.active_index       = -1
                st.session_state.pipeline_completed = set()
                st.session_state.step_count         = 0
                st.session_state.activity_log.append(f"━━━ Round {rn} / {rm} ━━━")
            elif msg.startswith("__COMPLIANT__:"):
                rn = msg.split(":", 1)[1]
                st.session_state.activity_log.append(f"✅ Compliant after {rn} round(s).")
            elif msg.startswith("__FINDINGS__:"):
                _, rn, count = msg.split(":", 2)
                st.session_state.activity_log.append(
                    f"Round {rn}: {count} finding(s) — incorporating and retrying..."
                )
            elif msg.startswith("__INFO__:"):
                st.session_state.activity_log.append(msg.split(":", 1)[1])
            elif msg.startswith("__USAGE__:"):
                _, tier, i, o, cr, cc = msg.split(":", 5)
                bucket = st.session_state.tokens.setdefault(
                    tier, {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
                )
                bucket["input"]        += int(i)
                bucket["output"]       += int(o)
                bucket["cache_read"]   += int(cr)
                bucket["cache_create"] += int(cc)
    except queue.Empty:
        pass

    # --- Render current state ---
    render_cards(st.session_state.active_index, st.session_state.pipeline_completed)
    render_activity()

    # --- Live stats (duration + cache) ---
    tokens   = st.session_state.tokens
    cr_total = sum(t["cache_read"]   for t in tokens.values())
    cc_total = sum(t["cache_create"] for t in tokens.values())
    fresh    = sum(t["input"]        for t in tokens.values())
    out      = sum(t["output"]       for t in tokens.values())
    total_in = cr_total + cc_total + fresh
    hit_rate = (cr_total / total_in * 100) if total_in else 0.0
    elapsed  = time.time() - st.session_state.pipeline_start_time
    st.caption(
        f"⏱ **{_fmt_duration(elapsed)}** · "
        f"💾 cache read **{cr_total:,}** · cache write **{cc_total:,}** · "
        f"fresh input **{fresh:,}** · output **{out:,}** · "
        f"hit rate **{hit_rate:.0f}%**"
    )

    # --- Schedule next tick or finalise ---
    if pipeline_finished:
        if "stopped" in result_holder:
            st.session_state.pipeline_error = "Pipeline stopped by user."
        elif "error" in result_holder:
            st.session_state.pipeline_error = result_holder["error"]
        else:
            st.session_state.pipeline_result     = str(result_holder.get("result", ""))
            st.session_state.git_result          = result_holder.get("git_result")
            st.session_state.findings_result     = result_holder.get("findings_result")
            st.session_state.auto_iterate_result = result_holder.get("auto_iterate_result")
        st.session_state.pipeline_done     = True
        st.session_state.pipeline_running  = False
        st.session_state.pipeline_end_time = time.time()
        st.rerun()
    else:
        time.sleep(0.5)
        st.rerun()
