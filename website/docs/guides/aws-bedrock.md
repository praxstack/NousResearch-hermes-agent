---
sidebar_position: 14
title: "AWS Bedrock"
description: "Use Hermes Agent with Amazon Bedrock — native AnthropicBedrock SDK, Bedrock API keys, IAM / SSO / instance-role authentication, cross-region inference, 1M context, VPC endpoints, Guardrails, and prompt caching"
---

# AWS Bedrock

Hermes Agent supports Amazon Bedrock as a **native provider** using:

- **AnthropicBedrock SDK** for Claude models — full feature parity: prompt caching, extended thinking, adaptive thinking (`xhigh` effort), 1M context window, fast mode.
- **Converse API** (boto3) for non-Claude models (Amazon Nova, Meta Llama, DeepSeek, Mistral, etc.).

No OpenAI-compatible proxy, no `bedrock-mantle`, no `bedrock-native`. Direct AWS calls only.

## Prerequisites

- **boto3** — install with `pip install hermes-agent[bedrock]`
- **Bedrock auth** — choose one method in `hermes model`:
  - Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`)
  - AWS profile (SSO, named profile)
  - Explicit AWS credentials (`AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`)
  - Default AWS SDK credential chain (IAM instance role, ECS task role, Lambda, etc.)
- **IAM permissions** — at minimum:
  - `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`
  - `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles`

:::tip EC2 / ECS / Lambda
Attach an IAM role with `AmazonBedrockFullAccess`. No API keys, no `.env` config — Hermes detects the instance role automatically.
:::

## Quick Start

```bash
# Install with Bedrock support
pip install hermes-agent[bedrock]

# Select Bedrock as your provider (interactive wizard)
hermes model
# → Choose "More providers..." → "AWS Bedrock"
# → Select auth method, region
# → Choose cross-region inference, global profile, prompt caching
# → Select model (e.g. global.anthropic.claude-opus-4-7 or :1m variant for 1M context)

hermes chat
```

## Configuration

After running `hermes model`, your `~/.hermes/config.yaml` will contain:

```yaml
model:
  default: global.anthropic.claude-opus-4-7
  provider: bedrock
  base_url: https://bedrock-runtime.us-east-1.amazonaws.com

bedrock:
  region: us-east-1
  auth_method: api_key
  use_cross_region_inference: true
  use_global_inference_profile: true
  use_prompt_caching: true
```

Hermes stores a Bedrock API key **only** as `AWS_BEARER_TOKEN_BEDROCK`. It never writes Bedrock credentials to `OPENAI_API_KEY` or `OPENAI_BASE_URL`.

### Auth Method

Set `bedrock.auth_method` to one of:

| Method | Source of truth | Use when |
|--------|-----------------|----------|
| `api_key` | `AWS_BEARER_TOKEN_BEDROCK` | You have a Bedrock API key |
| `profile` | `bedrock.profile` | You use AWS SSO or named profiles |
| `credentials` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN` | You want explicit credentials |
| `default_chain` | AWS SDK default credential chain | You run on AWS compute or manage auth outside Hermes |

:::note Bearer token normalization
If you paste a token that starts with `Bearer ` (common when copying from the AWS console), Hermes automatically strips the prefix. You'll see a confirmation message during setup.
:::

### Region

Set the AWS region (highest priority first):

1. `bedrock.region` in `config.yaml`
2. `AWS_REGION` environment variable
3. `AWS_DEFAULT_REGION` environment variable
4. Default: `us-east-1`

### Cross-Region Inference

Bedrock cross-region inference (CRI) routes requests across multiple AWS regions for higher throughput, lower latency, and automatic failover. Hermes applies the appropriate regional prefix based on your configured region:

| Region prefix | Used for |
|---|---|
| `us.` | `us-east-1`, `us-west-2`, etc. |
| `eu.` | `eu-west-1`, `eu-central-1`, etc. |
| `apac.` | `ap-southeast-1`, `ap-south-1`, etc. |
| `jp.` | `ap-northeast-1/2/3` (Japan-specific) |
| `au.` | `ap-southeast-2` (Australia) |
| `global.` | All regions (Opus 4.6+ only) |

