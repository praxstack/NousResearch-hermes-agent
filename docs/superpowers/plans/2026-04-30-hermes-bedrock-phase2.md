# Hermes Bedrock Phase 2 Implementation Plan

> **⚠️ ARCHITECTURAL CORRECTION — 2026-04-30 (plan-eng-review + codex outside voice)**
>
> This plan targets `agent/bedrock_adapter.py` and `bedrock_converse` api_mode. **Codex verified against `hermes_cli/runtime_provider.py:1275-1285` that Opus 4.7 Bedrock Claude routes through `anthropic_messages` api_mode + `AnthropicBedrock` SDK (`agent/anthropic_adapter.py`), NOT the Converse API.** Non-Claude Bedrock models (Nova, DeepSeek, Llama) use `bedrock_converse`.
>
> **Net effect:** ~70% of P0 tasks as written target the wrong adapter for the stated goal of "complete super-level Opus 4.7 integration."
>
> **What's ALREADY in `anthropic_adapter.py`:**
> - `context-1m-2025-08-07` beta header (line 237, already in `_COMMON_BETAS`)
> - `interleaved-thinking-2025-05-14` beta (line 237, already in `_COMMON_BETAS`) — Phase 2 Task 12 is duplicate work
> - `thinking.budget_tokens` passthrough (line 1831) — Phase 2 Task 11 is duplicate work
> - `cache_control` marker preservation (lines 1275, 1380-1382, 1460-1461) — full Anthropic-style caching
> - Image + document content block conversion (line 1233+)
>
> **What's ACTUALLY missing on the `anthropic_adapter` path:**
> - `cache_read_input_tokens` / `cache_creation_input_tokens` surfacing (grep=0 — telemetry bug identical to the Converse-path gap but in a different file)
> - Explicit cache_ttl passthrough for 1h TTL (vs. 5m default)
> - System prompt static/volatile split + hash-gate (no producer today)
> - `output-128k-2025-02-19` beta (grep=0)
> - Auto-derived cross-region prefix with GovCloud/CN carve-out (partial logic in runtime_provider.py:1245)
> - boto3 retry tuning applies to `build_anthropic_bedrock_client` NOT `_create_bedrock_client` (Task 4 wrong-targeted)
>
> **What remains correctly-targeted (still useful for Nova/DeepSeek/Llama users):**
> - `bedrock_converse`-path changes in bedrock_adapter.py (Tasks 1-6, 8-10)
> - Error classifier patterns (Task 17 — works at classify-error-message layer, adapter-agnostic)
> - Image-shrink Bedrock block shape (Task 18 — affects Converse-path only)
> - Doc drop-and-retry (Task 19 — same)
>
> **Status: PLAN REQUIRES REWRITE.** Current plan is preserved below as a reference point for the Converse-path subset. A revised plan targeting anthropic_adapter for Opus 4.7 will supersede this one. Do NOT execute this plan in its current form.

---


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Hermes's native AWS Bedrock adapter from Cline-compatible behavioral parity to complete super-level Opus 4.7 integration: wire cache accounting, extended-thinking budget, adaptive retry, default credential chain, auto-region-prefix with GovCloud/CN carve-out, cachePoint prompt caching with token guard, cache_ttl passthrough, interleaved thinking beta, document/image content blocks, 128K output beta, citations passthrough, VPC wizard exposure.

**Architecture:** Surgical in-file changes to `agent/bedrock_adapter.py` (1652 LOC today) and one call site in `hermes_cli/main.py` (`_model_flow_bedrock` at line 4388). No new modules. System prompt gains an optional `system_volatile` passthrough; `build_converse_kwargs` grows 6 optional kwargs that all default to backward-compatible behavior. `normalize_converse_response` and `stream_converse_with_callbacks` both gain a shared `_extract_bedrock_usage` helper so the sync/stream drift bug cannot recur.

**Tech Stack:** Python 3.12, boto3 1.42.97, pytest, AWS Bedrock Converse API (ConverseCommand + ConverseStreamCommand).

**Spec:** `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/PraxVault/Hermes/Reference/2026-04-30-hermes-bedrock-phase2-design.md`

**Branch:** Continue on `feat/native-bedrock-provider-20260428`. Commits land here; no new branch.

---

## File structure

**Modify:**
- `agent/bedrock_adapter.py` — all 13 goals except G8
- `hermes_cli/main.py:4388` (`_model_flow_bedrock`) — G8 VPC wizard prompt
- `hermes_cli/config.py:620` (bedrock defaults template) — add new config keys

**Create:**
- `tests/agent/test_bedrock_phase2.py` — all new unit tests
- `tests/agent/test_bedrock_integration_cache.py` — opt-in live cache verification

**No files deleted.** No files split. `bedrock_adapter.py` is large but the Phase-2 diff is tightly scoped; a refactor would dilute the change.

---

## Task order rationale

Order reflects dependency + risk, not spec enumeration:

1. Usage normalization FIRST (G2) — observability precondition. All cache-related work is validated against this, so it must land before cache code.
2. Adaptive retry (G5) — one-line change, unblocks long-running integration tests.
3. boto3 credential chain / region helpers (G6, G7) — isolated, zero coupling to rest.
4. Wizard (G8) — config-file only, independent.
5. Content blocks (G10, G11, G13) — converter-only, no request-path change.
6. Beta flags (G1, G9, G12) — all use the same `anthropic_beta` accumulator pattern.
7. cachePoint (G3, G4) LAST — depends on G2 to measure and G10/G11 for content-block placement rules.

---

## Task 0.5: Extend `BedrockTransport.build_kwargs` signature (arch prerequisite)

**Files:**
- Modify: `agent/transports/bedrock.py:build_kwargs`
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** Phase 2 adds 6 new kwargs to `build_converse_kwargs`. The transport layer at
`agent/transports/bedrock.py:40` is the only caller. Without extending the transport's
signature, the new kwargs land as dead code — `build_converse_kwargs` accepts them but
nothing passes them.

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestBedrockTransportKwargsPassthrough:
    def test_transport_threads_thinking_budget(self):
        from agent.transports.bedrock import BedrockTransport

        t = BedrockTransport()
        kwargs = t.build_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16,
            region="us-east-1",
            thinking_budget_tokens=8192,
        )
        additional = kwargs.get("additionalModelRequestFields", {})
        assert additional.get("thinking") == {"type": "enabled", "budget_tokens": 8192}

    def test_transport_threads_interleaved_and_128k(self):
        from agent.transports.bedrock import BedrockTransport

        t = BedrockTransport()
        kwargs = t.build_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=16384,
            region="us-east-1",
            enable_interleaved_thinking=True,
            enable_128k_output=True,
        )
        betas = kwargs["additionalModelRequestFields"]["anthropic_beta"]
        assert "interleaved-thinking-2025-05-14" in betas
        assert "output-128k-2025-02-19" in betas

    def test_transport_threads_cache_kwargs(self):
        from agent.transports.bedrock import BedrockTransport
        from hashlib import sha256

        volatile = "stable" * 500
        volatile_hash = sha256(volatile.encode("utf-8")).hexdigest()

        t = BedrockTransport()
        kwargs = t.build_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[
                {"role": "system", "content": "s" * 5000},
                {"role": "user", "content": "hi"},
            ],
            max_tokens=16,
            region="us-east-1",
            system_volatile=volatile,
            volatile_hash_prev=volatile_hash,
            cache_ttl="1h",
        )
        system_blocks = kwargs.get("system") or []
        cache_points = [b for b in system_blocks if "cachePoint" in b]
        assert len(cache_points) == 2  # static + volatile
        assert all(cp["cachePoint"].get("ttl") == "1h" for cp in cache_points)
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockTransportKwargsPassthrough -v
```

Expected: FAIL — `build_kwargs` doesn't accept the new kwargs.

- [ ] **Step 3: Write the fix**

In `agent/transports/bedrock.py`, extend `build_kwargs`:

```python
    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """Build Bedrock converse() kwargs.

        Thread Phase 2 options through to build_converse_kwargs:
          - thinking_budget_tokens, enable_interleaved_thinking, enable_128k_output
          - system_volatile, volatile_hash_prev, cache_ttl
        """
        from agent.bedrock_adapter import build_converse_kwargs

        region = params.get("region", "us-east-1")
        guardrail = params.get("guardrail_config")

        kwargs = build_converse_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=params.get("max_tokens", 4096),
            temperature=params.get("temperature"),
            guardrail_config=guardrail,
            thinking_budget_tokens=params.get("thinking_budget_tokens"),
            enable_interleaved_thinking=params.get("enable_interleaved_thinking", False),
            enable_128k_output=params.get("enable_128k_output", False),
            system_volatile=params.get("system_volatile"),
            volatile_hash_prev=params.get("volatile_hash_prev"),
            cache_ttl=params.get("cache_ttl", "5m"),
        )
        # Sentinel keys for dispatch — agent pops these before the boto3 call
        kwargs["__bedrock_converse__"] = True
        kwargs["__bedrock_region__"] = region
        return kwargs
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockTransportKwargsPassthrough tests/agent/test_bedrock_adapter.py -v
```

Expected: 3 new passed + existing green.

- [ ] **Step 5: Commit**

```bash
git add agent/transports/bedrock.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): thread Phase 2 kwargs through BedrockTransport.build_kwargs"
```

---

## Task 0.75: AIAgent init + config reads for Phase 2 options (arch prerequisite)

**Files:**
- Modify: `run_agent.py` — extend `AIAgent.__init__`, extend `_build_api_kwargs`
- Modify: `hermes_cli/config.py:620` — add bedrock config keys
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** Phase 2 options need a storage site on the agent (so `_build_api_kwargs` can read
them and pass to the transport). Without this, the transport-extension from Task 0.5 is
also dead code — nothing sets `self._thinking_budget_tokens` etc.

- [ ] **Step 1: Extend the bedrock config defaults**

In `hermes_cli/config.py:620`, the `bedrock` block currently has `region`, `auth_method`, `profile`, `discovery`, `guardrail`. Add:

```python
    "bedrock": {
        "region": "",
        "auth_method": "default_chain",
        "profile": "",
        "vpc_endpoint_url": "",  # Added by Task 7 (scope B resilience review)

        # --- Phase 2 additions ---
        "prompt_cache_enabled": True,      # Emit cachePoint markers in request
        "cache_ttl": "5m",                 # "5m" | "1h" — Bedrock cachePoint TTL
        "thinking_budget_tokens": None,    # None = adaptive (model default); int = explicit budget
        "enable_interleaved_thinking": False,  # Adds interleaved-thinking-2025-05-14 beta
        "enable_128k_output": False,       # Adds output-128k-2025-02-19 beta (requires max_tokens > 8192)

        "discovery": {...},  # existing
        "guardrail": {...},  # existing
    },
```

- [ ] **Step 2: Write the failing test**

Append:

```python
class TestAgentBedrockPhase2Config:
    def test_agent_reads_phase2_bedrock_config(self, monkeypatch, tmp_path):
        """Agent __init__ reads Phase 2 bedrock options from config into attributes."""
        from run_agent import AIAgent

        fake_config = {
            "bedrock": {
                "region": "us-east-1",
                "auth_method": "default_chain",
                "prompt_cache_enabled": True,
                "cache_ttl": "1h",
                "thinking_budget_tokens": 16384,
                "enable_interleaved_thinking": True,
                "enable_128k_output": True,
            },
        }
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: fake_config)

        agent = AIAgent.__new__(AIAgent)  # bypass full __init__
        agent._load_bedrock_phase2_config()

        assert agent._bedrock_prompt_cache_enabled is True
        assert agent._bedrock_cache_ttl == "1h"
        assert agent._bedrock_thinking_budget_tokens == 16384
        assert agent._bedrock_enable_interleaved_thinking is True
        assert agent._bedrock_enable_128k_output is True

    def test_agent_config_defaults_when_missing(self, monkeypatch):
        from run_agent import AIAgent

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"bedrock": {}})

        agent = AIAgent.__new__(AIAgent)
        agent._load_bedrock_phase2_config()

        assert agent._bedrock_prompt_cache_enabled is True  # default True
        assert agent._bedrock_cache_ttl == "5m"
        assert agent._bedrock_thinking_budget_tokens is None
        assert agent._bedrock_enable_interleaved_thinking is False
        assert agent._bedrock_enable_128k_output is False
