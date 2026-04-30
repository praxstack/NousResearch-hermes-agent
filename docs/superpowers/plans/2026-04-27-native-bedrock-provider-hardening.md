# Native Bedrock Provider Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hermes' native Amazon Bedrock provider Cline-grade or better for API-key, profile, credentials, and default-chain usage, without proxy/OpenAI/custom-provider baggage.

**Architecture:** Keep one canonical provider, `bedrock`. Resolve Bedrock auth once into a typed auth config, then pass that config into all Bedrock runtime, discovery, auxiliary, and AnthropicBedrock client creation paths. Remove legacy proxy/mantle repair from this CR and test native provider behavior directly.

**Tech Stack:** Python, boto3/botocore, AnthropicBedrock SDK, Hermes runtime provider/config CLI, pytest.

---

## Scope

In scope:
- Native `provider: bedrock` setup and runtime behavior.
- Auth methods: `api_key`, `profile`, `credentials`, `default_chain`.
- Strict API-key handling from `AWS_BEARER_TOKEN_BEDROCK`.
- Auth-aware Bedrock client cache and stale-client eviction.
- Claude Bedrock route and non-Claude Converse route.
- Converse streaming parity for text, tools, usage, and reasoning.
- Fresh setup docs and tests.
- Cline-grade Bedrock setup UX: auth method, region, model variant, inference-profile behavior, prompt caching, thinking effort, and advanced settings are visible/configurable without users memorizing Bedrock internals.

Out of scope:
- `bedrock-mantle`.
- `bedrock-native`.
- `localhost:8881`.
- proxy fallback.
- legacy auto-repair.
- migration of local broken configs.

## Files

- Modify: `agent/bedrock_adapter.py`
  - Own Bedrock auth resolution, client construction, cache keys, cache eviction, Converse streaming normalization, model discovery.
- Modify: `agent/anthropic_adapter.py`
  - Ensure Claude-on-Bedrock uses the same resolved auth contract.
- Modify: `agent/auxiliary_client.py`
  - Ensure auxiliary Bedrock client creation uses the shared auth path.
- Modify: `hermes_cli/auth.py`
  - Keep Bedrock as a native provider with `AWS_BEARER_TOKEN_BEDROCK`.
- Modify: `hermes_cli/main.py`
  - Keep fresh Bedrock setup flow; remove legacy repair prompt/path; expose Bedrock setup choices clearly.
- Modify: `hermes_cli/config.py`
  - Remove legacy Bedrock repair helpers and legacy provider cleanup.
- Modify: `hermes_cli/doctor.py`
  - Remove legacy Bedrock repair checks.
- Modify: `hermes_cli/runtime_provider.py`
  - Remove legacy rejection path; keep native Bedrock runtime resolution only.
- Modify: `agent/model_metadata.py`
  - Remove duplicate Bedrock context branch.
- Modify: `website/docs/guides/aws-bedrock.md`
  - Document fresh native setup only.
- Modify: `tests/agent/test_bedrock_adapter.py`
  - Add/repair auth, cache, streaming, reasoning, and model-ID tests.
- Modify: `tests/agent/test_bedrock_integration.py`
  - Keep provider integration tests focused on native Bedrock only.
- Modify: `tests/hermes_cli/test_bedrock_model_flow.py`
  - Remove legacy repair tests; keep fresh setup/model/settings tests.
- Modify: `tests/hermes_cli/test_config.py`
  - Remove legacy proxy cleanup tests unless still needed for non-Bedrock config hygiene.
- Modify: `tests/hermes_cli/test_doctor.py`
  - Remove legacy repair assertions; keep built-in Bedrock provider checks.

## Task 1: Remove Legacy Proxy/Mantle Surface

- [ ] **Step 1: Write failing search expectation**

Run:
```bash
rg -n "bedrock-mantle|bedrock-native|localhost:8881|127\\.0\\.0\\.1:8881|repair_legacy_bedrock|is_legacy_bedrock" agent hermes_cli tests website/docs/guides/aws-bedrock.md
```

Expected before fix: matches exist.
Expected after fix: no matches.

- [ ] **Step 2: Delete legacy repair code**

Remove:
- `is_legacy_bedrock_custom_config`
- `repair_legacy_bedrock_custom_config`
- `_repair_legacy_bedrock_custom_config_interactive`
- runtime legacy rejection helper
- doctor legacy repair helper
- docs/tests mentioning `bedrock-mantle`, `bedrock-native`, or `localhost:8881`