Enable/disable and configure in `hermes model` or directly in `config.yaml`:

```yaml
bedrock:
  use_cross_region_inference: true   # Enable regional prefix (us./eu./apac./etc.)
  use_global_inference_profile: true  # Prefer global.* for supported models (Opus 4.6+)
```

When both are true, Hermes prefers `global.*` for Claude Opus 4.6/4.7 and Sonnet 4.6, and uses the regional prefix for everything else. This matches Cline's `getModelId()` logic exactly.

### Prompt Caching

Bedrock prompt caching reduces input token costs by up to 90% by caching the system prompt and recent conversation turns on AWS infrastructure. Hermes automatically injects `cachePoint` markers (Converse API) or `cache_control` blocks (AnthropicBedrock SDK) on the two most recent user messages and the system prompt.

```yaml
bedrock:
  use_prompt_caching: true   # Enabled by default — recommended for long conversations
```

Caching is only activated for models that support it (Claude Sonnet 4.5+, Opus 4.6+, Nova Pro).

### VPC Endpoint

For enterprise deployments where Bedrock traffic must stay within your VPC:

```yaml
bedrock:
  vpc_endpoint_url: https://bedrock-runtime.us-east-1.vpce.amazonaws.com
```

Hermes also reads `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` and `AWS_ENDPOINT_URL` environment variables (same as the AWS CLI), with the config key taking highest priority.

## 1M Context Window

Claude Opus 4.7, Opus 4.6, and Sonnet 4.6 support a **1M token context window** on Bedrock, but it must be explicitly opted into. Bedrock defaults to 200K.

To use 1M context, select the `:1m` variant of the model:

```
global.anthropic.claude-opus-4-7:1m
us.anthropic.claude-opus-4-6-v1:1m
anthropic.claude-sonnet-4-6:1m
```

The `hermes model` picker automatically shows both variants (200K and :1m) for eligible models. On the wire, Hermes:
1. Strips the `:1m` suffix from the `modelId` sent to Bedrock
2. Adds `anthropic_beta: ["context-1m-2025-08-07"]` to `additionalModelRequestFields` (Converse path) or `extra_headers` (AnthropicBedrock SDK path)

:::caution Billing
The 1M context tier is billed at approximately 2× the input price once you exceed 200K tokens. Use it for long documents, large codebases, or extended conversations. For most tasks, the default 200K is sufficient.
:::

## Available Models

| Model | Default ID | 1M variant |
|-------|-----------|------------|
| Claude Opus 4.7 | `global.anthropic.claude-opus-4-7` | `global.anthropic.claude-opus-4-7:1m` |
| Claude Opus 4.6 | `global.anthropic.claude-opus-4-6-v1` | `global.anthropic.claude-opus-4-6-v1:1m` |
| Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6:1m` |
| Claude Sonnet 4.5 | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | — |
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | — |
| Amazon Nova Pro | `us.amazon.nova-pro-v1:0` | — |
| Amazon Nova Lite | `us.amazon.nova-lite-v1:0` | — |
| Amazon Nova Micro | `us.amazon.nova-micro-v1:0` | — |
| DeepSeek V3 | `deepseek.v3` | — |
| Meta Llama 4 Scout | `us.meta.llama4-scout-17b-instruct-v1:0` | — |
| Meta Llama 4 Maverick | `us.meta.llama4-maverick-17b-instruct-v1:0` | — |

You can also paste an **Application Inference Profile ARN** directly as the model ID:
```
arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/myprofile
```
This enables cost allocation tagging and usage tracking via pre-created profiles.

## Adaptive Thinking (Opus 4.6 / 4.7)

Claude Opus 4.6 and 4.7 use **adaptive thinking** instead of fixed thinking budgets. Set the effort level via the `reasoningEffort` agent config:

| Level | Description |
|-------|-------------|
| `none` | Disable thinking entirely |
| `low` | Fast responses, minimal reasoning |
| `medium` | Default |
| `high` | More thorough reasoning |
| `xhigh` | Opus 4.7+ only — between high and max |
| `max` | Maximum reasoning effort |

On Bedrock, Hermes passes the effort level as `output_config.effort` in `additionalModelRequestFields`. For Opus 4.6 (which does not support `xhigh`), Hermes automatically downgrades `xhigh → max` to prevent HTTP 400 errors.

## Guardrails

Apply [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) to all model invocations:

```yaml
bedrock:
  guardrail:
    guardrail_identifier: "abc123def456"  # From the Bedrock console
    guardrail_version: "1"                # Version number or "DRAFT"
    stream_processing_mode: "async"       # "sync" or "async"
    trace: "disabled"                     # "enabled", "disabled", or "enabled_full"
