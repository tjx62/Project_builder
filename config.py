"""
Shared configuration imported by both app.py and pages/Settings.py.

All provider detection, model ID maps, and default role assignments live here
so neither page needs to import from the other.
"""
import os
from pathlib import Path
from dotenv import load_dotenv, set_key, unset_key

DOTENV_PATH = str(Path(__file__).parent / ".env")
ORG_CONTEXT_PATH = Path(__file__).parent / "org_context.md"
STREAMLIT_CONFIG_PATH = Path(__file__).parent / ".streamlit" / "config.toml"


def read_dark_mode() -> bool:
    """Return True if dark mode is configured (default True)."""
    if not STREAMLIT_CONFIG_PATH.exists():
        return True
    import tomllib
    with open(STREAMLIT_CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("theme", {}).get("base", "dark") == "dark"


def write_dark_mode(dark: bool) -> None:
    """Persist dark/light mode preference to .streamlit/config.toml."""
    import re
    STREAMLIT_CONFIG_PATH.parent.mkdir(exist_ok=True)
    base = "dark" if dark else "light"
    content = STREAMLIT_CONFIG_PATH.read_text() if STREAMLIT_CONFIG_PATH.exists() else ""
    if "[theme]" in content:
        if re.search(r'base\s*=', content):
            content = re.sub(r'base\s*=\s*["\']?\w+["\']?', f'base = "{base}"', content)
        else:
            content = content.replace("[theme]", f'[theme]\nbase = "{base}"')
    else:
        content += f'\n[theme]\nbase = "{base}"\n'
    STREAMLIT_CONFIG_PATH.write_text(content)


def read_prompt_caching() -> bool:
    """Return True if prompt caching is enabled (default False)."""
    return os.environ.get("ANTHROPIC_PROMPT_CACHING", "0") == "1"


def write_prompt_caching(enabled: bool) -> None:
    """Persist prompt caching preference to .env and reload into os.environ."""
    set_key(DOTENV_PATH, "ANTHROPIC_PROMPT_CACHING", "1" if enabled else "0")
    load_dotenv(DOTENV_PATH, override=True)
    os.environ["ANTHROPIC_PROMPT_CACHING"] = "1" if enabled else "0"


load_dotenv(DOTENV_PATH)

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
    "planner":              "haiku",
    "architect":            "haiku",
    "specialist":           "haiku",
    "terraform_specialist": "haiku",
    "auditor":              "haiku",
    "wiring_reviewer":      "haiku",
    "remediation":          "haiku",
}

ROLE_LABELS: dict[str, str] = {
    "planner":              "Planner",
    "architect":            "Architect",
    "specialist":           "Specialists",
    "terraform_specialist": "Terraform Specialist",
    "auditor":              "Auditor",
    "wiring_reviewer":      "Wiring Reviewer",
    "remediation":          "Remediation Eng.",
}

# Human-readable labels for each provider type shown in the UI.
PROVIDER_LABELS = {
    "anthropic":   "🟣 Anthropic",
    "bedrock_sso": "🟠 Bedrock (SSO)",
    "bedrock_keys": "🟠 Bedrock (Keys)",
}


def detect_provider_type() -> str:
    """Derive the provider type from current environment variables."""
    if os.environ.get("LLM_PROVIDER", "anthropic").lower() == "bedrock":
        if os.environ.get("AWS_ACCESS_KEY_ID") and not os.environ.get("AWS_PROFILE"):
            return "bedrock_keys"
        return "bedrock_sso"
    return "anthropic"


def load_credentials_from_env() -> dict:
    """Return a credentials dict populated from current environment variables."""
    return {
        "api_key":            os.environ.get("ANTHROPIC_API_KEY", ""),
        "aws_profile":        os.environ.get("AWS_PROFILE", ""),
        "aws_region":         os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        "aws_access_key_id":  os.environ.get("AWS_ACCESS_KEY_ID", ""),
        "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        "aws_session_token":  os.environ.get("AWS_SESSION_TOKEN", ""),
    }