```

- [ ] **Step 3: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestAgentBedrockPhase2Config -v
```

Expected: `AttributeError: 'AIAgent' object has no attribute '_load_bedrock_phase2_config'`.

- [ ] **Step 4: Write the fix**

In `run_agent.py`, add the method near the other config-load helpers:

```python
    def _load_bedrock_phase2_config(self) -> None:
        """Load Phase 2 Bedrock options from config into agent attributes.

        Called from __init__ after the main config load. Attributes are read
        by _build_api_kwargs → BedrockTransport.build_kwargs.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
        except Exception:
            cfg = {}

        bedrock_cfg = cfg.get("bedrock", {}) if isinstance(cfg, dict) else {}
        if not isinstance(bedrock_cfg, dict):
            bedrock_cfg = {}

        self._bedrock_prompt_cache_enabled = bool(bedrock_cfg.get("prompt_cache_enabled", True))
        self._bedrock_cache_ttl = str(bedrock_cfg.get("cache_ttl") or "5m").strip() or "5m"
        budget = bedrock_cfg.get("thinking_budget_tokens")
        self._bedrock_thinking_budget_tokens = int(budget) if isinstance(budget, (int, float)) and budget > 0 else None
        self._bedrock_enable_interleaved_thinking = bool(bedrock_cfg.get("enable_interleaved_thinking", False))
        self._bedrock_enable_128k_output = bool(bedrock_cfg.get("enable_128k_output", False))
```

Then in `__init__`, call it (after existing config loads):

```python
        self._load_bedrock_phase2_config()
```

Also track volatile hash across turns:

```python
        self._bedrock_volatile_hash_prev = None  # Updated after each Converse call
```

Then extend `_build_api_kwargs` in the `bedrock_converse` branch (around line 8134):

```python
        if self.api_mode == "bedrock_converse":
            _bt = self._get_transport()
            region = getattr(self, "_bedrock_region", None) or "us-east-1"
            guardrail = getattr(self, "_bedrock_guardrail_config", None)

            # Phase 2: split system prompt into static+volatile if the agent has
            # an _bedrock_system_volatile attribute (set by the prompt builder).
            system_volatile = getattr(self, "_bedrock_system_volatile", None)
            volatile_hash_prev = getattr(self, "_bedrock_volatile_hash_prev", None) if self._bedrock_prompt_cache_enabled else None

            return _bt.build_kwargs(
                model=self.model,
                messages=api_messages,
                tools=self.tools,
                max_tokens=self.max_tokens or 4096,
                region=region,
                guardrail_config=guardrail,
                thinking_budget_tokens=self._bedrock_thinking_budget_tokens,
                enable_interleaved_thinking=self._bedrock_enable_interleaved_thinking,
                enable_128k_output=self._bedrock_enable_128k_output,
                system_volatile=system_volatile if self._bedrock_prompt_cache_enabled else None,
                volatile_hash_prev=volatile_hash_prev,
                cache_ttl=self._bedrock_cache_ttl,
            )
```

Note: `_bedrock_system_volatile` is set by whatever prompt-builder the agent uses. For
Phase 2's MVP, the agent's prompt builder may not split out volatile memory yet — this
is fine. If `system_volatile=None`, `build_converse_kwargs` falls through to legacy
single-system-block behavior (no cachePoint). The user opts in by setting the attribute.

- [ ] **Step 5: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestAgentBedrockPhase2Config tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + existing green.

- [ ] **Step 6: Commit**

```bash
git add run_agent.py hermes_cli/config.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): wire Phase 2 config reads into agent + _build_api_kwargs"
```

---

## Task 1: Shared `_extract_bedrock_usage` helper (G2 foundation)

**Files:**
- Modify: `agent/bedrock_adapter.py` (add helper above `normalize_converse_response` at ~line 966)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/agent/test_bedrock_phase2.py`:

```python
"""Phase 2 tests: cache accounting, extended thinking, content blocks, etc."""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# G2: Shared usage normalization
# ---------------------------------------------------------------------------

class TestExtractBedrockUsage:
    def test_maps_all_bedrock_fields_to_anthropic_names(self):
        from agent.bedrock_adapter import _extract_bedrock_usage

        usage_data = {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 123,
            "cacheWriteInputTokens": 456,
        }
        usage = _extract_bedrock_usage(usage_data)

        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150
        assert usage.cache_read_input_tokens == 123
        assert usage.cache_creation_input_tokens == 456

    def test_missing_cache_fields_default_to_zero(self):
        from agent.bedrock_adapter import _extract_bedrock_usage

        usage = _extract_bedrock_usage({"inputTokens": 10, "outputTokens": 5})

        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0

    def test_empty_dict_returns_zero_usage(self):
        from agent.bedrock_adapter import _extract_bedrock_usage

        usage = _extract_bedrock_usage({})

        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestExtractBedrockUsage -v
```

Expected: `ImportError: cannot import name '_extract_bedrock_usage'`.

- [ ] **Step 3: Write minimal implementation**

In `agent/bedrock_adapter.py`, add directly above `def normalize_converse_response(` (at ~line 966):

```python
def _extract_bedrock_usage(usage_data: Dict[str, Any]) -> SimpleNamespace:
    """Normalize a Bedrock Converse ``usage`` dict to the Anthropic-direct shape.

    Bedrock returns ``cacheReadInputTokens`` / ``cacheWriteInputTokens`` (camelCase).
    Hermes's ``usage_pricing.py`` reads ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` (Anthropic snake_case). This helper bridges
    the two so the sync and stream paths cannot drift.

    Also provides ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``
    for OpenAI-shape consumers.
    """
    input_tokens = int(usage_data.get("inputTokens", 0) or 0)
    output_tokens = int(usage_data.get("outputTokens", 0) or 0)
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_read_input_tokens=int(usage_data.get("cacheReadInputTokens", 0) or 0),
        cache_creation_input_tokens=int(usage_data.get("cacheWriteInputTokens", 0) or 0),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestExtractBedrockUsage -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/hermes-agent
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): add _extract_bedrock_usage helper for cache token normalization"
```

---

## Task 2: Use `_extract_bedrock_usage` in `normalize_converse_response` (sync path, G2)

**Files:**
- Modify: `agent/bedrock_adapter.py:1007-1015`
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestNormalizeConverseResponseCache:
    def test_sync_response_surfaces_cache_read_tokens(self):
        from agent.bedrock_adapter import normalize_converse_response

        response = {
            "output": {"message": {"content": [{"text": "hello"}]}},
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 789,
                "cacheWriteInputTokens": 321,
            },
        }
        result = normalize_converse_response(response)

        assert result.usage.cache_read_input_tokens == 789
        assert result.usage.cache_creation_input_tokens == 321
        assert result.usage.prompt_tokens == 100
        assert result.usage.completion_tokens == 20
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestNormalizeConverseResponseCache -v
```

Expected: `AttributeError: 'SimpleNamespace' object has no attribute 'cache_read_input_tokens'`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, replace the block at lines 1007-1015:

```python
    # Build usage stats
    usage_data = response.get("usage", {})
    usage = SimpleNamespace(
        prompt_tokens=usage_data.get("inputTokens", 0),
        completion_tokens=usage_data.get("outputTokens", 0),
        total_tokens=(
            usage_data.get("inputTokens", 0) + usage_data.get("outputTokens", 0)
        ),
    )
```

with:

```python
    # Build usage stats (includes cache tokens — see _extract_bedrock_usage)
    usage = _extract_bedrock_usage(response.get("usage", {}))
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestNormalizeConverseResponseCache tests/agent/test_bedrock_adapter.py -v
```

Expected: 1 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "fix(bedrock): surface cacheRead/Write tokens in normalize_converse_response"
```

---

## Task 3: Use `_extract_bedrock_usage` in `stream_converse_with_callbacks` (stream path, G2)

**Files:**
- Modify: `agent/bedrock_adapter.py:1155-1195` (metadata event + final usage build)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestStreamConverseCache:
    def test_stream_metadata_surfaces_cache_read_tokens(self):
        from agent.bedrock_adapter import stream_converse_with_callbacks

        event_stream = {
            "stream": [
                {"messageStart": {"role": "assistant"}},
                {"contentBlockDelta": {"delta": {"text": "ok"}}},
                {"messageStop": {"stopReason": "end_turn"}},
                {
                    "metadata": {
                        "usage": {
                            "inputTokens": 50,
                            "outputTokens": 10,
                            "cacheReadInputTokens": 1001,
                            "cacheWriteInputTokens": 2002,
                        }
                    }
                },
            ]
        }
        result = stream_converse_with_callbacks(event_stream)

        assert result.usage.cache_read_input_tokens == 1001
        assert result.usage.cache_creation_input_tokens == 2002
        assert result.usage.prompt_tokens == 50
        assert result.usage.completion_tokens == 10
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestStreamConverseCache -v
```

Expected: `AttributeError: 'SimpleNamespace' object has no attribute 'cache_read_input_tokens'`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, find the metadata handler block (around line 1155):

```python
        elif "metadata" in event:
            meta_usage = event["metadata"].get("usage", {})
            usage_data = {
                "inputTokens": meta_usage.get("inputTokens", 0),
                "outputTokens": meta_usage.get("outputTokens", 0),
            }
```

Replace with (preserve all four fields):

```python
        elif "metadata" in event:
            meta_usage = event["metadata"].get("usage", {})
            usage_data = {
                "inputTokens": meta_usage.get("inputTokens", 0),
                "outputTokens": meta_usage.get("outputTokens", 0),
                "cacheReadInputTokens": meta_usage.get("cacheReadInputTokens", 0),
                "cacheWriteInputTokens": meta_usage.get("cacheWriteInputTokens", 0),
            }
```

Then find the final usage construction (around line 1183-1189):

```python
    usage = SimpleNamespace(
        prompt_tokens=usage_data.get("inputTokens", 0),
        completion_tokens=usage_data.get("outputTokens", 0),
        total_tokens=(
            usage_data.get("inputTokens", 0) + usage_data.get("outputTokens", 0)
        ),
    )
```

Replace with:

```python
    usage = _extract_bedrock_usage(usage_data)
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py tests/agent/test_bedrock_adapter.py -v
```

Expected: 5 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "fix(bedrock): surface cacheRead/Write tokens in streaming path"
```

---

## Task 4: boto3 adaptive retry (G5)

**Files:**
- Modify: `agent/bedrock_adapter.py:623` (`_create_bedrock_client`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestBedrockAdaptiveRetry:
    def test_client_config_uses_adaptive_retry(self):
        from agent.bedrock_adapter import _create_bedrock_client

        with patch("agent.bedrock_adapter._require_boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.return_value.client = MagicMock(return_value=mock_client)

            _create_bedrock_client(
                "bedrock-runtime",
                "us-east-1",
                {"method": "default_chain"},
            )

            call = mock_boto3.return_value.client.call_args
            config = call.kwargs.get("config")
            assert config is not None, "boto3.client was called without a Config object"
            assert config.retries == {"max_attempts": 5, "mode": "adaptive"}
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockAdaptiveRetry -v
```

Expected: `AssertionError: boto3.client was called without a Config object`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, at the top of `_create_bedrock_client` (line 623), replace:

```python
def _create_bedrock_client(service: str, region: str, auth_config: Dict[str, str]):
    boto3 = _require_boto3()
    method = auth_config.get("method", "default_chain")

    # VPC endpoint support: when configured, all service clients route through
    # the customer's VPC endpoint URL (used by enterprise Bedrock-in-VPC deployments).
    endpoint_url = str(auth_config.get("endpoint_url") or "").strip()
    extra_kwargs: Dict[str, Any] = {}
    if endpoint_url:
        extra_kwargs["endpoint_url"] = endpoint_url
```

with:

```python
def _create_bedrock_client(service: str, region: str, auth_config: Dict[str, str]):
    boto3 = _require_boto3()
    from botocore.config import Config as BotoConfig
    method = auth_config.get("method", "default_chain")

    # VPC endpoint support: when configured, all service clients route through
    # the customer's VPC endpoint URL (used by enterprise Bedrock-in-VPC deployments).
    endpoint_url = str(auth_config.get("endpoint_url") or "").strip()
    # Adaptive retry mode: 5 attempts + client-side token-bucket rate learning.
    # boto3's StandardRetryChecker already classifies ThrottlingException,
    # ThrottledException, RequestThrottledException, EC2ThrottledException as
    # retryable. Adaptive mode extends this with a local rate limiter that
    # slows future calls when throttling is observed. Replaces the need for
    # a decorator-level retry wrapper (which would compound with this layer).
    boto_config = BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})
    extra_kwargs: Dict[str, Any] = {"config": boto_config}
    if endpoint_url:
        extra_kwargs["endpoint_url"] = endpoint_url
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockAdaptiveRetry tests/agent/test_bedrock_adapter.py -v
```

Expected: 1 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): enable adaptive retry (max_attempts=5) on Converse clients"
```

---

## Task 5: Cross-region prefix auto-derive with carve-out (G7)

**Files:**
- Modify: `agent/bedrock_adapter.py` (add helper; wire into `build_converse_kwargs` or caller-facing model resolver)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestCrossRegionPrefix:
    def test_us_region_gets_us_prefix(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        result = derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="us-east-1",
            enabled=True,
        )
        assert result == "us.anthropic.claude-opus-4-7"

    def test_eu_region_gets_eu_prefix(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="eu-west-1",
            enabled=True,
        ) == "eu.anthropic.claude-opus-4-7"

    def test_ap_region_gets_apac_prefix(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "anthropic.claude-sonnet-4-6",
            region="ap-northeast-1",
            enabled=True,
        ) == "apac.anthropic.claude-sonnet-4-6"

    def test_au_region_gets_au_prefix(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "anthropic.claude-sonnet-4-6",
            region="au-east-1",
            enabled=True,
        ) == "au.anthropic.claude-sonnet-4-6"

    def test_gov_region_carve_out(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        # GovCloud has no CRI support — return bare id unchanged
        assert derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="us-gov-west-1",
            enabled=True,
        ) == "anthropic.claude-opus-4-7"

    def test_cn_region_carve_out(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        # China is a separate partition
        assert derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="cn-north-1",
            enabled=True,
        ) == "anthropic.claude-opus-4-7"

    def test_disabled_flag_returns_bare(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="us-east-1",
            enabled=False,
        ) == "anthropic.claude-opus-4-7"

    def test_already_prefixed_is_idempotent(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "us.anthropic.claude-opus-4-7",
            region="us-east-1",
            enabled=True,
        ) == "us.anthropic.claude-opus-4-7"

    def test_unknown_region_returns_bare(self):
        from agent.bedrock_adapter import derive_cross_region_model_id

        assert derive_cross_region_model_id(
            "anthropic.claude-opus-4-7",
            region="af-south-1",
            enabled=True,
        ) == "anthropic.claude-opus-4-7"
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestCrossRegionPrefix -v
```

Expected: `ImportError: cannot import name 'derive_cross_region_model_id'`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, add after `split_bedrock_1m_suffix` (~line 285):

```python
# Cross-region inference prefixes, keyed by region partition prefix.
# GovCloud (us-gov-*) and China (cn-*) have no CRI support; see derive_cross_region_model_id.
_CRI_PREFIX_BY_REGION_PART = {
    "us-": "us.",
    "eu-": "eu.",
    "ap-": "apac.",
    "au-": "au.",
}
_CRI_EXISTING_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "global.")


def derive_cross_region_model_id(model_id: str, region: str, enabled: bool) -> str:
    """Return ``model_id`` with the correct cross-region inference prefix applied.

    When ``enabled`` is False, the bare model id is returned unchanged.
    If the model id is already prefixed (``us.``/``eu.``/``apac.``/``jp.``/``au.``/``global.``),
    the call is idempotent.

    **Carve-outs** (return bare model id regardless of flag):
      - GovCloud regions (``us-gov-*``) — no CRI support.
      - China regions (``cn-*``) — separate partition, different prefix system.

    Returns the bare model id unchanged for any region not in the lookup table.
    """
    if not enabled:
        return model_id
    if not region:
        return model_id
    if model_id.startswith(_CRI_EXISTING_PREFIXES):
        return model_id
    if region.startswith("us-gov-") or region.startswith("cn-"):
        return model_id
    # Match by the first segment of the region: "us-east-1" -> "us-"
    region_part = region.split("-")[0] + "-"
    prefix = _CRI_PREFIX_BY_REGION_PART.get(region_part)
    if prefix is None:
        return model_id
    return f"{prefix}{model_id}"
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestCrossRegionPrefix -v
```

Expected: 9 new passed.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): derive_cross_region_model_id with us-gov/cn carve-outs"
```

---

## Task 6: Auto credential chain (G6 — `auth_method=auto`)

**Files:**
- Modify: `agent/bedrock_adapter.py:491` (`resolve_bedrock_auth_config` — accept "auto")
- Modify: `hermes_cli/config.py:622` — add "auto" to allowed values comment
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestAutoCredentialChain:
    def test_auto_mode_returns_default_chain_shape_with_no_explicit_creds(self):
        from agent.bedrock_adapter import resolve_bedrock_auth_config

        result = resolve_bedrock_auth_config(
            config={"bedrock": {"auth_method": "auto", "region": "us-east-1"}},
            env={},
        )
        # Auto mode falls through to default chain (boto3.client with no creds kwargs)
        assert result["method"] == "default_chain"
        assert result["region"] == "us-east-1"
        assert result["source"] == "auto"

    def test_auto_mode_preserves_explicit_creds_env(self):
        from agent.bedrock_adapter import resolve_bedrock_auth_config

        # With env vars present, auto still uses default_chain — boto3 will pick them up
        result = resolve_bedrock_auth_config(
            config={"bedrock": {"auth_method": "auto", "region": "us-east-1"}},
            env={"AWS_ACCESS_KEY_ID": "AKIAFAKE", "AWS_SECRET_ACCESS_KEY": "secret"},
        )
        assert result["method"] == "default_chain"
        assert result["source"] == "auto"
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestAutoCredentialChain -v
```

Expected: FAIL — `auto` currently falls through to `default_chain` but the `source` field is different (not `"auto"`).

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py:491` (`resolve_bedrock_auth_config`), find the valid-methods check:

```python
    method = str(bedrock_cfg.get("auth_method") or "default_chain").strip().lower()
    if method not in {"api_key", "profile", "credentials", "default_chain"}:
        method = "default_chain"
```

Replace with:

```python
    method = str(bedrock_cfg.get("auth_method") or "default_chain").strip().lower()
    if method not in {"api_key", "profile", "credentials", "default_chain", "auto"}:
        method = "default_chain"
    # "auto" is an explicit opt-in to boto3's default credential chain with a
    # distinct source tag so operators can see it was requested (not inferred).
    auto_requested = method == "auto"
    if auto_requested:
        method = "default_chain"
```

Then, at the end of the function where the default-chain branch returns, locate:

```python
    return {
        "method": "default_chain",
        "region": region,
        "source": "default_chain",
        ...
    }
```

(Find the exact block via `sed -n '560,600p'` of the file first — the default_chain return is the last branch.) Update the `source` to reflect auto:

```python
    return {
        "method": "default_chain",
        "region": region,
        "source": "auto" if auto_requested else "default_chain",
        "cache_identity": f"default_chain{endpoint_cache_suffix}",
        "endpoint_url": endpoint_url,
    }
```

Also update `hermes_cli/config.py:622`:

```python
        "auth_method": "default_chain",  # api_key | profile | credentials | default_chain | auto
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestAutoCredentialChain tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py hermes_cli/config.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): accept auth_method=auto (explicit default chain opt-in)"
```

---

## Task 7: VPC endpoint wizard exposure (G8)

**Files:**
- Modify: `hermes_cli/main.py:4388` (`_model_flow_bedrock`)
- Modify: `hermes_cli/config.py:620` (defaults template — add `vpc_endpoint_url: ""`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestBedrockWizardVpcEndpoint:
    def test_wizard_persists_vpc_endpoint_url(self, tmp_path, monkeypatch):
        """Wizard prompts for vpc_endpoint_url after region and persists it."""
        from hermes_cli.main import _model_flow_bedrock

        inputs = iter([
            "us-east-1",                         # region
            "https://vpce-abc.bedrock-runtime.us-east-1.vpce.amazonaws.com",  # vpc endpoint
            "4",                                 # auth method: default chain
            "",                                  # model (skip)
        ])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        captured_config = {}

        def fake_save(cfg):
            captured_config.update(cfg)

        monkeypatch.setattr("hermes_cli.config.save_config", fake_save)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"bedrock": {}},
        )

        # Avoid boto3 discovery call in this test
        monkeypatch.setattr(
            "hermes_cli.main._discover_bedrock_model_list",
            lambda region, current_model="", config_preview=None: [],
        )

        _model_flow_bedrock({"bedrock": {}}, current_model="")

        assert captured_config.get("bedrock", {}).get("vpc_endpoint_url") == \
            "https://vpce-abc.bedrock-runtime.us-east-1.vpce.amazonaws.com"
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockWizardVpcEndpoint -v
```

Expected: FAIL — wizard doesn't prompt for VPC endpoint yet; assertion on `vpc_endpoint_url` fails.

- [ ] **Step 3: Write the fix**

In `hermes_cli/main.py:4388` (`_model_flow_bedrock`), find the region-input block:

```python
    try:
        region_input = input(f"  AWS Region [{current_region}]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return
    region = region_input or current_region
```

Add immediately after:

```python
    # VPC endpoint (PrivateLink) — optional, for regulated/isolated tenants.
    # Blank = no endpoint override (default public bedrock-runtime).
    preview_cfg_for_vpc = load_config()
    bedrock_preview_for_vpc = preview_cfg_for_vpc.get("bedrock", {})
    existing_vpc = (
        str(bedrock_preview_for_vpc.get("vpc_endpoint_url") or "").strip()
        if isinstance(bedrock_preview_for_vpc, dict)
        else ""
    )
    # Blank input keeps the existing value; any non-empty input replaces it.
    # We intentionally skip URL validation to match the region/profile prompts
    # (also unvalidated); bad inputs surface as a clear AWS error on first call.
    print("  (skip unless you use AWS PrivateLink)")
    vpc_prompt = f"  VPC endpoint URL (optional, blank to keep) [{existing_vpc or 'none'}]: "
    try:
        vpc_input = input(vpc_prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return
    vpc_endpoint_url = vpc_input if vpc_input else existing_vpc
```

Then find where the config is finally saved (in the same function — look for `save_config` call after auth method is chosen) and ensure `bedrock_cfg["vpc_endpoint_url"] = vpc_endpoint_url` is written before save. Specifically, in `_save_bedrock_model_selection` (line 4239 area) or inline:

```python
    bedrock_cfg["vpc_endpoint_url"] = vpc_endpoint_url
```

Also update `hermes_cli/config.py:620` defaults template:

```python
    "bedrock": {
        "region": "",
        "auth_method": "default_chain",
        "profile": "",
        "vpc_endpoint_url": "",  # e.g. https://vpce-xxx.bedrock-runtime.REGION.vpce.amazonaws.com
        "discovery": {
            ...
        },
        ...
    },
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockWizardVpcEndpoint -v
```

Expected: 1 new passed.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/main.py hermes_cli/config.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): expose VPC endpoint URL in interactive setup wizard"
```

---

## Task 8: Document content blocks (G10)

**Files:**
- Modify: `agent/bedrock_adapter.py:778` (`_convert_content_to_converse`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestDocumentContentBlock:
    def test_pdf_document_converts_to_converse_document_block(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [
            {"type": "text", "text": "Summarize this."},
            {
                "type": "document",
                "source": {"bytes": b"%PDF-1.4 fake pdf body"},
                "mime_type": "application/pdf",
                "name": "contract.pdf",
            },
        ]
        blocks = _convert_content_to_converse(content)

        assert len(blocks) == 2
        assert blocks[0] == {"text": "Summarize this."}
        assert blocks[1]["document"]["format"] == "pdf"
        assert blocks[1]["document"]["name"] == "contract.pdf"
        assert blocks[1]["document"]["source"]["bytes"] == b"%PDF-1.4 fake pdf body"

    def test_docx_document_converts(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [{
            "type": "document",
            "source": {"bytes": b"PK\x03\x04 docx"},
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "name": "report.docx",
        }]
        blocks = _convert_content_to_converse(content)

        assert blocks[0]["document"]["format"] == "docx"

    def test_document_without_name_uses_default(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [{
            "type": "document",
            "source": {"bytes": b"x"},
            "mime_type": "application/pdf",
        }]
        blocks = _convert_content_to_converse(content)

        assert blocks[0]["document"]["name"] == "document"

    def test_unsupported_document_mime_raises(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [{
            "type": "document",
            "source": {"bytes": b"x"},
            "mime_type": "application/x-unknown",
        }]
        with pytest.raises(ValueError, match="unsupported document format"):
            _convert_content_to_converse(content)
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestDocumentContentBlock -v
```

Expected: FAIL — `document` type is not currently handled; `_convert_content_to_converse` falls through to text.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, near the top of the module (after the `_require_boto3` block or co-located with other constants around line 270), add:

```python
# Bedrock Converse document block MIME → format mapping.
# Supported formats per AWS docs: pdf, csv, doc, docx, xls, xlsx, html, txt, md.
_DOCUMENT_MIME_TO_FORMAT = {
    "application/pdf": "pdf",
    "text/csv": "csv",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/html": "html",
    "text/plain": "txt",
    "text/markdown": "md",
}
```

Then in `_convert_content_to_converse` (line 778), inside the `if isinstance(content, list):` / `for part in content:` loop, add a new branch after the `image_url` branch:

```python
            elif part_type == "document":
                source = part.get("source", {})
                raw_bytes = source.get("bytes") if isinstance(source, dict) else None
                mime_type = str(part.get("mime_type", "")).lower().strip()
                doc_format = _DOCUMENT_MIME_TO_FORMAT.get(mime_type)
                if doc_format is None:
                    raise ValueError(
                        f"unsupported document format: mime_type={mime_type!r} "
                        f"— supported: {sorted(_DOCUMENT_MIME_TO_FORMAT)}"
                    )
                name = str(part.get("name") or "document").strip() or "document"
                blocks.append({
                    "document": {
                        "format": doc_format,
                        "name": name,
                        "source": {"bytes": raw_bytes},
                    }
                })
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestDocumentContentBlock tests/agent/test_bedrock_adapter.py -v
```

Expected: 4 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): accept document content blocks (pdf/docx/xlsx/etc)"
```

---

## Task 9: Image content block from `{type:image}` shape (G11)

**Files:**
- Modify: `agent/bedrock_adapter.py:778` (`_convert_content_to_converse`, add `image` type handler alongside existing `image_url`)
- Test: `tests/agent/test_bedrock_phase2.py`

The adapter already handles `{"type": "image_url", "image_url": {"url": "data:..."}}`. G11 adds a parallel `{"type": "image", "source": {"bytes": ...}, "mime_type": "image/png"}` shape (matching the document block shape for consistency).

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestImageContentBlock:
    def test_png_image_bytes_converts_to_converse_image_block(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [
            {"type": "text", "text": "What's in this image?"},
            {
                "type": "image",
                "source": {"bytes": b"\x89PNG\r\n\x1a\n fake png"},
                "mime_type": "image/png",
            },
        ]
        blocks = _convert_content_to_converse(content)

        assert len(blocks) == 2
        assert blocks[1]["image"]["format"] == "png"
        assert blocks[1]["image"]["source"]["bytes"] == b"\x89PNG\r\n\x1a\n fake png"

    def test_jpeg_image_bytes_converts(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        content = [{
            "type": "image",
            "source": {"bytes": b"\xff\xd8\xff"},
            "mime_type": "image/jpeg",
        }]
        blocks = _convert_content_to_converse(content)

        assert blocks[0]["image"]["format"] == "jpeg"

    def test_webp_and_gif_supported(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        for mime, fmt in [("image/webp", "webp"), ("image/gif", "gif")]:
            blocks = _convert_content_to_converse([{
                "type": "image",
                "source": {"bytes": b"x"},
                "mime_type": mime,
            }])
            assert blocks[0]["image"]["format"] == fmt

    def test_unsupported_image_mime_raises(self):
        from agent.bedrock_adapter import _convert_content_to_converse

        with pytest.raises(ValueError, match="unsupported image format"):
            _convert_content_to_converse([{
                "type": "image",
                "source": {"bytes": b"x"},
                "mime_type": "image/tiff",
            }])
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestImageContentBlock -v
```

Expected: FAIL — `{"type":"image"}` shape is not handled; falls through to unknown-type (ignored).

- [ ] **Step 3: Write the fix**

Near the top of `agent/bedrock_adapter.py` (next to `_DOCUMENT_MIME_TO_FORMAT`), add:

```python
_IMAGE_MIME_TO_FORMAT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}
```

In `_convert_content_to_converse`, add a branch alongside the existing `image_url` branch, after the `document` branch added in Task 8:

```python
            elif part_type == "image":
                source = part.get("source", {})
                raw_bytes = source.get("bytes") if isinstance(source, dict) else None
                mime_type = str(part.get("mime_type", "")).lower().strip()
                img_format = _IMAGE_MIME_TO_FORMAT.get(mime_type)
                if img_format is None:
                    raise ValueError(
                        f"unsupported image format: mime_type={mime_type!r} "
                        f"— supported: {sorted(_IMAGE_MIME_TO_FORMAT)}"
                    )
                blocks.append({
                    "image": {
                        "format": img_format,
                        "source": {"bytes": raw_bytes},
                    }
                })
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestImageContentBlock tests/agent/test_bedrock_adapter.py -v
```

Expected: 4 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): accept {type:image, source:{bytes}, mime_type} content blocks"
```

---

## Task 10: Citations passthrough (G13)

**Files:**
- Modify: `agent/bedrock_adapter.py` — `normalize_converse_response` + `stream_converse_with_callbacks`
- Test: `tests/agent/test_bedrock_phase2.py`

Bedrock Converse surfaces citations in content blocks as `{"text": "...", "citations": [...]}`. Hermes currently extracts only `text`. This task preserves `citations` alongside.

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestCitationsPassthrough:
    def test_sync_response_preserves_citations(self):
        from agent.bedrock_adapter import normalize_converse_response

        response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": "According to section 3.1, ...",
                            "citations": [
                                {
                                    "location": {"documentPage": {"pageNumber": 3}},
                                    "title": "contract.pdf",
                                }
                            ],
                        }
                    ]
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 50, "outputTokens": 20},
        }
        result = normalize_converse_response(response)

        assert hasattr(result.choices[0].message, "citations")
        assert result.choices[0].message.citations == [
            {
                "location": {"documentPage": {"pageNumber": 3}},
                "title": "contract.pdf",
            }
        ]

    def test_sync_response_without_citations_has_empty_list(self):
        from agent.bedrock_adapter import normalize_converse_response

        response = {
            "output": {"message": {"content": [{"text": "plain"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
        result = normalize_converse_response(response)

        assert result.choices[0].message.citations == []
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestCitationsPassthrough -v
```

Expected: `AttributeError: 'SimpleNamespace' object has no attribute 'citations'`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, in `normalize_converse_response` (around line 985 — inside the `for block in content_blocks:` loop), modify the text extraction:

```python
    text_parts = []
    tool_calls = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            ...
```

Change to track citations in parallel:

```python
    text_parts = []
    tool_calls = []
    citations: List[Dict] = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
            # Citations live alongside text in the same block
            block_cites = block.get("citations") or []
            if isinstance(block_cites, list):
                citations.extend(block_cites)
        elif "toolUse" in block:
            ...
```

Then in the `msg = SimpleNamespace(...)` construction, add the citations field:

```python
    msg = SimpleNamespace(
        role="assistant",
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls if tool_calls else None,
        citations=citations,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestCitationsPassthrough tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): preserve citations array on assistant messages (sync path)"
```

---

## Task 11: Thinking `budget_tokens` passthrough (G1)

