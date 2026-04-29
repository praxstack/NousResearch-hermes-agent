# Bedrock Provider Parity: OpenClaw + Pi + Hermes

**Status:** Draft for review
**Author:** Prax (w/ Claude)
**Date:** 2026-04-29
**Related:** `docs/superpowers/plans/2026-04-27-native-bedrock-provider-hardening.md` (Hermes CR)

---

## 1. Problem Statement

Three local coding-agent tools must reach 1:1 feature parity with Cline's Amazon Bedrock provider:

1. **Hermes** (`~/.hermes/hermes-agent`) — already on branch `feat/native-bedrock-provider-20260428`. Needs verification that recent commits cover the full Cline feature set and that the post-update patch guard still matches HEAD.
2. **OpenClaw** (source: `github.com/openclaw/openclaw`, install: `/opt/homebrew/lib/node_modules/openclaw`) — has a partial `amazon-bedrock` extension. Needs Cline-parity hardening + upstream PR.
3. **Pi** (source: `github.com/badlogic/pi-mono`, install: `/opt/homebrew/lib/node_modules/@mariozechner/pi-coding-agent`) — has a partial `amazon-bedrock` provider in `packages/ai`. Needs Cline-parity hardening + upstream PR.

Each tool must:
- Support the same four AWS auth modes Cline exposes (API key, AWS Profile, AWS Credentials, Default Chain).
- Expose the same four toggles (custom VPC endpoint, cross-region inference, global inference profile, prompt caching).
- Support adaptive thinking at all supported levels (None / Low / Medium / High) on Opus 4.6, Opus 4.7, Sonnet 4.6, plus any future Claude 4.x model whose metadata declares reasoning support.
- Support streaming for text, tool use, and reasoning content.
- Support Opus 4.7's `context-1m-2025-08-07` beta flag with `:1m` model-ID routing.
- Persist a canonical `BedrockAuthConfig` shape identical across all three tools.

Additionally, locally-applied modifications must survive `npm update`, `hermes update`, and `openclaw update` via a Hermes-style post-update patch-reapply guard.

## 2. Goals and Non-Goals

### In scope

