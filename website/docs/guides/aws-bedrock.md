---
sidebar_position: 14
title: "AWS Bedrock"
description: "Use Hermes Agent with Amazon Bedrock — native Converse API, Bedrock API keys, IAM authentication, Guardrails, and cross-region inference"
---

# AWS Bedrock

Hermes Agent supports Amazon Bedrock as a native provider using the **Converse API** — not an OpenAI-compatible proxy endpoint. This gives you full access to the Bedrock ecosystem: Bedrock API keys, IAM authentication, Guardrails, cross-region inference profiles, and all foundation models.

## Prerequisites

- **Bedrock auth** — choose one method in `hermes model`:
  - Bedrock API key (`AWS_BEARER_TOKEN_BEDROCK`)
  - AWS profile
  - explicit AWS access key credentials
  - default AWS SDK credential chain
- **AWS credentials** — for profile, explicit credentials, or default-chain auth, use any source supported by the [boto3 credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html):
  - IAM instance role (EC2, ECS, Lambda — zero config)
  - `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` environment variables
  - `AWS_PROFILE` for SSO or named profiles
  - `aws configure` for local development
- **boto3** — install with `pip install hermes-agent[bedrock]`
- **IAM permissions** — at minimum:
  - `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (for inference)
  - `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles` (for model discovery)

:::tip EC2 / ECS / Lambda
On AWS compute, attach an IAM role with `AmazonBedrockFullAccess` and you're done. No API keys, no `.env` configuration — Hermes detects the instance role automatically.
:::

## Quick Start

```bash
# Install with Bedrock support
pip install hermes-agent[bedrock]

# Select Bedrock as your provider
hermes model
# → Choose "More providers..." → "AWS Bedrock"
# → Select auth method, region, and model

# Start chatting
hermes chat
```

## Configuration

After running `hermes model`, your `~/.hermes/config.yaml` will contain:

```yaml
model:
  default: us.anthropic.claude-sonnet-4-6
  provider: bedrock
  base_url: https://bedrock-runtime.us-east-2.amazonaws.com

bedrock:
  region: us-east-2
  auth_method: api_key
```

Hermes stores a Bedrock API key only as `AWS_BEARER_TOKEN_BEDROCK`. It does not write Bedrock API keys into `OPENAI_API_KEY`, does not set `OPENAI_BASE_URL`, and does not route Bedrock through local proxy/custom provider state.

### Auth Method

Set `bedrock.auth_method` to one of:

| Method | Source of truth | Use when |
|--------|-----------------|----------|
| `api_key` | `AWS_BEARER_TOKEN_BEDROCK` | You have a Bedrock API key and want direct local requests |
| `profile` | `bedrock.profile` | You use AWS SSO or named profiles |
| `credentials` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN` | You want explicit credentials for Hermes |
| `default_chain` | AWS SDK default credential chain | You run on AWS compute or manage auth outside Hermes |

In `api_key`, `profile`, and `credentials` modes, Hermes uses the selected auth method as authoritative for Bedrock instead of silently falling through to unrelated ambient AWS credentials.

### Region

Set the AWS region in any of these ways (highest priority first):

1. `bedrock.region` in `config.yaml`
2. `AWS_REGION` environment variable
3. `AWS_DEFAULT_REGION` environment variable
4. Default: `us-east-1`

### Guardrails

To apply [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) to all model invocations:

```yaml
bedrock:
  region: us-east-2
  guardrail:
    guardrail_identifier: "abc123def456"  # From the Bedrock console
    guardrail_version: "1"                # Version number or "DRAFT"
    stream_processing_mode: "async"       # "sync" or "async"
    trace: "disabled"                     # "enabled", "disabled", or "enabled_full"
```

### Model Discovery

Hermes auto-discovers available models via the Bedrock control plane. You can customize discovery:

```yaml
bedrock:
  discovery:
    enabled: true
    provider_filter: ["anthropic", "amazon"]  # Only show these providers
    refresh_interval: 3600                     # Cache for 1 hour
```

## Available Models

Bedrock models use **inference profile IDs** for on-demand invocation. The `hermes model` picker shows these automatically, with recommended models at the top:

| Model | ID | Notes |
|-------|-----|-------|
| Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | Recommended — best balance of speed and capability |
| Claude Opus 4.6 | `us.anthropic.claude-opus-4-6-v1` | Most capable |
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Fastest Claude |
| Amazon Nova Pro | `us.amazon.nova-pro-v1:0` | Amazon's flagship |
| Amazon Nova Micro | `us.amazon.nova-micro-v1:0` | Fastest, cheapest |
| DeepSeek V3.2 | `deepseek.v3.2` | Strong open model |
| Llama 4 Scout 17B | `us.meta.llama4-scout-17b-instruct-v1:0` | Meta's latest |

:::info Cross-Region Inference
Models prefixed with `us.` use cross-region inference profiles, which provide better capacity and automatic failover across AWS regions. Models prefixed with `global.` route across all available regions worldwide.
:::

## Switching Models Mid-Session

Use the `/model` command during a conversation:

```
/model us.amazon.nova-pro-v1:0
/model deepseek.v3.2
/model us.anthropic.claude-opus-4-6-v1
```

## Diagnostics

```bash
hermes doctor
```

The doctor checks:
- Whether AWS credentials are available (env vars, IAM role, SSO)
- Whether `boto3` is installed
- Whether the Bedrock API is reachable (ListFoundationModels)
- Number of available models in your region
- Legacy Hermes Bedrock API-key configs that routed through `bedrock-mantle` or the old local `bedrock-native` proxy

Run `hermes doctor --fix` to repair legacy Bedrock API-key configs into native `provider: bedrock` configuration.

## Gateway (Messaging Platforms)

Bedrock works with all Hermes gateway platforms (Telegram, Discord, Slack, Feishu, etc.). Configure Bedrock as your provider, then start the gateway normally:

```bash
hermes gateway setup
hermes gateway start
```

The gateway reads `config.yaml` and uses the same Bedrock provider configuration.

## Troubleshooting

### "No API key found" / "No AWS credentials"

Check the selected `bedrock.auth_method`.

- `api_key`: set `AWS_BEARER_TOKEN_BEDROCK`.
- `profile`: set `bedrock.profile` or choose a profile in `hermes model`.
- `credentials`: set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
- `default_chain`: run `aws configure` or attach an IAM role to your compute instance.

### "Invocation of model ID ... with on-demand throughput isn't supported"

Use an **inference profile ID** (prefixed with `us.` or `global.`) instead of the bare foundation model ID. For example:
- ❌ `anthropic.claude-sonnet-4-6`
- ✅ `us.anthropic.claude-sonnet-4-6`

### "ThrottlingException"

You've hit the Bedrock per-model rate limit. Hermes automatically retries with backoff. To increase limits, request a quota increase in the [AWS Service Quotas console](https://console.aws.amazon.com/servicequotas/).