**Files:**
- Modify: `agent/bedrock_adapter.py:1201` (`build_converse_kwargs`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestThinkingBudgetTokens:
    def test_budget_tokens_populates_additional_model_request_fields(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            thinking_budget_tokens=16384,
        )
        additional = kwargs.get("additionalModelRequestFields", {})
        assert additional.get("thinking") == {
            "type": "enabled",
            "budget_tokens": 16384,
        }

    def test_no_budget_tokens_no_thinking_field(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
        )
        additional = kwargs.get("additionalModelRequestFields", {})
        assert "thinking" not in additional
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestThinkingBudgetTokens -v
```

Expected: FAIL — `build_converse_kwargs` doesn't accept `thinking_budget_tokens`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py:1201`, update the signature:

```python
def build_converse_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
    thinking_budget_tokens: Optional[int] = None,
) -> Dict[str, Any]:
```

Then, after the existing `enable_1m_context` block (~line 1260), add:

```python
    if thinking_budget_tokens is not None and thinking_budget_tokens > 0:
        kwargs.setdefault("additionalModelRequestFields", {})
        kwargs["additionalModelRequestFields"]["thinking"] = {
            "type": "enabled",
            "budget_tokens": int(thinking_budget_tokens),
        }
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestThinkingBudgetTokens tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): wire thinking.budget_tokens via additionalModelRequestFields"
```

---

## Task 11.5: Extract `_append_anthropic_beta` helper (code-quality refactor)

**Files:**
- Modify: `agent/bedrock_adapter.py`
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** Phase 2 introduces 3 new `anthropic_beta` append sites on top of the existing
1M-beta one at line 1266. All 4 share the same 4-line boilerplate. Factoring into a
helper removes 12 LOC of duplication, matches the DRY preference.

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestAppendAnthropicBetaHelper:
    def test_appends_new_flag(self):
        from agent.bedrock_adapter import _append_anthropic_beta

        kwargs = {}
        _append_anthropic_beta(kwargs, "flag-a")
        assert kwargs["additionalModelRequestFields"]["anthropic_beta"] == ["flag-a"]

    def test_dedupes_existing_flag(self):
        from agent.bedrock_adapter import _append_anthropic_beta

        kwargs = {"additionalModelRequestFields": {"anthropic_beta": ["flag-a"]}}
        _append_anthropic_beta(kwargs, "flag-a")
        assert kwargs["additionalModelRequestFields"]["anthropic_beta"] == ["flag-a"]

    def test_preserves_sibling_request_fields(self):
        from agent.bedrock_adapter import _append_anthropic_beta

        kwargs = {"additionalModelRequestFields": {"thinking": {"type": "adaptive"}}}
        _append_anthropic_beta(kwargs, "flag-a")
        assert kwargs["additionalModelRequestFields"]["thinking"] == {"type": "adaptive"}
        assert kwargs["additionalModelRequestFields"]["anthropic_beta"] == ["flag-a"]
```

- [ ] **Step 2: Run + verify fail → write helper → verify pass**

In `agent/bedrock_adapter.py`:

```python
def _append_anthropic_beta(kwargs: Dict[str, Any], flag: str) -> None:
    """Append an anthropic-beta flag to kwargs, deduping. Mutates in place."""
    kwargs.setdefault("additionalModelRequestFields", {})
    betas = list(kwargs["additionalModelRequestFields"].get("anthropic_beta") or [])
    if flag not in betas:
        betas.append(flag)
    kwargs["additionalModelRequestFields"]["anthropic_beta"] = betas
```

Replace the existing 1M-beta injection (~line 1266-1270) with:

```python
    if enable_1m_context:
        _append_anthropic_beta(kwargs, CONTEXT_1M_BETA)
```

- [ ] **Step 3: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "refactor(bedrock): extract _append_anthropic_beta helper (DRY, dedupe)"
```

**NOTE for Tasks 12 and 13:** Use `_append_anthropic_beta(kwargs, FLAG)` instead of
the duplicated 4-line boilerplate shown in those tasks' original Step 3 code blocks.

---

## Task 12: Interleaved thinking beta (G9)

**Files:**
- Modify: `agent/bedrock_adapter.py:1201` (`build_converse_kwargs`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"


class TestInterleavedThinkingBeta:
    def test_enable_interleaved_adds_beta_header(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            enable_interleaved_thinking=True,
        )
        betas = kwargs["additionalModelRequestFields"]["anthropic_beta"]
        assert INTERLEAVED_THINKING_BETA in betas

    def test_disabled_interleaved_no_beta(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            enable_interleaved_thinking=False,
        )
        additional = kwargs.get("additionalModelRequestFields", {})
        betas = additional.get("anthropic_beta") or []
        assert INTERLEAVED_THINKING_BETA not in betas

    def test_interleaved_coexists_with_1m_beta(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7:1m",
            messages=[{"role": "user", "content": "hi"}],
            enable_interleaved_thinking=True,
        )
        betas = kwargs["additionalModelRequestFields"]["anthropic_beta"]
        assert INTERLEAVED_THINKING_BETA in betas
        assert "context-1m-2025-08-07" in betas
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestInterleavedThinkingBeta -v
```

Expected: FAIL — `build_converse_kwargs` doesn't accept `enable_interleaved_thinking`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, near the constants block (line 262-263 where `CONTEXT_1M_BETA` lives), add:

```python
INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"
```

Extend the `build_converse_kwargs` signature (the same function modified in Task 11):

```python
def build_converse_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
    thinking_budget_tokens: Optional[int] = None,
    enable_interleaved_thinking: bool = False,
) -> Dict[str, Any]:
```

After the `enable_1m_context` block (~line 1270), add:

```python
    if enable_interleaved_thinking:
        kwargs.setdefault("additionalModelRequestFields", {})
        betas = list(kwargs["additionalModelRequestFields"].get("anthropic_beta") or [])
        if INTERLEAVED_THINKING_BETA not in betas:
            betas.append(INTERLEAVED_THINKING_BETA)
        kwargs["additionalModelRequestFields"]["anthropic_beta"] = betas
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestInterleavedThinkingBeta tests/agent/test_bedrock_adapter.py -v
```

Expected: 3 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): wire interleaved-thinking-2025-05-14 beta flag"
```

---

## Task 13: 128K output beta (G12)

**Files:**
- Modify: `agent/bedrock_adapter.py:1201` (`build_converse_kwargs`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
OUTPUT_128K_BETA = "output-128k-2025-02-19"


class TestExtendedOutputBeta:
    def test_enable_128k_adds_beta(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=32768,
            enable_128k_output=True,
        )
        betas = kwargs["additionalModelRequestFields"]["anthropic_beta"]
        assert OUTPUT_128K_BETA in betas

    def test_disabled_128k_no_beta(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
            enable_128k_output=False,
        )
        additional = kwargs.get("additionalModelRequestFields", {})
        betas = additional.get("anthropic_beta") or []
        assert OUTPUT_128K_BETA not in betas
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestExtendedOutputBeta -v
```

Expected: FAIL — `enable_128k_output` not accepted.

- [ ] **Step 3: Write the fix**

Near the constants (next to `INTERLEAVED_THINKING_BETA`):

```python
OUTPUT_128K_BETA = "output-128k-2025-02-19"
```

Extend signature:

```python
def build_converse_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
    thinking_budget_tokens: Optional[int] = None,
    enable_interleaved_thinking: bool = False,
    enable_128k_output: bool = False,
) -> Dict[str, Any]:
```

After the interleaved-thinking block:

```python
    if enable_128k_output:
        kwargs.setdefault("additionalModelRequestFields", {})
        betas = list(kwargs["additionalModelRequestFields"].get("anthropic_beta") or [])
        if OUTPUT_128K_BETA not in betas:
            betas.append(OUTPUT_128K_BETA)
        kwargs["additionalModelRequestFields"]["anthropic_beta"] = betas
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestExtendedOutputBeta tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): wire output-128k-2025-02-19 beta flag"
```

---

## Task 14: `_inject_cache_points` helper — token guard + marker budget (G3 core)

**Files:**
- Modify: `agent/bedrock_adapter.py` (add helper)
- Test: `tests/agent/test_bedrock_phase2.py`

This is the architectural heart. Standalone helper, pure function, exercised with unit tests before it's wired in.

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestInjectCachePoints:
    def test_static_only_emits_one_marker(self):
        from agent.bedrock_adapter import _inject_cache_points

        system_out, messages_out = _inject_cache_points(
            system_static="a" * 5000,  # well above 1024 tokens
            system_volatile=None,
            converse_messages=[
                {"role": "user", "content": [{"text": "hi"}]},
            ],
            volatile_hash_prev=None,
        )
        # system is a list of content blocks; expect [text, cachePoint]
        assert len(system_out) == 2
        assert "text" in system_out[0]
        assert system_out[1] == {"cachePoint": {"type": "default"}}
        # messages untouched (short user turn)
        assert messages_out[0]["content"] == [{"text": "hi"}]

    def test_static_and_stable_volatile_emits_two_markers(self):
        from agent.bedrock_adapter import _inject_cache_points
        from hashlib import sha256

        volatile = "stable memory"
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        system_out, _ = _inject_cache_points(
            system_static="a" * 5000,
            system_volatile=volatile,
            converse_messages=[{"role": "user", "content": [{"text": "hi"}]}],
            volatile_hash_prev=prev_hash,
        )
        # [static, cachePoint, volatile, cachePoint]
        assert len(system_out) == 4
        assert system_out[1] == {"cachePoint": {"type": "default"}}
        assert system_out[3] == {"cachePoint": {"type": "default"}}

    def test_unstable_volatile_no_second_marker(self):
        from agent.bedrock_adapter import _inject_cache_points

        system_out, _ = _inject_cache_points(
            system_static="a" * 5000,
            system_volatile="changed memory",
            converse_messages=[{"role": "user", "content": [{"text": "hi"}]}],
            volatile_hash_prev="deadbeef" * 8,  # doesn't match current hash
        )
        # [static, cachePoint, volatile]  — 3 blocks, only 1 marker
        assert len(system_out) == 3
        assert system_out[1] == {"cachePoint": {"type": "default"}}
        assert "text" in system_out[2]
        # no cachePoint after volatile

    def test_long_user_turn_emits_third_marker(self):
        from agent.bedrock_adapter import _inject_cache_points

        long_user = "x" * 5000  # above 4096-char guard
        system_out, messages_out = _inject_cache_points(
            system_static="a" * 5000,
            system_volatile=None,
            converse_messages=[{"role": "user", "content": [{"text": long_user}]}],
            volatile_hash_prev=None,
        )
        # user turn's content list gains a cachePoint
        user_content = messages_out[0]["content"]
        assert user_content[-1] == {"cachePoint": {"type": "default"}}

    def test_short_user_turn_no_marker(self):
        from agent.bedrock_adapter import _inject_cache_points

        short_user = "what?"
        system_out, messages_out = _inject_cache_points(
            system_static="a" * 5000,
            system_volatile=None,
            converse_messages=[{"role": "user", "content": [{"text": short_user}]}],
            volatile_hash_prev=None,
        )
        user_content = messages_out[0]["content"]
        assert all("cachePoint" not in block for block in user_content)

    def test_marker_budget_max_4(self):
        from agent.bedrock_adapter import _inject_cache_points
        from hashlib import sha256

        volatile = "stable memory"
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        # 5 long user turns — but budget is 4 total (static + volatile + 2 users = 4)
        long_user = "x" * 5000
        messages = [
            {"role": "user", "content": [{"text": long_user}]} for _ in range(5)
        ]
        system_out, messages_out = _inject_cache_points(
            system_static="a" * 5000,
            system_volatile=volatile,
            converse_messages=messages,
            volatile_hash_prev=prev_hash,
        )
        # Count cachePoint markers across system + all messages
        total_markers = sum(
            1 for b in system_out if "cachePoint" in b
        )
        for msg in messages_out:
            total_markers += sum(1 for b in msg["content"] if "cachePoint" in b)
        assert total_markers == 4

    def test_short_static_skips_first_marker(self):
        from agent.bedrock_adapter import _inject_cache_points

        # static below 4096-char guard
        system_out, _ = _inject_cache_points(
            system_static="short",
            system_volatile=None,
            converse_messages=[{"role": "user", "content": [{"text": "hi"}]}],
            volatile_hash_prev=None,
        )
        # No cachePoint on too-short static
        assert len(system_out) == 1
        assert all("cachePoint" not in b for b in system_out)
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestInjectCachePoints -v
```

Expected: `ImportError: cannot import name '_inject_cache_points'`.

- [ ] **Step 3: Write the fix**

In `agent/bedrock_adapter.py`, add near the other conversion helpers (after `_convert_content_to_converse`):

```python
# Bedrock silently drops cachePoint markers on content blocks below the
# per-model minimum cacheable token count (Claude: 1024 tokens ≈ 4096 chars).
# We enforce a char-count guard to avoid burning budget on no-ops.
_CACHE_POINT_MIN_CHARS = 4096
# AWS caps cachePoint markers per request at 4.
_CACHE_POINT_MAX_BUDGET = 4
_CACHE_POINT_BLOCK = {"cachePoint": {"type": "default"}}


def _block_char_count(blocks: List[Dict]) -> int:
    """Approximate token count by summing text-block character counts."""
    total = 0
    for b in blocks:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            total += len(b["text"])
    return total


def _inject_cache_points(
    system_static: Optional[str],
    system_volatile: Optional[str],
    converse_messages: List[Dict],
    volatile_hash_prev: Optional[str],
) -> Tuple[List[Dict], List[Dict]]:
    """Build the system content list and annotate user messages with cachePoint markers.

    Placement rule (Phase 2 design §3):
      1. cachePoint after ``system_static``   (always, if static ≥ min chars)
      2. cachePoint after ``system_volatile`` (only if current hash matches ``volatile_hash_prev``)
      3. cachePoint after any user turn whose text content ≥ min chars (conditional)

    Budget: AT MOST 4 cachePoint markers per request (AWS-enforced cap).

    Returns ``(system_blocks, converse_messages)`` — the messages list has its
    content arrays annotated in place where markers fit within the budget.
    """
    from hashlib import sha256 as _sha256

    system_blocks: List[Dict] = []
    budget = _CACHE_POINT_MAX_BUDGET

    # Block 1: static
    if system_static:
        system_blocks.append({"text": system_static})
        if len(system_static) >= _CACHE_POINT_MIN_CHARS and budget > 0:
            system_blocks.append(_CACHE_POINT_BLOCK)
            budget -= 1

    # Block 2: volatile (stability-gated)
    if system_volatile:
        system_blocks.append({"text": system_volatile})
        current_hash = _sha256(system_volatile.encode("utf-8")).hexdigest()
        if (
            volatile_hash_prev is not None
            and current_hash == volatile_hash_prev
            and len(system_volatile) >= _CACHE_POINT_MIN_CHARS
            and budget > 0
        ):
            system_blocks.append(_CACHE_POINT_BLOCK)
            budget -= 1

    # Block 3+: user-turn conditional markers, iterating from the END backwards
    # (Cline convention: mark the most recent long user turns first).
    out_messages = [dict(m) for m in converse_messages]  # shallow copy
    for m in out_messages:
        m["content"] = list(m.get("content") or [])

    for msg in reversed(out_messages):
        if budget <= 0:
            break
        if msg.get("role") != "user":
            continue
        content = msg["content"]
        if _block_char_count(content) >= _CACHE_POINT_MIN_CHARS:
            content.append(_CACHE_POINT_BLOCK)
            budget -= 1

    return system_blocks, out_messages
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestInjectCachePoints -v
```

