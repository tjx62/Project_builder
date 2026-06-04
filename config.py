"""
Shared configuration imported by both app.py and pages/Settings.py.

All provider detection, model ID maps, and default role assignments live here
so neither page needs to import from the other.
"""
import os
from dotenv import load_dotenv

load_dotenv()

USE_BEDROCK = os.environ.get("LLM_PROVIDER", "anthropic").lower() == "bedrock"
AWS_PROFILE = os.environ.get("AWS_PROFILE") or None
AWS_REGION  = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# To add a new model tier:
#   1. Add its key + model string to both dicts below.
#   2. Append the key to TIER_OPTIONS.
#   3. Add a color + icon entry in TIER_COLORS / TIER_ICONS.
#   4. Optionally set it as the default for a role in DEFAULT_MODEL_ASSIGNMENTS.
BEDROCK_MODEL_IDS = {
    "haiku":  "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "bedrock/us.anthropic.claude-sonnet-4-6",
    "opus":   "bedrock/us.anthropic.claude-opus-4-8",
}
ANTHROPIC_MODEL_IDS = {
    "haiku":  "anthropic/claude-haiku-4-5-20251001",
    "sonnet": "anthropic/claude-sonnet-4-6",
    "opus":   "anthropic/claude-opus-4-7",
}

TIER_OPTIONS = ["haiku", "sonnet", "opus"]
TIER_COLORS  = {"haiku": "#10B981", "sonnet": "#6366F1", "opus": "#F59E0B"}
TIER_ICONS   = {"haiku": "🟢",      "sonnet": "🟣",      "opus": "🟠"}

DEFAULT_MODEL_ASSIGNMENTS: dict[str, str] = {
    "planner":         "haiku",
    "architect":       "opus",
    "specialist":      "sonnet",
    "auditor":         "opus",
    "wiring_reviewer": "sonnet",
    "remediation":     "sonnet",
}

ROLE_LABELS: dict[str, str] = {
    "planner":         "Planner",
    "architect":       "Architect",
    "specialist":      "Specialists",
    "auditor":         "Auditor",
    "wiring_reviewer": "Wiring Reviewer",
    "remediation":     "Remediation Eng.",
}


def inject_bedrock_credentials() -> None:
    """Resolve AWS SSO credentials via boto3 and export them for LiteLLM.

    Raises RuntimeError with a helpful message if the SSO session has expired.
    """
    import boto3
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    creds = session.get_credentials()
    if creds is None:
        hint = (
            f"Run `aws sso login --profile {AWS_PROFILE}`."
            if AWS_PROFILE
            else "Configure AWS credentials or set AWS_PROFILE in .env."
        )
        raise RuntimeError(f"No AWS credentials found. {hint}")
    frozen = creds.get_frozen_credentials()
    os.environ["AWS_ACCESS_KEY_ID"]     = frozen.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    os.environ["AWS_DEFAULT_REGION"]    = AWS_REGION
    if frozen.token:
        os.environ["AWS_SESSION_TOKEN"] = frozen.token
    elif "AWS_SESSION_TOKEN" in os.environ:
        del os.environ["AWS_SESSION_TOKEN"]