```

## Model Discovery

Hermes auto-discovers models via the Bedrock control plane. Customize discovery:

```yaml
bedrock:
  discovery:
    enabled: true
    provider_filter: ["anthropic", "amazon"]  # Only show these providers
    refresh_interval: 3600                     # Cache for 1 hour
```

## Diagnostics

```bash
hermes doctor
```

When Bedrock is your active provider, doctor shows a dedicated section:

```
◆ Bedrock Configuration
  ✓ Auth method      (Bedrock API Key (AWS_BEARER_TOKEN_BEDROCK))
  ✓ Region           (us-east-1)
  ✓ Active model     (global.anthropic.claude-opus-4-7 [200K context window])
  ✓ Cross-region inference  (enabled)
  ✓ Global inference profile (enabled)
  ✓ Prompt caching   (enabled)
  ✓ API connectivity (us-east-1, 126 models)
```

For the 1M variant:
```
  ✓ Active model     (global.anthropic.claude-opus-4-7:1m [1M context window])
```

## Gateway (Messaging Platforms)

Bedrock works with all Hermes gateway platforms (Telegram, Discord, Slack, Feishu, etc.). Configure Bedrock as your provider, then start the gateway normally:

```bash
hermes gateway setup
hermes gateway start
```

## Troubleshooting

### "No API key found" / "No AWS credentials"

Check the selected `bedrock.auth_method`.

- `api_key`: set `AWS_BEARER_TOKEN_BEDROCK`.
- `profile`: set `bedrock.profile` or choose a profile in `hermes model`.
- `credentials`: set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
- `default_chain`: run `aws configure` or attach an IAM role to your compute instance.

### "Invocation of model ID ... with on-demand throughput isn't supported"

Use an **inference profile ID** (prefixed with `us.`, `eu.`, `apac.`, or `global.`) instead of the bare foundation model ID. Example:
- ❌ `anthropic.claude-sonnet-4-6`
- ✅ `us.anthropic.claude-sonnet-4-6`

Enable `use_cross_region_inference: true` in `hermes model` to have Hermes apply the correct prefix automatically.

### "ValidationException: Context window exceeded" on 1M model

Bedrock requires the `context-1m-2025-08-07` beta header to accept inputs larger than 200K tokens. Hermes injects this header automatically when you use a `:1m` model variant. If you're seeing this error, ensure you've selected the `:1m` variant (e.g. `global.anthropic.claude-opus-4-7:1m`) and not the bare model ID.

### "ThrottlingException"

You've hit Bedrock's per-model rate limit. Hermes automatically retries with exponential backoff (3 attempts). To increase limits, request a quota increase in the [AWS Service Quotas console](https://console.aws.amazon.com/servicequotas/).

### Stale connection errors

If you see `AssertionError` or `ConnectionClosedError` retries, Bedrock's cached boto3 client has a dead connection (common after Mac sleep / network switch). Hermes detects stale connections and automatically evicts the cached client so the retry uses a fresh connection. If retries keep failing, restart Hermes or run `hermes chat` again.