- [ ] **Step 3: Run native setup tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/hermes_cli/test_bedrock_model_flow.py tests/hermes_cli/test_doctor.py tests/hermes_cli/test_config.py
```

Expected: legacy tests are gone; fresh Bedrock setup tests pass.

## Task 2: Make Bedrock Auth Config Authoritative

- [ ] **Step 1: Add failing tests**

In `tests/agent/test_bedrock_adapter.py`, add tests proving:
- `api_key` mode passes the resolved bearer token into client creation.
- `profile` mode ignores bearer token and explicit credentials.
- `credentials` mode ignores bearer token and profile.
- `default_chain` ignores bearer token unless `api_key` mode is selected.

- [ ] **Step 2: Refactor client factory**

In `agent/bedrock_adapter.py`, make `_create_bedrock_client()` use `auth_config` as the only source of truth.

Required behavior:
- `profile`: create `boto3.Session(profile_name=auth_config["profile"])`.
- `credentials`: call `boto3.client(..., aws_access_key_id=..., aws_secret_access_key=..., aws_session_token=...)`.
- `default_chain`: call normal boto3 default chain with no bearer-token influence.
- `api_key`: prefer explicit botocore bearer-token configuration if available; otherwise use a small locked env bridge that writes `AWS_BEARER_TOKEN_BEDROCK=auth_config["api_key"]` only during client construction and restores immediately.

- [ ] **Step 3: Remove broad process-global masking**

Delete or narrow `_masked_aws_env()` so it is not the primary isolation mechanism for every auth mode.

- [ ] **Step 4: Run auth tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/agent/test_bedrock_adapter.py -k "auth or client"
```

Expected: all auth/client tests pass.

## Task 3: Fix Auth-Aware Cache Eviction

- [ ] **Step 1: Repair stale-cache tests**

Update the stale-cache tests to seed cache entries with real auth-aware keys such as:
```python
"bedrock-runtime:us-east-1:api_key:api_key:abcd1234"
```

- [ ] **Step 2: Fix eviction**

Change `invalidate_runtime_client(region)` to remove all runtime cache entries that start with:
```python
f"bedrock-runtime:{region}:"
```

Return `True` if at least one entry was removed.

- [ ] **Step 3: Run stale-cache tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/agent/test_bedrock_adapter.py::TestCallConverseInvalidatesOnStaleError tests/agent/test_bedrock_adapter.py -k "invalidate_runtime_client"
```

Expected: all stale-cache tests pass.

## Task 4: Preserve Converse Reasoning

- [ ] **Step 1: Add failing stream reasoning test**

In `tests/agent/test_bedrock_adapter.py`, add a test where a Converse stream contains:
```python
{"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking..."}}}}
```

Assert:
- `on_reasoning_delta` receives `"thinking..."`.
- returned `response.choices[0].message.reasoning` or `reasoning_content` contains `"thinking..."`.

- [ ] **Step 2: Implement accumulation**

In `stream_converse_with_callbacks()`, accumulate reasoning chunks into a list and attach joined text to the returned message:
```python
msg.reasoning = "\n".join(reasoning_parts) if reasoning_parts else None
msg.reasoning_content = msg.reasoning
```

- [ ] **Step 3: Run stream tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/agent/test_bedrock_adapter.py -k "stream or reasoning"
```

Expected: streaming and reasoning tests pass.

## Task 5: Define Cline-Grade Bedrock Setup UX and Config Contract

- [ ] **Step 1: Add failing setup UX tests**

In `tests/hermes_cli/test_bedrock_model_flow.py`, add tests proving a fresh Bedrock setup can capture:
- auth method: `api_key`, `profile`, `credentials`, or `default_chain`
- region
- model selection from Hermes-known Bedrock model aliases and variants
- cross-region inference preference
- global inference profile preference
- prompt caching preference where supported
- thinking/adaptive reasoning effort where supported
- optional custom VPC endpoint only when explicitly configured

- [ ] **Step 2: Define saved config keys**

Fresh setup must save only native Bedrock state:
```yaml
model:
  provider: bedrock
  base_url: https://bedrock-runtime.<region>.amazonaws.com
  default: <resolved-bedrock-model-or-profile-id>
  reasoning_effort: <none|low|medium|high>
bedrock:
  region: <region>
  auth_method: <selected>
  use_cross_region_inference: <bool>
  use_global_inference_profile: <bool>
  prompt_caching: <bool>
  vpc_endpoint_url: <optional-url>
```

Do not require users to know when `global.` or `us.` prefixes are required. The setup/model resolver owns that mapping.

- [ ] **Step 3: Implement visible advanced settings**

The Bedrock setup flow must make these choices visible in the advanced Bedrock path:
- auth method-specific inputs
- region
- model variant
- inference profile mode: direct, regional cross-region, or global
- prompt caching
- thinking effort
- VPC endpoint URL

This does not require building a new rich UI. It requires the CLI/setup prompts and saved config to expose the same decisions users see in Cline-like integrations.