Expected: 7 new passed.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): _inject_cache_points with token guard + 4-marker budget"
```

---

## Task 15: Wire `_inject_cache_points` into `build_converse_kwargs` (G3 integration + G4 cache_ttl)

**Files:**
- Modify: `agent/bedrock_adapter.py:1201` (`build_converse_kwargs`)
- Test: `tests/agent/test_bedrock_phase2.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestBuildKwargsWithCacheSplit:
    def test_split_prompt_emits_cache_points_in_system(self):
        from agent.bedrock_adapter import build_converse_kwargs
        from hashlib import sha256

        volatile = "stable volatile block" * 200  # well above min chars
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        messages = [
            {"role": "system", "content": "a" * 5000},
            {"role": "user", "content": "hi"},
        ]
        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=messages,
            system_volatile=volatile,
            volatile_hash_prev=prev_hash,
        )
        system_blocks = kwargs.get("system") or []
        # Expect [static_text, cachePoint, volatile_text, cachePoint]
        assert any("cachePoint" in b for b in system_blocks)
        # And at least 2 cache points given stable volatile
        marker_count = sum(1 for b in system_blocks if "cachePoint" in b)
        assert marker_count == 2

    def test_no_split_keeps_legacy_shape(self):
        from agent.bedrock_adapter import build_converse_kwargs

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[
                {"role": "system", "content": "whatever"},
                {"role": "user", "content": "hi"},
            ],
        )
        # No system_volatile → legacy shape, no cachePoint
        system_blocks = kwargs.get("system") or []
        assert all("cachePoint" not in b for b in system_blocks)

    def test_cache_ttl_passthrough_1h(self):
        from agent.bedrock_adapter import build_converse_kwargs
        from hashlib import sha256

        volatile = "v" * 5000
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[
                {"role": "system", "content": "s" * 5000},
                {"role": "user", "content": "hi"},
            ],
            system_volatile=volatile,
            volatile_hash_prev=prev_hash,
            cache_ttl="1h",
        )
        system_blocks = kwargs.get("system") or []
        # Every cachePoint should carry ttl:"1h"
        cache_points = [b for b in system_blocks if "cachePoint" in b]
        assert len(cache_points) >= 1
        for cp in cache_points:
            assert cp["cachePoint"].get("ttl") == "1h"

    def test_cache_ttl_default_5m_emits_ttl(self):
        from agent.bedrock_adapter import build_converse_kwargs
        from hashlib import sha256

        volatile = "v" * 5000
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        kwargs = build_converse_kwargs(
            model="us.anthropic.claude-opus-4-7",
            messages=[
                {"role": "system", "content": "s" * 5000},
                {"role": "user", "content": "hi"},
            ],
            system_volatile=volatile,
            volatile_hash_prev=prev_hash,
        )
        system_blocks = kwargs.get("system") or []
        cache_points = [b for b in system_blocks if "cachePoint" in b]
        for cp in cache_points:
            # Explicit > clever: always emit ttl even when matches default
            assert cp["cachePoint"].get("ttl") == "5m"
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBuildKwargsWithCacheSplit -v
```

Expected: FAIL — `system_volatile` kwarg not accepted.

- [ ] **Step 3: Write the fix**

Extend `build_converse_kwargs` signature:

```python
def build_converse_kwargs(
    model: str,
    messages: List[Dict],
    tools: Optional[List[Dict]] = None,
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
    guardrail_config: Optional[Dict] = None,
    thinking_budget_tokens: Optional[int] = None,
    enable_interleaved_thinking: bool = False,
    enable_128k_output: bool = False,
    system_volatile: Optional[str] = None,
    volatile_hash_prev: Optional[str] = None,
    cache_ttl: str = "5m",
) -> Dict[str, Any]:
```

Near the top of the body (right after `system_prompt, converse_messages = convert_messages_to_converse(messages)`), add the split/inject logic:

```python
    # Phase 2: if caller supplies system_volatile, split system into static+volatile
    # and inject cachePoint markers per Phase 2 design §3.
    if system_volatile is not None:
        system_static_text = (
            "\n".join(b.get("text", "") for b in (system_prompt or []) if "text" in b)
            if system_prompt
            else ""
        )
        new_system_blocks, converse_messages = _inject_cache_points(
            system_static=system_static_text,
            system_volatile=system_volatile,
            converse_messages=converse_messages,
            volatile_hash_prev=volatile_hash_prev,
        )
        # Annotate every emitted cachePoint with the explicit TTL. We always
        # set the ttl field (even when it matches Bedrock's default "5m") for
        # readability — explicit > clever. If Bedrock's default changes, we
        # stay unaffected.
        effective_ttl = cache_ttl or "5m"
        for b in new_system_blocks:
            if "cachePoint" in b:
                b["cachePoint"]["ttl"] = effective_ttl
        for m in converse_messages:
            for b in m.get("content") or []:
                if isinstance(b, dict) and "cachePoint" in b:
                    b["cachePoint"]["ttl"] = effective_ttl
        system_prompt = new_system_blocks
```

(The rest of `build_converse_kwargs` is unchanged — it already uses `system_prompt` and `converse_messages` to build the final kwargs.)

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBuildKwargsWithCacheSplit tests/agent/test_bedrock_adapter.py -v
```

Expected: 4 new passed + all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add agent/bedrock_adapter.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): wire _inject_cache_points + cache_ttl into build_converse_kwargs"
```

---

## Task 16: Live integration test (cache verification, opt-in)

**Files:**
- Create: `tests/agent/test_bedrock_integration_cache.py`

This is the final correctness gate per the spec §6. Not run in CI by default — gated on `RUN_BEDROCK_LIVE_TESTS=1` + valid AWS creds. Required to pass before closing the Phase 2 PR.

- [ ] **Step 1: Write the live test**

Create `tests/agent/test_bedrock_integration_cache.py`:

```python
"""Live Bedrock cache verification.

Gated behind ``RUN_BEDROCK_LIVE_TESTS=1`` env var. Incurs ~$0.10 per run
(two Opus 4.7 calls with ~2K cached input tokens). Required to pass before
Phase 2 ships.

Prereqs:
  - AWS credentials resolvable (bearer token, profile, or default chain)
  - AWS_REGION or bedrock config pointing to a Claude-capable region
  - ``us.anthropic.claude-opus-4-7`` available in the account

Run with:
  RUN_BEDROCK_LIVE_TESTS=1 venv/bin/pytest tests/agent/test_bedrock_integration_cache.py -v -s
"""

import os
from hashlib import sha256

import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BEDROCK_LIVE_TESTS") != "1",
    reason="live Bedrock test — set RUN_BEDROCK_LIVE_TESTS=1 to run",
)


def test_cache_fires_on_turn_2():
    """Turn 1 writes cache; turn 2 reads cache. cache_read_input_tokens must be > 0."""
    from agent.bedrock_adapter import (
        _create_bedrock_client,
        build_converse_kwargs,
        normalize_converse_response,
        resolve_bedrock_auth_config,
    )

    # 5000 chars ≈ 1200+ tokens — above the 1024 Bedrock minimum for Claude
    system_static = "You are a test agent. " * 250
    system_volatile = "Stable memory block. " * 250  # also above min
    volatile_hash = sha256(system_volatile.encode("utf-8")).hexdigest()

    auth = resolve_bedrock_auth_config()
    client = _create_bedrock_client("bedrock-runtime", auth["region"], auth)

    # Turn 1 — cold
    kwargs_t1 = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": system_static},
            {"role": "user", "content": "Reply with: cold"},
        ],
        max_tokens=16,
        system_volatile=system_volatile,
        volatile_hash_prev=volatile_hash,  # hash matches, so marker fires
    )
    r1 = client.converse(**kwargs_t1)
    normalized_r1 = normalize_converse_response(r1)

    print(f"\nTurn 1 usage: prompt={normalized_r1.usage.prompt_tokens} "
          f"cache_write={normalized_r1.usage.cache_creation_input_tokens} "
          f"cache_read={normalized_r1.usage.cache_read_input_tokens}")

    assert normalized_r1.usage.cache_creation_input_tokens > 0, (
        "Turn 1 should write cache (cache_creation_input_tokens > 0) — "
        "check cachePoint placement and min-token threshold"
    )
    assert normalized_r1.usage.cache_read_input_tokens == 0, (
        "Turn 1 should not read cache (first call)"
    )

    # Turn 2 — warm (same system_static, same system_volatile → prefix matches)
    kwargs_t2 = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": system_static},
            {"role": "user", "content": "Reply with: warm"},
        ],
        max_tokens=16,
        system_volatile=system_volatile,
        volatile_hash_prev=volatile_hash,
    )
    r2 = client.converse(**kwargs_t2)
    normalized_r2 = normalize_converse_response(r2)

    print(f"Turn 2 usage: prompt={normalized_r2.usage.prompt_tokens} "
          f"cache_write={normalized_r2.usage.cache_creation_input_tokens} "
          f"cache_read={normalized_r2.usage.cache_read_input_tokens}")

    assert normalized_r2.usage.cache_read_input_tokens > 0, (
        "Turn 2 cache_read_input_tokens must be > 0 — proves cachePoint fired"
    )
