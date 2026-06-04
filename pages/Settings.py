import streamlit as st
from config import (
    USE_BEDROCK, AWS_PROFILE, AWS_REGION,
    TIER_OPTIONS, DEFAULT_MODEL_ASSIGNMENTS, ROLE_LABELS,
    BEDROCK_MODEL_IDS, ANTHROPIC_MODEL_IDS,
    inject_bedrock_credentials,
)

st.set_page_config(page_title="Settings — Adaptive Code Builder", page_icon="⚙️", layout="wide")
st.title("⚙️ Settings")

# Initialise session state defaults (shared with app.py via Streamlit's global session).
if "model_assignments" not in st.session_state:
    st.session_state.model_assignments = dict(DEFAULT_MODEL_ASSIGNMENTS)


# ──────────────────────────────────────────────
# LLM Provider
# ──────────────────────────────────────────────
st.header("LLM Provider")
if USE_BEDROCK:
    profile_label = f"`{AWS_PROFILE}`" if AWS_PROFILE else "default profile"
    st.info(f"🟠 **AWS Bedrock** — profile: {profile_label} · region: `{AWS_REGION}`")
    st.caption("To change provider or profile, update `LLM_PROVIDER` and `AWS_PROFILE` in your `.env` file and restart the app.")
    if st.button("🔑 Refresh AWS Credentials", help="Re-resolve SSO credentials if your session has expired."):
        try:
            inject_bedrock_credentials()
            st.success("Credentials refreshed successfully.")
        except Exception as e:
            hint = (
                f"Run `aws sso login --profile {AWS_PROFILE}` in your terminal."
                if AWS_PROFILE else "Run `aws sso login` in your terminal."
            )
            st.error(f"{e}\n\n{hint}")
else:
    st.info("🟣 **Anthropic direct API**")
    st.caption("To switch to Bedrock, set `LLM_PROVIDER=bedrock` and `AWS_PROFILE=<profile>` in your `.env` file and restart the app.")

st.divider()

# ──────────────────────────────────────────────
# Model Assignments
# ──────────────────────────────────────────────
st.header("Model Assignments")
st.caption(
    "Choose which model tier each pipeline role uses. "
    "Changes take effect on the next pipeline run — no restart needed."
)

model_ids = BEDROCK_MODEL_IDS if USE_BEDROCK else ANTHROPIC_MODEL_IDS

ma = st.session_state.model_assignments
cols = st.columns(3)
for i, (role_key, role_label) in enumerate(ROLE_LABELS.items()):
    with cols[i % 3]:
        current = ma.get(role_key, DEFAULT_MODEL_ASSIGNMENTS[role_key])
        chosen = st.selectbox(
            role_label,
            options=TIER_OPTIONS,
            index=TIER_OPTIONS.index(current) if current in TIER_OPTIONS else 0,
            key=f"settings_ma_{role_key}",
        )
        ma[role_key] = chosen
        st.caption(f"`{model_ids.get(chosen, chosen)}`")

st.divider()
col1, col2 = st.columns([1, 5])
with col1:
    if st.button("Reset to defaults", use_container_width=True):
        st.session_state.model_assignments = dict(DEFAULT_MODEL_ASSIGNMENTS)
        st.rerun()

st.divider()

# ──────────────────────────────────────────────
# How to add a new model tier
# ──────────────────────────────────────────────
with st.expander("ℹ️ How to add a new model tier"):
    st.markdown("""
1. Open **`config.py`** and add the new model's ID to both `BEDROCK_MODEL_IDS` and `ANTHROPIC_MODEL_IDS`.
2. Append the tier key to `TIER_OPTIONS` (controls display order in the selectboxes above).
3. Add a hex color to `TIER_COLORS` and an emoji to `TIER_ICONS` (used on pipeline cards).
4. Optionally update `DEFAULT_MODEL_ASSIGNMENTS` to make it the default for a role.

No other files need to change.
""")
