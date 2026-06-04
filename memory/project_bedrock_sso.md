---
name: project-bedrock-sso
description: Bedrock SSO support implemented in app.py — LLM_PROVIDER=bedrock + AWS_PROFILE wires everything; billing still blocked as of 2026-06-03
metadata:
  type: project
---

Bedrock SSO support is fully wired in `app.py`. Set `LLM_PROVIDER=bedrock` and `AWS_PROFILE` in `.env` to activate.

**Why:** Customer wants to route LLM calls through AWS Bedrock rather than direct Anthropic API. Bedrock is currently blocked by an AWS billing issue, but the code is ready.

**How to apply:** When billing is resolved, no code changes needed — just `.env` changes. The `_inject_bedrock_credentials()` function uses boto3 to resolve SSO credentials and exports them as `AWS_*` env vars for LiteLLM. Credentials are refreshed at startup and before each pipeline run. A sidebar button allows mid-session refresh without restarting.