- **G1:** Fork OpenClaw → branch `feat/bedrock-cline-parity-20260429`. Bring to Cline parity. Open upstream PR.
- **G2:** Fork pi-mono → branch `feat/bedrock-cline-parity-20260429`. Bring to Cline parity. Open upstream PR.
- **G3:** Audit Hermes' existing CR against Cline parity; close any remaining gaps.
- **G4:** Extend `~/.hermes/hooks/post-update-patches/handler.py` (or add a sibling hook) to re-apply and integrity-check local modifications to installed OpenClaw and Pi JS bundles after every `npm -g upgrade`.
- **G5:** Document a canonical `BedrockAuthConfig` schema used by all three tools (Python / TypeScript) and a migration script that converts pre-parity configs into the new shape.
- **G6:** Maintain ≥95% test coverage for Bedrock code paths in each tool; add failing tests for every gap before fixing it (TDD per Hermes' existing spec conventions).

### Out of scope

- **O1:** Non-Claude Bedrock models (Nova, Mistral, Llama, Cohere, Titan) beyond whatever the existing provider already supports. We pass through but don't add new feature work for them.
- **O2:** Bedrock Agents, Knowledge Bases, or Guardrails APIs. This spec covers **inference only** (Converse + ConverseStream).
- **O3:** AWS SSO login flows. Rely on `aws sso login` run out-of-band; the provider reads the resolved credential chain.
- **O4:** `bedrock-mantle` legacy proxy path (already removed from Hermes; we do not reintroduce).
- **O5:** New UI surface area beyond matching Cline's existing controls. Pi is TUI-only — its "UI" is the interactive-mode selector; OpenClaw adds CLI setup prompts, not a webview.
- **O6:** Bedrock's provisioned-throughput workflow. Users who need it can paste a provisioned-throughput model ID manually; no first-class UX.

## 3. System Overview

Three implementations, one contract:

```
┌─────────────────────────────────────────────────────────────────────┐
│                      BedrockAuthConfig (contract)                   │
│  mode | region | api_key | profile | access_key_id | secret_access_key
│  session_token | vpc_endpoint_url | use_cross_region_inference      │
│  use_global_inference_profile | prompt_caching | adaptive_thinking  │
│  enable_1m_context                                                  │
└─────────────────────────────────────────────────────────────────────┘
          │                        │                        │
          ▼                        ▼                        ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Hermes (Python)  │    │ OpenClaw (TS)    │    │ Pi (TS)          │
│ agent/bedrock_   │    │ extensions/      │    │ packages/ai/src/ │
│   adapter.py     │    │   amazon-bedrock │    │   providers/     │
│ agent/transports/│    │   /index.ts      │    │   amazon-bedrock │
│   bedrock.py     │    │   /discovery.ts  │    │   .ts            │
│ hermes_cli/      │    │   /setup-api.ts  │    │ coding-agent/src/│
│   main.py        │    │ cli/.../Bedrock* │    │   modes/         │
│                  │    │                  │    │   interactive/   │
│ boto3 +          │    │ @aws-sdk/client- │    │ @aws-sdk/client- │
│ anthropic-bedrock│    │   bedrock-runtime│    │   bedrock-runtime│
└──────────────────┘    └──────────────────┘    └──────────────────┘
          │                        │                        │
          └────────────────────────┼────────────────────────┘
                                   ▼
                  ┌──────────────────────────────────┐
                  │   AWS Bedrock Runtime API        │
                  │   Converse / ConverseStream      │
                  │   + ListFoundationModels         │
                  │   + ListInferenceProfiles        │
                  └──────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│            Post-Update Patch Guard (Hermes-style)                    │
│  ~/.hermes/hooks/post-update-patches/handler.py                      │
│   • Verifies marker strings in installed OpenClaw/Pi dist bundles    │
│   • Re-applies text-anchor patches if markers missing                │
│   • Logs + recovery note if reapply fails                            │
│  Triggered by: gateway:startup hook on every Hermes boot             │
└──────────────────────────────────────────────────────────────────────┘
```

**External dependencies:**

- **Python (Hermes):** `boto3 >=1.34`, `botocore >=1.34`, `anthropic[bedrock] >=0.39`, `PyYAML`.
- **TypeScript (OpenClaw, Pi):** `@aws-sdk/client-bedrock-runtime >=3.798`, `@aws-sdk/client-bedrock >=3.798` (for `ListFoundationModels`, `ListInferenceProfiles`), `@smithy/node-http-handler`, `@smithy/types`, `proxy-agent` (optional, for `HTTP_PROXY` env).

## 4. Core Domain Model

### 4.1 `BedrockAuthConfig` (shared contract)

| Field                              | Type                                                                       | Default             | Validation                                                                 |
| ---------------------------------- | -------------------------------------------------------------------------- | ------------------- | -------------------------------------------------------------------------- |
| `mode`                             | `"api_key" \| "profile" \| "credentials" \| "default_chain"`               | `"default_chain"`   | Required. Any other value → `bedrock_auth_mode_invalid` error.             |
| `region`                           | `string`                                                                   | `"us-east-1"`       | Must match `^[a-z]{2}-[a-z]+-\d+$` OR be the literal value in `AWS_REGION`.|
| `api_key`                          | `string \| null`                                                           | `null`              | Required when `mode=="api_key"`. Stripped of leading/trailing whitespace. Must NOT start with `Bearer `. Error if empty after strip. |
| `profile`                          | `string \| null`                                                           | `null`              | Required when `mode=="profile"`. Must match `^[A-Za-z0-9_-]{1,128}$`.      |
| `access_key_id`                    | `string \| null`                                                           | `null`              | Required when `mode=="credentials"`. Must match `^AKIA\|ASIA[A-Z0-9]{16}$` OR any value for non-AWS-managed keys. |
| `secret_access_key`                | `string \| null`                                                           | `null`              | Required when `mode=="credentials"`. Length ≥ 1.                           |
| `session_token`                    | `string \| null`                                                           | `null`              | Optional in `credentials` mode. Required for `ASIA` temporary keys.        |
| `vpc_endpoint_url`                 | `string \| null`                                                           | `null`              | If set, must match `^https://`. Overrides standard `bedrock-runtime.<region>.amazonaws.com`. |
| `use_cross_region_inference`       | `boolean`                                                                  | `true`              | When `true`, picker injects regional prefix (`us.`, `eu.`, etc.) into Claude model IDs. |
| `use_global_inference_profile`     | `boolean`                                                                  | `true`              | When `true` AND `use_cross_region_inference==true`, picker injects `global.` prefix and strips regional prefix. Mutually compatible with CRI. |
| `prompt_caching`                   | `boolean`                                                                  | `true`              | When `true`, Claude messages get `cachePoint` injection per Anthropic spec. Auto-disabled for non-Anthropic models. |
| `adaptive_thinking`                | `"none" \| "low" \| "medium" \| "high"`                                    | `"high"` for supported models, `"none"` otherwise | `"low"`, `"medium"`, `"high"` valid only on adaptive-capable models (see §4.3). Any other value on unsupported model → silent downgrade to `"none"` + warn. |
| `enable_1m_context`                | `boolean`                                                                  | `true` when model supports, `false` otherwise | Auto-enabled when model is `opus-4-7` or `opus-4-6`. Sends `anthropic-beta: context-1m-2025-08-07` header + applies `:1m` suffix to model ID. |

**Important nuance:** `default_chain` mode ignores `api_key`, `profile`, `access_key_id`, `secret_access_key`, `session_token` fields entirely — even if set. It reads `AWS_BEARER_TOKEN_BEDROCK`, `AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, etc. from the ambient environment via the native AWS SDK default credential chain.

**Important boundary:** `mode=="api_key"` takes **strict** precedence: if the user selected `api_key` mode but `AWS_BEARER_TOKEN_BEDROCK` is unset AND `api_key` field is null, raise `bedrock_auth_api_key_missing`. Do NOT silently fall back to default chain — that masks configuration errors.

**Unknown field policy:** Implementations MUST ignore unknown keys in the persisted config (forward compatibility). They MUST NOT error on unknown keys.

### 4.2 `BedrockModelInfo` (resolved model metadata)

| Field                    | Type                                              | Notes                                                                 |
| ------------------------ | ------------------------------------------------- | --------------------------------------------------------------------- |
| `id`                     | `string`                                          | The model ID sent to Bedrock (e.g., `global.anthropic.claude-opus-4-7:1m`). |
| `base_model_id`          | `string`                                          | Bare model ID without inference-profile prefix or `:1m` suffix.       |
| `family`                 | `"opus" \| "sonnet" \| "haiku" \| "other"`        | Derived from base ID.                                                 |
| `version`                | `string`                                          | E.g., `"4.7"`, `"4.6"`, `"3.5"`.                                     |
| `supports_reasoning`     | `boolean`                                         | True for Claude 4.6+ and Opus 4.5+.                                  |
| `supports_1m_context`    | `boolean`                                         | True for Opus 4.6, Opus 4.7 (verify against `models.generated.ts` or equivalent catalog). |
| `supports_prompt_cache`  | `boolean`                                         | True for all Claude models on Bedrock.                               |
| `max_tokens`             | `integer`                                         | Context window. `1_000_000` for 1M-capable models + `:1m` suffix; else model-default. |
| `is_inference_profile`   | `boolean`                                         | True if ID contains `global.`, `us.`, `eu.`, `apac.`, `au.`, `jp.`.  |
| `is_application_profile` | `boolean`                                         | True if ID is an ARN containing `application-inference-profile`.     |

### 4.3 Adaptive-Thinking Eligibility Table

Exact set of base model IDs eligible for non-`none` adaptive thinking:

```
anthropic.claude-opus-4-7
anthropic.claude-opus-4-6
anthropic.claude-sonnet-4-6
```

(Match by `base_model_id.startsWith(x)` to allow version-point releases. Any model catalog entry with `reasoning: true` in `models.generated.ts` / Hermes `model_metadata.py` is also eligible.)

**Important nuance:** Opus 4.7 ships with Anthropic's API default of `thinkingDisplay: "omitted"`. Both OpenClaw and Pi already default to `"summarized"` to match older 4.x behavior. This spec **preserves** the `"summarized"` default on Bedrock for all three tools for consistency; users can opt into `"omitted"` via an advanced config key.

## 5. Per-Tool Contracts

### 5.1 Hermes (Python)

**Status:** ~95% complete on branch `feat/native-bedrock-provider-20260428`. Gaps listed below.

**Files (existing):**
- `agent/bedrock_adapter.py` — Converse streaming, auth resolution, client cache.
- `agent/transports/bedrock.py` — `BedrockTransport` class.
- `agent/anthropic_adapter.py` — AnthropicBedrock client (Claude path) + 1M beta wiring.
- `agent/auxiliary_client.py` — Summarization/compression/session-search auxiliary clients.
- `hermes_cli/main.py` — Interactive setup wizard.
- `hermes_cli/runtime_provider.py` — CRI prefix logic.
- `hermes_cli/config.py` — Config read/write.
- `hermes_cli/doctor.py` — Diagnostics.
- `agent/model_metadata.py` — Context length table.

**Required changes for Cline parity:**

| ID | Change | File |
| -- | ------ | ---- |
| H1 | Verify `BedrockAuthConfig` has explicit `credentials` mode (access_key_id / secret_access_key / session_token fields) distinct from `api_key` and `profile`. | `agent/bedrock_adapter.py::resolve_bedrock_auth_config` |
| H2 | Verify interactive setup wizard prompts for all four modes as distinct radio choices, matching Cline's UI (not a single "auth type" free-text). | `hermes_cli/main.py` |
| H3 | Confirm `vpc_endpoint_url` is a visible config key in the advanced setup path. | `hermes_cli/main.py`, `hermes_cli/config.py` |
| H4 | Confirm `use_cross_region_inference` AND `use_global_inference_profile` are exposed as **two** independent booleans (Cline shows both checkboxes). | `hermes_cli/runtime_provider.py`, `hermes_cli/main.py` |
| H5 | Add missing failing test if not already present: `test_bedrock_auth_modes_are_disjoint` — verifies selecting one mode clears the other modes' fields on save. | `tests/agent/test_bedrock_adapter.py` |
| H6 | Update `BEDROCK_CR_MARKERS` in `hooks/post-update-patches/handler.py` to include any new needles introduced since 2026-04-27. | `~/.hermes/hooks/post-update-patches/handler.py` |

**Exit criteria:** All existing 459 tests pass + the new gap tests pass. `hermes doctor` reports Bedrock OK. CR markers all present after a mock `hermes update` cycle.

### 5.2 OpenClaw (TypeScript)

**Fork target:** `github.com/<user>/openclaw` branch `feat/bedrock-cline-parity-20260429` off latest `main`.
**Upstream:** `github.com/openclaw/openclaw`.
**Install path (local):** `/opt/homebrew/lib/node_modules/openclaw/` (globally installed via `npm install -g openclaw`).

**Files (existing in upstream):**

```
extensions/amazon-bedrock/
  index.ts                    # Main provider entry — adaptive-thinking, cache-point injection
  discovery.ts                # ListFoundationModels + ListInferenceProfiles
  setup-api.ts                # Setup flow hook
  config-api.ts               # Config registration
  config-compat.ts            # Legacy config migration
  embedding-provider.ts       # Embedding model support
  memory-embedding-adapter.ts # Memory subsystem integration
  register.sync.runtime.ts    # Runtime registration
  + tests for each
cli/src/components/
  BedrockSetup.tsx            # Ink-based CLI setup UI (if present)
  BedrockCustomModelFlow.tsx  # Custom model entry flow (if present)
webview-ui/src/components/settings/providers/
  BedrockProvider.tsx         # Webview settings UI (Cline-style)
```

(If `cli/src/components/BedrockSetup.tsx` or `webview-ui/...` don't exist in OpenClaw's tree, they must be created based on the Cline reference files at the same relative path.)

**Required changes for Cline parity:**

| ID | Change |
| -- | ------ |
| OC1 | Audit `extensions/amazon-bedrock/index.ts` option resolution for 4-way `mode` parsing. Add `AwsCredentialsAuth` interface (access_key_id, secret_access_key, session_token) distinct from `AwsProfileAuth` and `AwsBearerTokenAuth`. |
| OC2 | Ensure `BedrockRuntimeClientConfig` construction uses a match-on-mode dispatch: `api_key` → `token + authSchemePreference: ["httpBearerAuth"]`; `profile` → `profile` field; `credentials` → `credentials: { accessKeyId, secretAccessKey, sessionToken }`; `default_chain` → pass no explicit auth. |
| OC3 | Expose `vpc_endpoint_url` as a settings field. When set, pass as `endpoint` in `BedrockRuntimeClientConfig`. |
| OC4 | Verify `use_cross_region_inference` AND `use_global_inference_profile` are **both** exposed as separate booleans in the setup UI. Prefix resolution: if `global && cri` → `global.` prefix; if `cri && !global` → region-specific prefix; if `!cri` → bare base model ID. |
| OC5 | Wire 1M-context beta: when model's `supports_1m_context` is true and the user-selected model ends with `:1m` OR the "Use 1M context" toggle is on, inject `anthropic-beta: context-1m-2025-08-07` request header. |
| OC6 | Ensure adaptive-thinking dropdown shows `None / Low / Medium / High` for eligible models, `None` only for others. Default: `High` when eligible, else `None`. |
| OC7 | Ensure streaming reasoning content is piped through: `reasoningContent.reasoningText` deltas emit as thinking blocks; `reasoningContent.signature` concatenated into the thinking block's signature. |
| OC8 | Add failing tests **before** each fix: `bedrock-auth-modes.test.ts`, `bedrock-vpc-endpoint.test.ts`, `bedrock-1m-context-beta.test.ts`, `bedrock-adaptive-thinking-eligibility.test.ts`, `bedrock-streaming-reasoning.test.ts`. |
| OC9 | Update `webview-ui/src/components/settings/providers/BedrockProvider.tsx` (if OpenClaw has a webview) to visually match Cline's layout (auth radio, region, toggles, model picker, adaptive thinking dropdown). |
| OC10 | Update `extensions/amazon-bedrock/package.json` to bump patch version; `CHANGELOG.md` entry. |

**PR expectations:**
- Include a `docs/providers/amazon-bedrock.md` addition documenting all four auth modes with examples.
- Squash-merge candidate. Commit message: `feat(bedrock): Cline-parity auth modes, VPC endpoint, 1M beta, adaptive thinking`.

### 5.3 Pi (TypeScript, pi-mono monorepo)

**Fork target:** `github.com/<user>/pi-mono` branch `feat/bedrock-cline-parity-20260429` off latest `main`.
**Upstream:** `github.com/badlogic/pi-mono`.
**Install path (local):** `/opt/homebrew/lib/node_modules/@mariozechner/pi-coding-agent/` (published from `packages/coding-agent` in the monorepo).

**Files (existing in upstream):**

```
packages/ai/src/
  bedrock-provider.ts                       # Re-export barrel
  providers/amazon-bedrock.ts               # Main stream implementation (956 LOC)
  models.generated.ts                       # Generated model catalog
  types.ts                                  # Shared type definitions
packages/ai/test/
  bedrock-endpoint-resolution.test.ts
  bedrock-thinking-payload.test.ts
  bedrock-models.test.ts
  bedrock-utils.ts
packages/coding-agent/src/
  bun/register-bedrock.ts                   # Bun-specific registration
  core/model-resolver.ts                    # Default model per provider
  core/auth-storage.ts                      # Credential storage
  core/model-registry.ts                    # Provider registration
  modes/interactive/interactive-mode.ts     # Interactive setup prompts
```

**Required changes for Cline parity:**

| ID | Change |
| -- | ------ |
| P1 | Extend `BedrockOptions` in `packages/ai/src/providers/amazon-bedrock.ts` to include explicit AWS-Credentials fields: `accessKeyId?`, `secretAccessKey?`, `sessionToken?`, `vpcEndpointUrl?`, `useCrossRegionInference?` (default `true`), `useGlobalInferenceProfile?` (default `true`), `enable1MContext?`. |
| P2 | Refactor `BedrockRuntimeClientConfig` construction to a `resolveBedrockAuthMode(options): "api_key" \| "profile" \| "credentials" \| "default_chain"` dispatch. Each branch sets only the matching SDK client fields; all others are left undefined. |
| P3 | `credentials` mode: construct `config.credentials = { accessKeyId, secretAccessKey, sessionToken }`. Skip bearer token and profile entirely. |
| P4 | `vpcEndpointUrl`, when set, overrides `config.endpoint`. Matches existing `endpointRegion` detection. |
| P5 | Extend `packages/coding-agent/src/core/auth-storage.ts` to persist `BedrockAuthConfig` under the `amazon-bedrock` provider key. Migration: convert existing `{ type: "api_key", key: "..." }` entries to new `{ mode: "api_key", api_key: "...", region, ... }` shape. Preserve backward compat read. |
| P6 | Interactive setup (`interactive-mode.ts`) gains Bedrock-specific branch: after the user picks `amazon-bedrock`, prompt for auth mode (radio), then mode-specific fields, then region, then toggles. Model picker unchanged (reuses existing `model-resolver.ts`). |
| P7 | `enable1MContext: true` path sends `anthropic-beta: context-1m-2025-08-07` header + applies `:1m` suffix to `modelId`. Use existing `CachePointType` / `CacheTTL` imports. |
| P8 | Failing tests first: `packages/ai/test/bedrock-auth-modes.test.ts`, `bedrock-credentials-mode.test.ts`, `bedrock-vpc-endpoint.test.ts`, `bedrock-1m-context.test.ts`, `bedrock-migration.test.ts` (in `coding-agent/test`). |
| P9 | Update `packages/coding-agent/docs/models.md` and `packages/ai/README.md` (if present) to document all four auth modes. |
| P10 | Bump `packages/ai/package.json` and `packages/coding-agent/package.json` versions; add `CHANGELOG.md` entries. |

**PR expectations:**
- Follow pi-mono's CONTRIBUTING.md (auto-close policy — expect maintainer review lead time).
- Pass existing vitest suite + new tests. No failures in `test.sh`.

## 6. Configuration Specification

### 6.1 Config Cheat Sheet (flat; every key/type/default)

```
# Hermes (~/.hermes/config.yaml — model section)
model.provider                             string      "bedrock"
model.base_url                             string      "https://bedrock-runtime.<region>.amazonaws.com"
model.default                              string      "global.anthropic.claude-opus-4-7:1m"
model.context_length                       integer     1000000

# Hermes (top-level bedrock block)
bedrock.mode                               string      "default_chain"
bedrock.region                             string      "us-east-1"
bedrock.api_key                            string|null null
bedrock.profile                            string|null null
bedrock.access_key_id                      string|null null
bedrock.secret_access_key                  string|null null
bedrock.session_token                      string|null null
bedrock.vpc_endpoint_url                   string|null null
bedrock.use_cross_region_inference         boolean     true
bedrock.use_global_inference_profile       boolean     true
bedrock.prompt_caching                     boolean     true
bedrock.adaptive_thinking                  enum        "high"
bedrock.enable_1m_context                  boolean     auto

# OpenClaw (~/.openclaw/config.json — providers.amazon-bedrock)
providers.amazon-bedrock.auth.mode         string      "default_chain"
providers.amazon-bedrock.auth.region       string      "us-east-1"
providers.amazon-bedrock.auth.apiKey       string|null null
providers.amazon-bedrock.auth.profile      string|null null
providers.amazon-bedrock.auth.accessKeyId  string|null null
providers.amazon-bedrock.auth.secretAccessKey string|null null
providers.amazon-bedrock.auth.sessionToken string|null null
providers.amazon-bedrock.vpcEndpointUrl    string|null null
providers.amazon-bedrock.useCrossRegionInference   boolean true
providers.amazon-bedrock.useGlobalInferenceProfile boolean true
providers.amazon-bedrock.promptCaching     boolean     true
providers.amazon-bedrock.adaptiveThinking  enum        "high"
providers.amazon-bedrock.enable1MContext   boolean     auto

# Pi (~/.pi/config.json — providers.amazon-bedrock)
providers.amazon-bedrock.mode              string      "default_chain"
providers.amazon-bedrock.region            string      "us-east-1"
providers.amazon-bedrock.apiKey            string|null null
providers.amazon-bedrock.profile           string|null null
providers.amazon-bedrock.accessKeyId       string|null null
providers.amazon-bedrock.secretAccessKey   string|null null
providers.amazon-bedrock.sessionToken      string|null null
providers.amazon-bedrock.vpcEndpointUrl    string|null null
providers.amazon-bedrock.useCrossRegionInference   boolean true
providers.amazon-bedrock.useGlobalInferenceProfile boolean true
providers.amazon-bedrock.promptCaching     boolean     true
providers.amazon-bedrock.adaptiveThinking  enum        "high"
providers.amazon-bedrock.enable1MContext   boolean     auto
```

### 6.2 Environment Variable Fallback

When `mode == "default_chain"`, implementations read from (in order):

1. `AWS_BEARER_TOKEN_BEDROCK` → use as bearer token, auth scheme `httpBearerAuth`.
2. `AWS_PROFILE` → use as profile name.
3. `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + (optional) `AWS_SESSION_TOKEN` → static credentials.
4. EC2/ECS instance metadata (IMDS) — only on AWS-hosted machines.
5. AWS SSO session — if `AWS_PROFILE` points at an SSO profile.

If none resolve, raise `bedrock_auth_default_chain_empty` at first API call (not at config load — lazy validation).

### 6.3 Migration

Each tool includes a one-shot migration that runs on first startup after the upgrade:

- **Old shape:** `{ type: "api_key", key: "..." }` or `{ provider: "bedrock", api_key: "..." }`
- **New shape:** `{ mode: "api_key", api_key: "...", region: "us-east-1", use_cross_region_inference: true, ... }`

Migration is **idempotent** — running it twice leaves config unchanged. Original config is backed up to `<config-dir>/backups/<timestamp>-pre-bedrock-parity.json` before rewrite.

## 7. State Machine / Lifecycle

### 7.1 Auth resolution lifecycle

```
[config_load]
    │
    ├── mode == "default_chain" ──► [resolve_from_env] ──► [sdk_client_construct]
    │
    ├── mode == "api_key" ──► [validate_api_key_nonempty]
    │                           │
    │                           ├── missing ──► raise bedrock_auth_api_key_missing
    │                           └── present ──► [sdk_client_construct_bearer]
    │
    ├── mode == "profile" ──► [validate_profile_exists_in_aws_config]
    │                           │
    │                           ├── not_found ──► raise bedrock_auth_profile_not_found
    │                           └── found ──► [sdk_client_construct_profile]
    │
    └── mode == "credentials" ──► [validate_access_key_and_secret_present]
                                    │
                                    ├── missing ──► raise bedrock_auth_credentials_missing
                                    └── present ──► [sdk_client_construct_static]
```

### 7.2 Streaming lifecycle

```
[stream_start]
    ├── build_request (messages + tools + thinking_budget + cachePoint)
    ├── apply_1m_beta_header (if enable_1m_context && model.supports_1m)
    ├── open_bedrock_converse_stream
    └── ┌── event_loop
        │   ├── messageStart               → emit stream_start
        │   ├── contentBlockStart          → open block (text | tool | thinking)
        │   ├── contentBlockDelta          → append to current block
        │   │   ├── delta.text             → text_delta
        │   │   ├── delta.toolUse.input    → tool_input_delta
        │   │   └── delta.reasoningContent → thinking_delta (+signature accumulate)
        │   ├── contentBlockStop           → close block
        │   ├── messageStop (stopReason)   → finalize message
        │   ├── metadata (usage)           → accumulate usage
        │   └── [on stale-connection error] ─► evict_client_cache(region) + reopen
        └── [on stream_end] → emit final AssistantMessage
```

## 8. Failure Model and Recovery Strategy

Every named error, its trigger, and its recovery:

| Error Name                              | Trigger                                                                       | Recovery                                                                              |
| --------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `bedrock_auth_mode_invalid`             | `mode` field is not one of the four allowed values.                          | Fail config load with user-facing error naming the valid set.                        |
| `bedrock_auth_api_key_missing`          | `mode=="api_key"` but `api_key` field is null/empty AND `AWS_BEARER_TOKEN_BEDROCK` is unset. | Fail at first API call. Tell user to run setup or export env var.                    |
| `bedrock_auth_profile_not_found`        | `mode=="profile"` but profile is missing from `~/.aws/config`.               | Fail at first API call. Direct user to `aws configure --profile <name>`.            |
| `bedrock_auth_credentials_missing`      | `mode=="credentials"` but access_key_id or secret_access_key is null.        | Fail at first API call with list of missing fields.                                 |
| `bedrock_auth_default_chain_empty`      | `mode=="default_chain"` and SDK finds no credentials.                        | Fail at first API call. Direct user to setup wizard.                                |
| `bedrock_region_not_supported`          | Selected region has no Bedrock endpoint (rare, e.g., `af-south-1`).          | Fallback to `us-east-1` with warn log. Persist warning so user sees it once.        |
| `bedrock_model_not_available`           | Selected model ID not in `ListFoundationModels` result for region.           | Prompt user to pick from discovered list. Never auto-substitute.                    |
| `bedrock_stale_client`                  | `ThrottlingException` or `botocore.exceptions.ConnectionError` on cached client. | Evict client cache for `region`. Reconstruct on next call. Attempt is retried once automatically. |
| `bedrock_1m_beta_unsupported`           | Model doesn't support 1M context but `enable_1m_context==true`.              | Silent downgrade to default context. Warn user at setup time, not at every call.    |
| `bedrock_vpc_endpoint_unreachable`      | Connection to `vpc_endpoint_url` fails.                                      | Surface error verbatim. Do NOT fall back to standard endpoint (that could cause data-exfil if VPC-only policy). |
| `bedrock_adaptive_thinking_unsupported` | Non-adaptive model + `adaptive_thinking != "none"`.                          | Silent downgrade to `"none"` + warn once.                                          |
| `bedrock_converse_stream_timeout`       | Stream exceeds wall-clock cap (900s default, matches Hermes).                | Close client, raise TimeoutError. User sees "response took too long, try again."    |

**Recovery invariants:**

- **Never retry** on `bedrock_auth_*` errors. Auth misconfiguration doesn't fix itself.
- **Always evict client cache** before any retry that involves credential rotation.
- **Never silently fall back across auth modes.** `api_key` mode never tries profile on failure.

## 9. Post-Update Patch Guard

### 9.1 Design

Mirrors Hermes' existing `~/.hermes/hooks/post-update-patches/handler.py` pattern. Triggered by the same `gateway:startup` hook the existing guard already uses.

**New functions added to the same file (or a companion file `handler_dist.py` imported by `handler.py`):**

```python
def _check_openclaw_dist_markers():
    """Verify locally-applied Bedrock patches are present in installed OpenClaw bundle."""

def _check_pi_dist_markers():
    """Verify locally-applied Bedrock patches are present in installed Pi bundle."""

def _reapply_openclaw_dist_patches():
    """Re-apply text-anchor patches to /opt/homebrew/lib/node_modules/openclaw/dist/*.js."""

def _reapply_pi_dist_patches():
    """Re-apply text-anchor patches to /opt/homebrew/lib/node_modules/@mariozechner/pi-coding-agent/dist/*.js."""
```

### 9.2 Marker Table

| Marker Label                       | Tool     | File (relative to install root)                            | Needle (exact string)                                              |
| ---------------------------------- | -------- | ---------------------------------------------------------- | ------------------------------------------------------------------ |
| OpenClaw 4-way auth switch         | OpenClaw | `dist/model-auth-Bic7ggHC.js` (or successor hash)          | `"credentials" === mode` (specific to Cline-parity auth branch)    |
| OpenClaw VPC endpoint              | OpenClaw | `dist/*.js` (grep-based lookup)                            | `vpcEndpointUrl`                                                   |
| OpenClaw 1M beta                   | OpenClaw | `dist/*.js`                                                | `context-1m-2025-08-07`                                            |
| OpenClaw global inference profile  | OpenClaw | `dist/*.js`                                                | `useGlobalInferenceProfile`                                        |
| Pi 4-way auth dispatch             | Pi       | `dist/ai/amazon-bedrock.js` (bundled)                      | `resolveBedrockAuthMode`                                           |
| Pi credentials mode                | Pi       | `dist/ai/amazon-bedrock.js`                                | `accessKeyId:e.accessKeyId` (or similar — verify against bundle after PR) |
| Pi VPC endpoint                    | Pi       | `dist/ai/amazon-bedrock.js`                                | `vpcEndpointUrl`                                                   |
| Pi 1M beta                         | Pi       | `dist/ai/amazon-bedrock.js`                                | `context-1m-2025-08-07`                                            |

**Important nuance:** When the upstream PR lands and `npm update` pulls a version that has the feature built-in, the markers WILL be present naturally. The guard becomes a no-op in that case (logs "all markers present"). This is the desired steady state.

### 9.3 Patch File Layout

Local patches live in `~/.hermes/patches/dist/`:

```
~/.hermes/patches/dist/
  openclaw-2026.4.26.patch          # Text-anchor patches keyed to OpenClaw version
  openclaw-2026.4.26.markers.json   # List of marker needles to verify
  pi-0.70.5.patch
  pi-0.70.5.markers.json
  README.md                          # How to regenerate patches after upstream merge
```

Each `.patch` file is a list of `{file, old, new, label}` entries. The handler reads the matching file for the currently-installed version; if no file matches, logs a warning and skips (meaning upstream merged the feature — no patch needed).

### 9.4 Version-Aware Patch Selection

```python
def _select_patch_file(tool: str, installed_version: str) -> Path | None:
    """Return patch file matching installed version, or None if upstream-merged."""
    patch_dir = HERMES_HOME / "patches" / "dist"
    candidate = patch_dir / f"{tool}-{installed_version}.patch"
    if candidate.exists():
        return candidate
    # Upstream merged → no patch needed.
    return None
```

**Exit cleanly** if no patch file matches — do NOT attempt to patch a version we haven't tested against.

## 10. Reference Algorithms

### 10.1 Auth resolution (shared pseudocode)

```
function resolveBedrockAuth(config: BedrockAuthConfig, env: ProcessEnv) -> AwsSdkAuthInput:
    if config.mode == "api_key":
        token = coalesce(config.api_key, env.AWS_BEARER_TOKEN_BEDROCK)
        if token is empty:
            raise bedrock_auth_api_key_missing
        return { token: { token: stripBearerPrefix(token) },
                 authSchemePreference: ["httpBearerAuth"] }

    if config.mode == "profile":
        if isEmpty(config.profile):
            raise bedrock_auth_profile_not_found(name=null)
        return { profile: config.profile }

    if config.mode == "credentials":
        if isEmpty(config.access_key_id) or isEmpty(config.secret_access_key):
            raise bedrock_auth_credentials_missing(
                missing=[field for field in ("access_key_id","secret_access_key")
                         if isEmpty(config[field])])
        creds = { accessKeyId: config.access_key_id,
                  secretAccessKey: config.secret_access_key }
        if not isEmpty(config.session_token):
            creds.sessionToken = config.session_token
        return { credentials: creds }

    if config.mode == "default_chain":
        # Let the SDK resolve. Empty chain raises at first API call.
        return {}

    raise bedrock_auth_mode_invalid(got=config.mode)


function stripBearerPrefix(token: string) -> string:
    t = token.strip()
    if t.lower().startsWith("bearer "):
        return t[7:].strip()
    return t
```

### 10.2 Model ID routing with CRI and global profile

```
function resolveBedrockModelId(
    base_id: string,             # e.g., "anthropic.claude-opus-4-7"
    region: string,              # e.g., "us-east-1"
    use_cri: boolean,
    use_global: boolean,
    enable_1m: boolean,
    model_supports_1m: boolean
) -> string:
    result = base_id

    if use_cri:
        if use_global:
            # Strip any regional prefix, add "global."
            for prefix in ("us.", "eu.", "apac.", "au.", "jp."):
                if result.startsWith(prefix):
                    result = result[len(prefix):]
                    break
            result = "global." + result
        else:
            # Inject regional prefix matching the region.
            regional = regionToPrefix(region)  # us-east-1 -> "us.", eu-west-1 -> "eu.", etc.
            if regional and not result.startsWith(regional):
                result = regional + result

    if enable_1m and model_supports_1m and not result.endsWith(":1m"):
        result = result + ":1m"

    return result


function regionToPrefix(region: string) -> string | null:
    if region.startsWith("us-"): return "us."
    if region.startsWith("eu-"): return "eu."
    if region.startsWith("ap-"): return "apac."
    if region == "ap-southeast-2": return "au."
    if region == "ap-northeast-1": return "jp."
    return null
```

### 10.3 Post-update patch guard (handler pseudocode)

```
function checkAndReapplyDistPatches(tool: "openclaw" | "pi"):
    install_root = getInstallRoot(tool)
    if not install_root.exists():
        log.info(f"[post-update] {tool} not installed — skip")
        return

    installed_version = readVersionFromPackageJson(install_root / "package.json")
    patch_file = selectPatchFile(tool, installed_version)

    if patch_file is None:
        log.info(f"[post-update] {tool} {installed_version} — no patch file (upstream merged)")
        return

    markers_file = patch_file.withSuffix(".markers.json")
    markers = readJson(markers_file)

    missing = []
    for marker in markers:
        full_path = install_root / marker.file
        if not full_path.exists():
            missing.append((marker.label, marker.file, "file not found"))
            continue
        content = full_path.readText()
        if marker.needle not in content:
            missing.append((marker.label, marker.file, "needle missing"))

    if not missing:
        log.info(f"[post-update] {tool} dist markers all present ✓")
        return

    log.warning(f"[post-update] {tool} dist missing {len(missing)} markers; attempting reapply")
    for m in missing:
        log.warning(f"  ✗ {m.label}  {m.file}  ({m.reason})")

    patches = readPatchFile(patch_file)
    applied = 0
    for p in patches:
        target = install_root / p.file
        content = target.readText()
        if p.old in content:
            content = content.replace(p.old, p.new, 1)
            target.writeText(content)
            applied += 1
            log.info(f"[post-update] {tool} applied patch: {p.label}")
        else:
            log.warning(f"[post-update] {tool} anchor missing for: {p.label} — skip")

    log.info(f"[post-update] {tool} reapplied {applied}/{len(patches)} patches")
```

## 11. Test and Validation Matrix

### 11.1 Core Conformance (must pass for every tool)

| Test                                               | Hermes | OpenClaw | Pi  |
| -------------------------------------------------- | :----: | :------: | :-: |
| `api_key` mode sends bearer token header           |   ✓    |    ✓     |  ✓  |
| `profile` mode uses profile, ignores bearer        |   ✓    |    ✓     |  ✓  |
| `credentials` mode uses static creds, ignores rest |   ✓    |    ✓     |  ✓  |
| `default_chain` mode passes no explicit auth       |   ✓    |    ✓     |  ✓  |
| `api_key` missing → named error (not silent fallback) |  ✓   |    ✓     |  ✓  |
| `credentials` missing → named error                |   ✓    |    ✓     |  ✓  |
| `vpc_endpoint_url` overrides standard endpoint     |   ✓    |    ✓     |  ✓  |
| CRI prefix injected correctly per region           |   ✓    |    ✓     |  ✓  |
| `global.` prefix when both toggles on              |   ✓    |    ✓     |  ✓  |
| `:1m` suffix on Opus 4.7 with enable_1m_context    |   ✓    |    ✓     |  ✓  |
| `anthropic-beta: context-1m-2025-08-07` sent       |   ✓    |    ✓     |  ✓  |
| Adaptive thinking eligibility respected            |   ✓    |    ✓     |  ✓  |
| Streaming emits text deltas                        |   ✓    |    ✓     |  ✓  |
| Streaming emits tool-use deltas                    |   ✓    |    ✓     |  ✓  |
| Streaming emits reasoning deltas + signatures      |   ✓    |    ✓     |  ✓  |
| Cache point injection for Anthropic models         |   ✓    |    ✓     |  ✓  |
| Cache points NOT injected for non-Anthropic models |   ✓    |    ✓     |  ✓  |
| Client cache evicted on stale-connection error     |   ✓    |    ✓     |  ✓  |

### 11.2 Extension Conformance (test if shipped)

| Test                                              | Notes                                                         |
| ------------------------------------------------- | ------------------------------------------------------------- |
| Application-inference-profile ARN resolution      | OpenClaw has existing tests; Pi/Hermes verify matching        |
| Cross-region inference with au./apac./jp. prefix  | Matches OpenClaw's existing `discovery.test.ts` coverage      |
| Migration from legacy config shape                | Pre-existing `{ type: "api_key", key }` → new shape           |
| HTTP/1.1 fallback via `AWS_BEDROCK_FORCE_HTTP1`   | Pi has this today; replicate in OpenClaw if absent            |
| Proxy agent via `HTTP_PROXY`                      | Pi has this today; replicate if absent                        |

### 11.3 Integration Profile (needs real AWS credentials)

Marked with env var `BEDROCK_INTEGRATION_TESTS=1`. Skipped by default in CI.

| Test                                    | What it proves                                           |
| --------------------------------------- | -------------------------------------------------------- |
| Real Opus 4.7 call with `:1m` suffix    | 1M context window actually honored end-to-end            |
| Real CRI call returns `us.` prefix      | CRI works against live AWS                               |
| Real streaming with reasoning          | `reasoningContent` event structure matches spec          |
| `ListFoundationModels` succeeds         | Region/auth/IAM setup is correct                         |
| `ListInferenceProfiles` succeeds        | IAM policy includes `bedrock:ListInferenceProfiles`     |

### 11.4 Patch Guard Tests (Hermes only)

| Test                                              | Proves                                                   |
| ------------------------------------------------- | -------------------------------------------------------- |
| Missing OpenClaw markers → log + attempt reapply  | Guard detects dist rewrite                               |
| All OpenClaw markers present → no-op log          | Steady state when upstream merges                        |
| Patch file for unknown version → skip with warn   | Safety: never patch an untested bundle                   |
| Reapply increments counter correctly              | Idempotent behavior                                      |
| Reapply leaves file unchanged if anchor missing   | No partial writes                                        |

## 12. Observability

Each tool logs at INFO for the following events:

- Auth mode resolved (`"bedrock: auth mode resolved: profile=<name>"`)
- Region resolved (`"bedrock: region=us-east-1 (from config)"`)
- Model ID routed (`"bedrock: model routed: anthropic.claude-opus-4-7 → global.anthropic.claude-opus-4-7:1m"`)
- Client cache hit/miss
- Stream start/end with duration and token counts
- Stream cache metrics (read/write)

At WARN:

- Adaptive thinking downgrade on unsupported model
- 1M beta silent downgrade
- Stale-client eviction

At ERROR:

- Any named error from §8

**Log key prefix:** `bedrock:` (Python), `[bedrock]` (TS). Makes `grep bedrock` across all three tools' logs easy.

## 13. Security and Operational Safety

- **Credential storage:** Never log `api_key`, `access_key_id`, `secret_access_key`, `session_token`. Masked in all log lines. Written to config files with `0600` perms (Unix).
- **Env-var leakage:** When `mode != "api_key"`, ensure `AWS_BEARER_TOKEN_BEDROCK` does NOT leak into the SDK client. This is Hermes' existing `_masked_aws_env` concern — preserve the behavior in OpenClaw/Pi.
- **VPC endpoint:** On `bedrock_vpc_endpoint_unreachable`, do NOT fall back to the public endpoint. Some users run Bedrock under a VPC-only IAM policy; a fallback would silently circumvent it.
- **Bearer token format:** Strip `Bearer ` prefix if user pastes it. Reject empty/whitespace-only tokens.
- **PRs:** Submit with `SECURITY.md` awareness. Don't include real AWS credentials in test fixtures (use `"dummy-access-key"` / `"dummy-secret-key"` as Pi already does).

## 14. Implementation Checklist (Definition of Done)

### Phase A — Research & Fork (all three tools in parallel)

- [ ] Fork `openclaw/openclaw` → `<user>/openclaw` branch `feat/bedrock-cline-parity-20260429`
- [ ] Fork `badlogic/pi-mono` → `<user>/pi-mono` branch `feat/bedrock-cline-parity-20260429`
- [ ] Clone Cline reference (done: `~/research-bedrock/cline`)
- [ ] Audit each existing Bedrock provider against Cline's `src/core/api/providers/bedrock.ts`
- [ ] Produce a per-file gap list committed as `docs/bedrock-gap-analysis.md` in each fork

### Phase B — Hermes verification

- [ ] Run `./venv/bin/python -m pytest tests/agent/test_bedrock_adapter.py tests/agent/test_bedrock_integration.py tests/hermes_cli/test_bedrock_model_flow.py` — expect green
- [ ] Verify all four auth modes work via `hermes doctor`
- [ ] Confirm all markers in `BEDROCK_CR_MARKERS` are present in `agent/bedrock_adapter.py`, `hermes_cli/*.py` on HEAD
- [ ] If gaps found: write failing test → fix → commit on `feat/native-bedrock-provider-20260428`

### Phase C — OpenClaw parity (TDD)

- [ ] Write failing tests (OC8) before any implementation
- [ ] Implement OC1 through OC10 sequentially
- [ ] Run OpenClaw's existing test suite — 100% pass
- [ ] Update docs (OC-docs)
- [ ] Bump version, update CHANGELOG
- [ ] Open upstream PR against `openclaw/openclaw:main`
- [ ] Ensure PR passes OpenClaw CI

### Phase D — Pi parity (TDD)

- [ ] Write failing tests (P8) before any implementation
- [ ] Implement P1 through P10 sequentially
- [ ] Run `./test.sh` in pi-mono — 100% pass
- [ ] Update docs (P-docs)
- [ ] Bump packages' versions, update CHANGELOG
- [ ] Open upstream PR against `badlogic/pi-mono:main`
- [ ] Ensure PR passes pi-mono CI

### Phase E — Post-update patch guard

- [ ] Create `~/.hermes/patches/dist/` directory structure
- [ ] Generate `openclaw-<current-version>.patch` + `.markers.json` from fork's diff against installed dist
- [ ] Generate `pi-<current-version>.patch` + `.markers.json` similarly
- [ ] Add `_check_openclaw_dist_markers` / `_reapply_openclaw_dist_patches` / Pi equivalents to `~/.hermes/hooks/post-update-patches/handler.py` (or sibling file)
- [ ] Wire into `handle()` function
- [ ] Write unit tests for marker detection + reapply
- [ ] Simulate a `hermes update` + verify guard runs and passes

### Phase F — Documentation & handoff

- [ ] Update `docs/superpowers/plans/2026-04-27-native-bedrock-provider-hardening.md` with any addenda
- [ ] Write `docs/bedrock-parity-handoff.md` — explains current status of upstream PRs
- [ ] Commit all changes on relevant branches
- [ ] Landing plan: merge Hermes branch to main when PRs are accepted upstream

## 15. Out-of-Scope Addenda

Flag explicitly: these are deliberately not included in this spec's scope.

- Rotating per-session credentials on long-running processes (would need a background refresher thread; not today).
- Multi-account support (single active account per tool for now).
- Bedrock Guardrails / content moderation integration.
- Bedrock Agents / Knowledge Bases.
- Non-AWS proxy (e.g., LiteLLM) using the Bedrock model ID format — users can use the existing custom-endpoint path outside this spec's flow.
- A shared `bedrock-auth-config` npm package (nice future work; not required for parity).

---

## Self-Review Summary

- **Sections:** 15 plus cheat-sheet + checklist
- **Line count:** ~950
- **Gaps found during self-review:** Added explicit `Important nuance` on `api_key` mode strict precedence; added migration section; added version-aware patch selection; added out-of-scope addenda.
- **Remaining `[TBD]`:** None. Every field has a type and default; every error has a name and recovery; every state transition has a trigger; all four auth modes have explicit validation rules.
- **Confidence:** Ready for implementation. PR-opening steps assume the user has GitHub access under their standard `prax-lannister` handle (or equivalent) — if forks should live under a different account, that's the only open decision.