def write_env_provider(provider_type: str, credentials: dict) -> None:
    """Persist provider + credentials to the .env file and reload into os.environ."""
    path = DOTENV_PATH

    if provider_type == "anthropic":
        set_key(path, "LLM_PROVIDER", "anthropic")
        if credentials.get("api_key"):
            set_key(path, "ANTHROPIC_API_KEY", credentials["api_key"])
        unset_key(path, "AWS_PROFILE")

    elif provider_type == "bedrock_sso":
        set_key(path, "LLM_PROVIDER", "bedrock")
        if credentials.get("aws_profile"):
            set_key(path, "AWS_PROFILE", credentials["aws_profile"])
        if credentials.get("aws_region"):
            set_key(path, "AWS_DEFAULT_REGION", credentials["aws_region"])
        unset_key(path, "AWS_ACCESS_KEY_ID")
        unset_key(path, "AWS_SECRET_ACCESS_KEY")
        unset_key(path, "AWS_SESSION_TOKEN")

    elif provider_type == "bedrock_keys":
        set_key(path, "LLM_PROVIDER", "bedrock")
        unset_key(path, "AWS_PROFILE")
        if credentials.get("aws_access_key_id"):
            set_key(path, "AWS_ACCESS_KEY_ID", credentials["aws_access_key_id"])
        if credentials.get("aws_secret_access_key"):
            set_key(path, "AWS_SECRET_ACCESS_KEY", credentials["aws_secret_access_key"])
        if credentials.get("aws_session_token"):
            set_key(path, "AWS_SESSION_TOKEN", credentials["aws_session_token"])
        else:
            unset_key(path, "AWS_SESSION_TOKEN")
        if credentials.get("aws_region"):
            set_key(path, "AWS_DEFAULT_REGION", credentials["aws_region"])

    load_dotenv(path, override=True)


def inject_credentials_from_config(provider_type: str, credentials: dict) -> None:
    """Inject the right credentials into os.environ for the given provider type.

    For bedrock_sso: resolves SSO/boto3 temporary credentials.
    For bedrock_keys: sets env vars directly from the dict.
    For anthropic: no-op (API key is passed directly to LLM objects).
    """
    if provider_type == "bedrock_sso":
        import boto3
        profile = credentials.get("aws_profile") or None
        region  = credentials.get("aws_region") or "us-east-1"
        session = boto3.Session(profile_name=profile, region_name=region)
        creds = session.get_credentials()
        if creds is None:
            hint = (
                f"Run `aws sso login --profile {profile}`."
                if profile
                else "Configure AWS credentials or set AWS_PROFILE in .env."
            )
            raise RuntimeError(f"No AWS credentials found. {hint}")
        frozen = creds.get_frozen_credentials()
        os.environ["AWS_ACCESS_KEY_ID"]     = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        os.environ["AWS_DEFAULT_REGION"]    = region
        if frozen.token:
            os.environ["AWS_SESSION_TOKEN"] = frozen.token
        elif "AWS_SESSION_TOKEN" in os.environ:
            del os.environ["AWS_SESSION_TOKEN"]

    elif provider_type == "bedrock_keys":
        region = credentials.get("aws_region") or "us-east-1"
        if credentials.get("aws_access_key_id"):
            os.environ["AWS_ACCESS_KEY_ID"]     = credentials["aws_access_key_id"]
        if credentials.get("aws_secret_access_key"):
            os.environ["AWS_SECRET_ACCESS_KEY"] = credentials["aws_secret_access_key"]
        os.environ["AWS_DEFAULT_REGION"] = region
        if credentials.get("aws_session_token"):
            os.environ["AWS_SESSION_TOKEN"] = credentials["aws_session_token"]
        elif "AWS_SESSION_TOKEN" in os.environ:
            del os.environ["AWS_SESSION_TOKEN"]


def inject_bedrock_credentials() -> None:
    """Legacy helper — resolves SSO credentials from the env-configured profile."""
    inject_credentials_from_config("bedrock_sso", {
        "aws_profile": AWS_PROFILE,
        "aws_region":  AWS_REGION,
    })