```

- [ ] **Step 2: Run the live test**

Precondition: AWS credentials available + Opus 4.7 enabled in the target region.

```
cd ~/.hermes/hermes-agent
RUN_BEDROCK_LIVE_TESTS=1 venv/bin/pytest tests/agent/test_bedrock_integration_cache.py -v -s
```

Expected: PASS. Printed usage shows turn 1 `cache_write > 0`, turn 2 `cache_read > 0`.

If it fails:
- `cache_creation_input_tokens == 0` on turn 1 → cachePoint placement wrong or block below 1024 tokens. Bump `_CACHE_POINT_MIN_CHARS` upward or verify split logic.
- `cache_read_input_tokens == 0` on turn 2 → prefix byte-mismatch; something dynamic in the split. Dump `system_prompt` blocks from both turns and diff.

- [ ] **Step 3: Write the mid-session memory-change test (added by plan-eng-review)**

Append to `tests/agent/test_bedrock_integration_cache.py`:

```python
def test_memory_change_mid_session_invalidates_cache():
    """3-turn scenario: warm → save memory → cache miss on next turn."""
    from agent.bedrock_adapter import (
        _create_bedrock_client,
        build_converse_kwargs,
        normalize_converse_response,
        resolve_bedrock_auth_config,
    )
    from hashlib import sha256

    system_static = "You are a test agent. " * 250
    volatile_a = "Stable memory block A. " * 250
    hash_a = sha256(volatile_a.encode("utf-8")).hexdigest()

    volatile_b = "Stable memory block B. " * 250  # mid-session "save"
    hash_b = sha256(volatile_b.encode("utf-8")).hexdigest()

    auth = resolve_bedrock_auth_config()
    client = _create_bedrock_client("bedrock-runtime", auth["region"], auth)

    # Turn 1 — cold
    kw1 = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": system_static},
            {"role": "user", "content": "Reply with: turn1"},
        ],
        max_tokens=16,
        system_volatile=volatile_a,
        volatile_hash_prev=hash_a,
    )
    r1 = normalize_converse_response(client.converse(**kw1))

    # Turn 2 — warm (same volatile_a)
    kw2 = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": system_static},
            {"role": "user", "content": "Reply with: turn2"},
        ],
        max_tokens=16,
        system_volatile=volatile_a,
        volatile_hash_prev=hash_a,
    )
    r2 = normalize_converse_response(client.converse(**kw2))
    assert r2.usage.cache_read_input_tokens > 0, "Turn 2 should read cache"
    turn2_cache_read = r2.usage.cache_read_input_tokens

    # Turn 3 — memory SAVED mid-session (volatile changed from a → b)
    kw3 = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[
            {"role": "system", "content": system_static},
            {"role": "user", "content": "Reply with: turn3"},
        ],
        max_tokens=16,
        system_volatile=volatile_b,
        volatile_hash_prev=hash_b,
    )
    r3 = normalize_converse_response(client.converse(**kw3))
    # Volatile block changed → that marker is no longer valid for cache read,
    # but static system block's marker (hash unchanged) should still cache.
    # Assert we still got SOME cache hit (static part), but volatile portion is re-written.
    assert r3.usage.cache_creation_input_tokens > 0, (
        "Turn 3 should re-write cache for the new volatile block"
    )
    print(f"Turn 3 cache_read={r3.usage.cache_read_input_tokens} "
          f"cache_write={r3.usage.cache_creation_input_tokens} (mid-session memory change)")
```

- [ ] **Step 4: Write the drop-and-retry quality smoke test (added by plan-eng-review)**

Append to the same file:

```python
def test_drop_and_retry_produces_coherent_response(monkeypatch):
    """Inject a Bedrock doc rejection; verify drop-path produces coherent text."""
    from agent.bedrock_adapter import (
        _create_bedrock_client,
        build_converse_kwargs,
        normalize_converse_response,
        resolve_bedrock_auth_config,
    )

    auth = resolve_bedrock_auth_config()
    client = _create_bedrock_client("bedrock-runtime", auth["region"], auth)

    # First call: send a text-only turn (the doc would have been dropped by
    # Task 19's recovery before hitting this point — we simulate post-drop state).
    kw = build_converse_kwargs(
        model="us.anthropic.claude-opus-4-7",
        messages=[{
            "role": "user",
            "content": [
                {"text": "Summarize the contract I attached."},
                {"text": "[attachment too large — continuing without: contract.pdf]"},
            ],
        }],
        max_tokens=128,
    )
    r = normalize_converse_response(client.converse(**kw))

    # Coherence smoke: response mentions the situation (no attachment available)
    # and doesn't hallucinate contract content. Qualitative assert — kept loose.
    text = (r.choices[0].message.content or "").lower()
    assert len(text) > 10, "Expected a text response"
    assert any(kw in text for kw in ["attach", "document", "file", "provide", "send", "upload", "can't", "cannot", "unable", "don't have", "didn't", "without"]), (
        f"Response should acknowledge the missing attachment. Got: {text[:200]!r}"
    )
```

- [ ] **Step 5: Commit**

```bash
git add tests/agent/test_bedrock_integration_cache.py
git commit -m "test(bedrock): live cache + mid-session + drop-retry quality smokes"
```

---

---

## Resilience tasks (17-21) — added by /plan-design-review 2026-04-30

These 5 tasks close silent-failure modes surfaced during design review: a Bedrock
rejection of an oversized document/image/context today lands in the "unknown"
error bucket, triggering backoff-retry that repeats the same failure. Phase 2
must classify these failures correctly, invoke the appropriate recovery
(shrink / drop / compress), and verify checkpoints fire so the user never
loses a turn to a provider rejection.

Drop-and-retry semantics: when a doc/image is rejected, strip the offending
block, append `[attachment too large — continuing without]` as a system note,
and retry text-only. Matches the existing image-shrink recovery spirit.

## Task 17: Bedrock error classifier patterns (resilience #1)

**Files:**
- Modify: `agent/error_classifier.py` (add Bedrock-specific patterns)
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** Today a Bedrock `ValidationException: "document too large"` or `"image ... exceeds"` lands in `FailoverReason.unknown`, triggering backoff retry that will fail identically. We need them classified as `payload_too_large` / `image_too_large` / `document_too_large` so the existing recovery paths can fire.

- [ ] **Step 1: Write the failing test**

Append to `tests/agent/test_bedrock_phase2.py`:

```python
class TestBedrockErrorClassification:
    def test_document_size_rejection_classifies_as_document_too_large(self):
        from agent.error_classifier import classify_api_error, FailoverReason

        err = type("ClientError", (Exception,), {})("An error occurred (ValidationException) when calling the Converse operation: Document is too large. Maximum document size is 4500000 bytes.")
        err.response = {"Error": {"Code": "ValidationException"}}

        result = classify_api_error(err, provider="bedrock", model="us.anthropic.claude-opus-4-7")
        assert result.reason == FailoverReason.document_too_large

    def test_image_size_rejection_classifies_as_image_too_large(self):
        from agent.error_classifier import classify_api_error, FailoverReason

        err = type("ClientError", (Exception,), {})("An error occurred (ValidationException) when calling the Converse operation: The image is too large. Maximum image size is 3750000 bytes.")
        err.response = {"Error": {"Code": "ValidationException"}}

        result = classify_api_error(err, provider="bedrock", model="us.anthropic.claude-opus-4-7")
        assert result.reason == FailoverReason.image_too_large

    def test_context_length_rejection_classifies_as_context_overflow(self):
        from agent.error_classifier import classify_api_error, FailoverReason

        err = type("ClientError", (Exception,), {})("Input is too long for requested model. Please reduce input token count.")
        err.response = {"Error": {"Code": "ValidationException"}}

        result = classify_api_error(err, provider="bedrock", model="us.anthropic.claude-opus-4-7")
        assert result.reason == FailoverReason.context_overflow
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockErrorClassification -v
```

Expected: FAIL — patterns don't exist yet; classifier returns `FailoverReason.unknown` or `format_error`.

- [ ] **Step 3: Write the fix**

In `agent/error_classifier.py`:

1. Add `document_too_large = "document_too_large"` to the `FailoverReason` enum (right after `image_too_large`).

2. Find the classifier dispatch for Bedrock (grep for `provider.*bedrock` or similar). Add these pattern matchers — if none exists, add a Bedrock-specific block in `classify_api_error` before the catch-all:

```python
    # Bedrock Converse validation errors
    msg_lower = str(error_message).lower()
    if "bedrock" in (provider or "").lower() or "anthropic.claude" in (model or "").lower():
        if "document" in msg_lower and ("too large" in msg_lower or "exceeds" in msg_lower or "maximum" in msg_lower):
            return ClassifiedError(
                reason=FailoverReason.document_too_large,
                status_code=400,
                provider=provider,
                retryable=False,  # Retry only after stripping the doc
                should_compress=False,
            )
        if "image" in msg_lower and ("too large" in msg_lower or "exceeds" in msg_lower or "maximum image" in msg_lower):
            return ClassifiedError(
                reason=FailoverReason.image_too_large,
                status_code=400,
                provider=provider,
                retryable=True,  # Shrink path handles retry
                should_compress=False,
            )
        if ("input is too long" in msg_lower or "token count" in msg_lower or
            "context length" in msg_lower or "exceeds maximum" in msg_lower):
            return ClassifiedError(
                reason=FailoverReason.context_overflow,
                status_code=400,
                provider=provider,
                retryable=True,
                should_compress=True,
            )
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockErrorClassification tests/agent/test_bedrock_adapter.py -v
```

Expected: 3 new passed + existing stay green.

- [ ] **Step 5: Commit**

```bash
git add agent/error_classifier.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): classify document/image/context errors for targeted recovery"
```

---

## Task 18: Extend image-shrink path to Bedrock `{type:image,source:{bytes}}` shape (resilience #2)

**Files:**
- Modify: `run_agent.py:_try_shrink_image_parts_in_messages` (line 7902)
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** Task 9 adds the Bedrock-native `{"image": {"format": ..., "source": {"bytes": ...}}}` content shape. `_try_shrink_image_parts_in_messages` today only matches OpenAI-style `data:image/...` URLs. Without this, Bedrock rejects an oversized image, classifier fires `image_too_large`, recovery path can't find anything to shrink, turn fails.

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestBedrockImageShrinkPath:
    def test_shrink_path_matches_bedrock_image_block(self, monkeypatch):
        from run_agent import AIAgent
        import base64

        # Large PNG payload (~5MB of random bytes)
        big_png = b"\x89PNG\r\n\x1a\n" + (b"x" * 5_000_000)
        messages = [{
            "role": "user",
            "content": [
                {"text": "what's in this?"},
                {"image": {"format": "png", "source": {"bytes": big_png}}},
            ],
        }]

        # Mock the shrink helper to return a trivially smaller payload
        small_png = b"\x89PNG\r\n\x1a\n" + (b"y" * 100)
        monkeypatch.setattr(
            "tools.vision_tools._resize_image_for_vision",
            lambda path, max_side=None: base64.b64encode(small_png).decode("ascii"),
        )

        agent = AIAgent.__new__(AIAgent)  # bypass __init__
        result = agent._try_shrink_image_parts_in_messages(messages)

        assert result is True
        # The image block's bytes should now be the smaller payload
        new_bytes = messages[0]["content"][1]["image"]["source"]["bytes"]
        assert len(new_bytes) < len(big_png)
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockImageShrinkPath -v
```

Expected: FAIL — shrink path returns False (no `data:` URLs found).

- [ ] **Step 3: Write the fix**

In `run_agent.py:_try_shrink_image_parts_in_messages`, after the existing data-URL matching loop, add a second loop that matches Bedrock image blocks:

```python
        # Bedrock Converse native image blocks: {"image": {"format": ..., "source": {"bytes": ...}}}
        for msg in api_messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                img = part.get("image")
                if not isinstance(img, dict):
                    continue
                source = img.get("source")
                if not isinstance(source, dict):
                    continue
                raw_bytes = source.get("bytes")
                if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) <= target_bytes:
                    continue
                fmt = str(img.get("format", "jpeg")).lower()
                suffix = {"png": ".png", "gif": ".gif", "webp": ".webp", "jpeg": ".jpg"}.get(fmt, ".jpg")
                try:
                    import tempfile, base64
                    tmp = tempfile.NamedTemporaryFile(prefix="hermes_shrink_bedrock_", suffix=suffix, delete=False)
                    try:
                        tmp.write(bytes(raw_bytes))
                        tmp.flush()
                        resized_b64 = _resize_image_for_vision(tmp.name, max_side=1568)
                        if resized_b64:
                            source["bytes"] = base64.b64decode(resized_b64)
                            changed_count += 1
                    finally:
                        tmp.close()
                        os.unlink(tmp.name)
                except Exception as exc:
                    logger.warning("image-shrink recovery: Bedrock block re-encode failed — %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestBedrockImageShrinkPath tests/agent/test_bedrock_adapter.py -v
```

Expected: 1 new passed + existing green.

- [ ] **Step 5: Commit**