- [ ] **Step 4: Run setup UX tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/hermes_cli/test_bedrock_model_flow.py -k "bedrock and (setup or advanced or model)"
```

Expected: fresh setup tests pass without writing OpenAI/custom/proxy values.

## Task 6: Tighten Model IDs and Bedrock Settings Contract

- [ ] **Step 1: Add/verify model-ID tests**

Tests must cover:
- raw Anthropic Bedrock ID where direct invocation is valid
- global inference profile ID
- regional inference profile ID
- no user requirement to manually type provider prefixes for common models

- [ ] **Step 2: Verify setup flow saves native config only**

Fresh setup must save:
```yaml
model:
  provider: bedrock
  base_url: https://bedrock-runtime.<region>.amazonaws.com
bedrock:
  region: <region>
  auth_method: <selected>
```

It must not save:
```text
OPENAI_API_KEY
OPENAI_BASE_URL
bedrock-mantle
localhost:8881
```

- [ ] **Step 3: Run setup/model tests**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/hermes_cli/test_bedrock_model_flow.py tests/agent/test_bedrock_integration.py
```

Expected: native setup and model routing tests pass.

## Task 7: Remove Duplicate Context Logic and Update Docs

- [ ] **Step 1: Remove duplicate Bedrock context branch**

In `agent/model_metadata.py`, keep one Bedrock static context branch before generic custom endpoint probing.

- [ ] **Step 2: Update docs**

In `website/docs/guides/aws-bedrock.md`, document:
- native provider only
- supported auth methods
- region/model selection
- advanced settings
- no OpenAI/OpenRouter/proxy dependency

Do not mention legacy proxy migration.

- [ ] **Step 3: Run docs/search checks**

Run:
```bash
rg -n "bedrock-mantle|bedrock-native|localhost:8881|127\\.0\\.0\\.1:8881|repair_legacy_bedrock|is_legacy_bedrock" agent hermes_cli tests website/docs/guides/aws-bedrock.md
```

Expected: no matches.

## Task 8: Full Verification

- [ ] **Step 1: Run targeted Bedrock suite**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_integration.py tests/agent/test_anthropic_adapter.py tests/agent/test_auxiliary_client.py tests/hermes_cli/test_bedrock_model_flow.py tests/hermes_cli/test_doctor.py tests/hermes_cli/test_config.py
```

Expected: pass.

- [ ] **Step 2: Run provider/runtime smoke suite**

Run:
```bash
./venv/bin/python -m pytest -q -o addopts='' tests/hermes_cli/test_runtime_provider_resolution.py tests/run_agent/test_streaming.py tests/run_agent/test_run_agent.py
```

Expected: pass, or unrelated failures documented with exact failing tests.

- [ ] **Step 3: Inspect final diff**

Run:
```bash
git diff -- agent/bedrock_adapter.py agent/anthropic_adapter.py agent/auxiliary_client.py hermes_cli/auth.py hermes_cli/main.py hermes_cli/config.py hermes_cli/doctor.py hermes_cli/runtime_provider.py agent/model_metadata.py website/docs/guides/aws-bedrock.md tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_integration.py tests/hermes_cli/test_bedrock_model_flow.py
```

Expected: no legacy proxy repair code; auth/client/streaming changes are scoped to native Bedrock.

## Self-Review

- Spec coverage: covers legacy removal, auth source of truth, cache eviction, streaming reasoning, setup UX, model/settings contract, duplicate context cleanup, docs, and verification.
- Placeholder scan: no TODO/TBD placeholders.
- Type consistency: plan consistently uses `BedrockAuthConfig`, `resolve_bedrock_auth_config`, `_create_bedrock_client`, `invalidate_runtime_client`, and `stream_converse_with_callbacks`.

## Plan Design Review

Verdict: revise before implementation, then proceed.

Scores:
- Setup UX clarity: 8/10 after adding Task 5. It now names the exact Bedrock choices users must see instead of hiding them behind model IDs.
- User control: 8/10. Auth method, region, model variant, inference profile mode, prompt caching, thinking effort, and VPC endpoint are explicit.
- Fresh-user fit: 9/10. The plan avoids legacy migration and treats this as a clean native provider CR.
- Cline parity: 8/10. The plan now covers the same visible choices as Cline. Implementation still needs proof from tests and live smoke.
- Risk control: 8/10. The plan attacks the known P1/P2/P3 review findings and removes proxy/OpenAI/custom-provider ambiguity.

Required design revision already applied:
- Added Task 5 so the Bedrock setup UX/config contract is first-class.

Remaining design risk:
- Hermes may not currently have a rich picker primitive. If so, the implementation should still expose these choices through the existing CLI/setup flow and persisted config, without inventing a new UI in this CR.

## Handoff

After `plan-design-review`, implementation should proceed with tests first. Preferred execution mode: subagent-driven per task, with parent review after each task.