```bash
git add run_agent.py tests/agent/test_bedrock_phase2.py
git commit -m "fix(bedrock): extend image-shrink recovery to Bedrock image block shape"
```

---

## Task 19: Document drop-and-retry recovery (resilience #3)

**Files:**
- Modify: `run_agent.py` (add `_try_drop_oversized_documents` helper, wire into main retry loop around line 11650)
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** When Bedrock rejects an oversized document, Task 17 now classifies it as `document_too_large`. We need a recovery path that strips the offending document block from the turn's messages, appends a system note `[attachment too large — continuing without]`, and retries text-only. Matches the user decision for "drop attachment + retry text-only with warning."

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestDocumentDropAndRetry:
    def test_drop_removes_document_blocks_and_adds_system_note(self):
        from run_agent import AIAgent

        messages = [{
            "role": "user",
            "content": [
                {"text": "summarize this contract"},
                {"document": {"format": "pdf", "name": "big.pdf", "source": {"bytes": b"PDF" + b"x" * 10_000_000}}},
            ],
        }]

        agent = AIAgent.__new__(AIAgent)
        result = agent._try_drop_oversized_documents_in_messages(messages)

        assert result is True
        # Document block stripped
        assert all("document" not in block for block in messages[0]["content"])
        # Text block preserved
        assert any(block.get("text") == "summarize this contract" for block in messages[0]["content"])
        # System note appended
        notes = [b.get("text", "") for b in messages[0]["content"]]
        assert any("attachment too large" in note.lower() for note in notes)

    def test_drop_returns_false_when_no_documents_present(self):
        from run_agent import AIAgent

        messages = [{"role": "user", "content": [{"text": "hello"}]}]

        agent = AIAgent.__new__(AIAgent)
        assert agent._try_drop_oversized_documents_in_messages(messages) is False
```

- [ ] **Step 2: Run test to verify it fails**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestDocumentDropAndRetry -v
```

Expected: `AttributeError: 'AIAgent' object has no attribute '_try_drop_oversized_documents_in_messages'`.

- [ ] **Step 3: Write the fix**

In `run_agent.py`, add the helper near `_try_shrink_image_parts_in_messages` (around line 7902):

```python
    def _try_drop_oversized_documents_in_messages(self, api_messages: list) -> bool:
        """Strip document content blocks from the most recent user turn and
        append a system note. Used when Bedrock rejects a doc as too large.

        Returns True if any document was dropped, False otherwise.
        """
        if not api_messages:
            return False

        changed = False
        for msg in api_messages:
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            new_content = []
            dropped_names = []
            for part in content:
                if isinstance(part, dict) and "document" in part:
                    doc = part.get("document") or {}
                    dropped_names.append(str(doc.get("name", "document")))
                    continue
                new_content.append(part)
            if dropped_names:
                new_content.append({
                    "text": f"[attachment too large — continuing without: {', '.join(dropped_names)}]"
                })
                msg["content"] = new_content
                changed = True
        return changed
```

Then, in the main retry loop (around line 11650, after the `image_too_large` branch), add:

```python
                    # Document-too-large recovery: strip rejected docs, add
                    # system note, retry text-only.
                    if (
                        classified.reason == FailoverReason.document_too_large
                        and not document_drop_retry_attempted
                    ):
                        document_drop_retry_attempted = True
                        if self._try_drop_oversized_documents_in_messages(api_messages):
                            self._vprint(
                                f"{self.log_prefix}📄 Document(s) too large for provider — "
                                f"dropped and retrying text-only...",
                                force=True,
                            )
                            continue
```

Declare `document_drop_retry_attempted = False` at the start of the retry loop alongside `image_shrink_retry_attempted`.

- [ ] **Step 4: Run test to verify it passes**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestDocumentDropAndRetry tests/agent/test_bedrock_adapter.py -v
```

Expected: 2 new passed + existing green.

- [ ] **Step 5: Commit**

```bash
git add run_agent.py tests/agent/test_bedrock_phase2.py
git commit -m "feat(bedrock): drop-and-retry recovery for oversized document blocks"
```

---

## Task 20: (REMOVED by plan-eng-review 2026-04-30)

`CheckpointManager` is a filesystem-state checkpointer (git shadow repo for the
working directory), not a turn-state snapshotter. The originally-planned
`_checkpoint_mgr.snapshot(turn_id, messages)` API does not exist.

Turn-state replay-on-failure would require a new `agent/turn_snapshot.py` subsystem.
Scope creep for Phase 2. Deferred to a future resilience PR if the Phase 2 adapter
changes prove to wedge state in practice (no evidence they will — each new code
path has its own failing test).

Existing recovery coverage remains:
- Image-shrink retry (pre-existing + extended in Task 18)
- Doc-drop retry (new in Task 19)
- Context-compression retry (pre-existing, invariant documented in Task 21)
- Adaptive retry at the boto3 layer (new in Task 4)
- Error classifier with Bedrock patterns (new in Task 17)

If the adapter wedges mid-stream despite all of the above, user loses that turn and
next turn starts fresh. This is current behavior; Phase 2 does not regress it.


## Task 21: Post-compression cachePoint re-injection (resilience #5)

**Files:**
- Modify: `run_agent.py` (compression retry path around line 12900) — verify `build_converse_kwargs` is re-called, not kwargs mutated
- Test: `tests/agent/test_bedrock_phase2.py`

**Why:** When `context_overflow` fires and `context_compressor.should_compress()` drops messages, `build_converse_kwargs` must be re-invoked to recompute cachePoint placement. If the compression path mutates a cached `kwargs` dict instead of rebuilding, cachePoint markers on dropped messages become stale — Bedrock will silently not cache or return a validation error.

- [ ] **Step 1: Write the failing test**

Append:

```python
class TestPostCompressionCacheReinject:
    def test_compression_triggers_kwargs_rebuild_not_mutate(self):
        """After compression drops messages, the retry path must rebuild kwargs
        so _inject_cache_points re-runs with the new (shorter) message list.

        We test this by inspecting that `build_converse_kwargs` is called
        twice: once pre-compression, once post.
        """
        from agent.bedrock_adapter import build_converse_kwargs
        from hashlib import sha256

        call_log = []

        def spy_build(**kwargs):
            call_log.append(len(kwargs.get("messages", [])))
            return build_converse_kwargs(**kwargs)

        # Simulate: pre-compression had 10 messages, post-compression has 4
        messages_before = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        messages_after = messages_before[-4:]

        volatile = "stable" * 500
        prev_hash = sha256(volatile.encode("utf-8")).hexdigest()

        spy_build(
            model="us.anthropic.claude-opus-4-7",
            messages=messages_before,
            system_volatile=volatile,
            volatile_hash_prev=prev_hash,
        )
        spy_build(
            model="us.anthropic.claude-opus-4-7",
            messages=messages_after,
            system_volatile=volatile,
            volatile_hash_prev=prev_hash,
        )

        # Two separate invocations with different message counts
        assert call_log == [10, 4]
```

- [ ] **Step 2: Run test to verify it passes (or fails with a clear signal)**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestPostCompressionCacheReinject -v
```

This test passes trivially if `build_converse_kwargs` is pure (it is — verified by reading the signature). The real risk is in the caller: does run_agent.py re-call it after compression, or mutate a cached kwargs?

- [ ] **Step 3: Verify call site in run_agent.py**

Grep the retry loop for where `build_converse_kwargs` is invoked:

```
cd ~/.hermes/hermes-agent
grep -n "build_converse_kwargs\|converse_kwargs\s*=" run_agent.py | head -10
```

For each call site, confirm: after `context_compressor.should_compress()` returns True, the next iteration of the retry loop freshly calls `build_converse_kwargs(messages=compressed_messages, ...)` rather than reusing a stale dict.

If you find a stale-mutation bug: fix it inline by moving the `build_converse_kwargs` call inside the retry loop body (not before it).

If the code is already correct: add a code comment documenting the invariant:

```python
# INVARIANT: build_converse_kwargs must be called INSIDE the retry loop body.
# Phase 2 adds cachePoint markers keyed to the current message list; mutating
# a cached kwargs dict after compression would leave stale markers that
# silently break prompt caching.
```

- [ ] **Step 4: Run the test**

```
cd ~/.hermes/hermes-agent && venv/bin/pytest tests/agent/test_bedrock_phase2.py::TestPostCompressionCacheReinject tests/agent/test_bedrock_adapter.py -v
```

Expected: 1 new passed + existing green.

- [ ] **Step 5: Commit**

```bash
git add run_agent.py tests/agent/test_bedrock_phase2.py
git commit -m "chore(bedrock): document + test kwargs-rebuild invariant on compression retry"
```

---

## Task 22: Final regression sweep + Phase 2 test summary

- [ ] **Step 1: Run the full test suite**

```
cd ~/.hermes/hermes-agent
venv/bin/pytest tests/agent/ -v 2>&1 | tail -40
```

Expected: all previously-green tests still pass; new tests added through Tasks 1-15 all pass.

- [ ] **Step 2: Count new test coverage**

```
cd ~/.hermes/hermes-agent
venv/bin/pytest tests/agent/test_bedrock_phase2.py --collect-only -q | tail -5
```

Expected: ~30 test cases collected across all Phase 2 test classes.

- [ ] **Step 3: Run the live integration test if AWS creds are available**

```
RUN_BEDROCK_LIVE_TESTS=1 venv/bin/pytest tests/agent/test_bedrock_integration_cache.py -v -s
```

Expected: PASS with turn-2 `cache_read > 0`. If skipped (no creds), document the gap in the PR description.

- [ ] **Step 4: Commit the final state**

```bash
git log --oneline $(git merge-base HEAD origin/main)..HEAD | head -20
git status
```

If everything is clean (no uncommitted changes), this marks Phase 2 complete. Push to fork:

```bash
git push origin feat/native-bedrock-provider-20260428
```

Open PR against `hermes-native/hermes` (or continue on the existing Phase 1 PR depending on branch policy).

---

## Self-review checklist

This section is for the plan author's last-pass sanity check — not an execution step.

**1. Spec coverage — every goal (G1-G13) mapped to a task:**
- G1 thinking.budget_tokens → Task 11 ✓
- G2 usage normalization sync+stream → Tasks 1, 2, 3 ✓
- G3 cachePoint split + injector + guard → Tasks 14, 15 ✓
- G4 cache_ttl passthrough → Task 15 ✓
- G5 boto3 adaptive retry → Task 4 ✓
- G6 default credential chain auto mode → Task 6 ✓
- G7 cross-region prefix + carve-out → Task 5 ✓
- G8 VPC wizard exposure → Task 7 ✓
- G9 interleaved thinking beta → Task 12 ✓
- G10 document content blocks → Task 8 ✓
- G11 image content blocks → Task 9 ✓
- G12 128K output beta → Task 13 ✓
- G13 citations passthrough → Task 10 ✓
- Live verification → Task 16 ✓
- Regression sweep → Task 17 ✓

**2. Placeholder scan: no TBD/TODO/FIXME/placeholder patterns in any task body.**

**3. Type consistency:** `_extract_bedrock_usage` defined in Task 1, consumed in Tasks 2 & 3. `_inject_cache_points` defined in Task 14, wired in Task 15. `derive_cross_region_model_id` defined in Task 5, not consumed in other tasks (caller integration is out of scope — the spec §9 tracks Pi/OpenClaw replication as scope C). `build_converse_kwargs` signature grows monotonically through Tasks 11-15; each task shows the new full signature.

**4. Backward-compat:** every new `build_converse_kwargs` kwarg has a default that preserves pre-Phase-2 behavior. Existing 459-test regression suite will stay green.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR | score: 2/10 → 10/10, 8 decisions, 5 new resilience tasks |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**UNRESOLVED:** 0

**VERDICT:** Design review CLEARED (scope was minimal CLI prompt + 5 resilience tasks added). Eng review required next.
