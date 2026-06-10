"""Tests for Claude-first family ordering in the Bedrock /model picker.

Incident (2026-06-11): `/model` on Bedrock listed `global.amazon.nova-2-lite-v1:0`
at index 0 because discover_bedrock_models sorted by
``(global-first, name.lower())`` and "Amazon Nova..." alphabetizes above
"Claude...". A careless `/model`+Enter+Enter silently switched the session to
a tiny non-reasoning model.

Fix: prepend a family-rank tier to the sort key so Claude Opus (the user's
locked default family) ranks first, other Claude next, other reasoning models
next, and small/utility models (Nova, Titan, embed, stability) last. The
existing ``(global-first, name)`` terms are preserved as LOWER-priority
tiebreakers, so the within-family global-first contract
(``test_global_profiles_sorted_first``) stays green.

Council-locked policy (llm-council-plus, 2026-06-11, 2/2 unanimous):
  Tier 0: Claude Opus
  Tier 1: Claude (Fable, Sonnet, Haiku, other Claude)
  Tier 2: other reasoning (DeepSeek, Llama, Qwen, Mistral, GLM, Kimi,
          MiniMax, Nemotron, Cohere, ...) + UNKNOWN/new families
  Tier 3: small/utility/non-text-primary (Nova, Titan, embed, rerank,
          stability, twelvelabs, ...)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# _bedrock_family_rank — pure tier classifier
# ---------------------------------------------------------------------------

class TestBedrockFamilyRank:
    def test_claude_opus_is_tier_0(self):
        from agent.bedrock_adapter import _bedrock_family_rank
        assert _bedrock_family_rank("us.anthropic.claude-opus-4-8") == 0
        assert _bedrock_family_rank("global.anthropic.claude-opus-4-8:1m") == 0
        assert _bedrock_family_rank("anthropic.claude-opus-4-1-20250805-v1:0") == 0

    def test_other_claude_is_tier_1(self):
        from agent.bedrock_adapter import _bedrock_family_rank
        assert _bedrock_family_rank("us.anthropic.claude-fable-5") == 1
        assert _bedrock_family_rank("global.anthropic.claude-sonnet-4-6") == 1
        assert _bedrock_family_rank("us.anthropic.claude-haiku-4-5-20251001-v1:0") == 1

    def test_opus_ranks_above_fable_within_claude(self):
        # Q1 council verdict: Opus (the locked default) must outrank Fable.
        from agent.bedrock_adapter import _bedrock_family_rank
        assert _bedrock_family_rank("us.anthropic.claude-opus-4-8") < \
            _bedrock_family_rank("us.anthropic.claude-fable-5")

    def test_other_reasoning_is_tier_2(self):
        from agent.bedrock_adapter import _bedrock_family_rank
        assert _bedrock_family_rank("deepseek.v3.2") == 2
        assert _bedrock_family_rank("us.meta.llama4-maverick-17b-instruct-v1:0") == 2
        assert _bedrock_family_rank("qwen.qwen3-32b-v1:0") == 2
        assert _bedrock_family_rank("zai.glm-5") == 2
        assert _bedrock_family_rank("moonshot.kimi-k2-thinking") == 2
        assert _bedrock_family_rank("minimax.minimax-m2.5") == 2
        assert _bedrock_family_rank("mistral.mistral-large-3-675b-instruct") == 2

    def test_small_utility_is_tier_3(self):
        from agent.bedrock_adapter import _bedrock_family_rank
        assert _bedrock_family_rank("global.amazon.nova-2-lite-v1:0") == 3
        assert _bedrock_family_rank("us.amazon.nova-pro-v1:0") == 3
        assert _bedrock_family_rank("amazon.nova-micro-v1:0") == 3
        assert _bedrock_family_rank("global.cohere.embed-v4:0") == 3
        assert _bedrock_family_rank("us.twelvelabs.marengo-embed-3-0-v1:0") == 3

    def test_nova_ranks_below_every_claude(self):
        # The exact incident: Nova must never outrank a Claude model.
        from agent.bedrock_adapter import _bedrock_family_rank
        nova = _bedrock_family_rank("global.amazon.nova-2-lite-v1:0")
        for claude in (
            "us.anthropic.claude-opus-4-8",
            "us.anthropic.claude-fable-5",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        ):
            assert _bedrock_family_rank(claude) < nova

    def test_unknown_family_defaults_to_tier_2_not_3(self):
        # Q2 council verdict: unknown/new families default to Tier 2
        # (visible mid-list), NOT Tier 3 (silently buried).
        from agent.bedrock_adapter import _bedrock_family_rank
        rank = _bedrock_family_rank("acme.brand-new-reasoner-v9")
        assert rank == 2

    def test_rank_is_total_order_safe_on_garbage(self):
        from agent.bedrock_adapter import _bedrock_family_rank
        # Must never raise; returns an int for any string.
        for junk in ("", "   ", "no-dots", "::::", "GLOBAL.ANTHROPIC.CLAUDE-OPUS-4-8"):
            assert isinstance(_bedrock_family_rank(junk), int)


# ---------------------------------------------------------------------------
# discover_bedrock_models — end-to-end ordering with the new tier key
# ---------------------------------------------------------------------------

class TestDiscoverOrdering:
    def _client_with(self, foundation_ids, profile_ids):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.list_foundation_models.return_value = {
            "modelSummaries": [
                {
                    "modelId": mid,
                    "modelName": mid.split(".")[-1],
                    "providerName": mid.split(".")[0].title(),
                    "inputModalities": ["TEXT"],
                    "outputModalities": ["TEXT"],
                    "responseStreamingSupported": True,
                    "modelLifecycle": {"status": "ACTIVE"},
                }
                for mid in foundation_ids
            ]
        }
        c.list_inference_profiles.return_value = {
            "inferenceProfileSummaries": [
                {
                    "inferenceProfileId": pid,
                    "inferenceProfileName": pid,
                    "status": "ACTIVE",
                    "models": [],
                }
                for pid in profile_ids
            ]
        }
        return c

    def test_claude_opus_first_nova_last(self):
        from unittest.mock import patch
        from agent.bedrock_adapter import discover_bedrock_models, reset_discovery_cache
        reset_discovery_cache()
        client = self._client_with(
            foundation_ids=["deepseek.v3.2"],
            profile_ids=[
                "global.amazon.nova-2-lite-v1:0",
                "global.anthropic.claude-opus-4-8",
                "global.anthropic.claude-fable-5",
            ],
        )
        with patch("agent.bedrock_adapter._get_bedrock_control_client", return_value=client):
            models = discover_bedrock_models("us-east-1")
        ids = [m["id"] for m in models]
        # Claude Opus first, Nova strictly last.
        assert ids[0] == "global.anthropic.claude-opus-4-8"
        assert ids[-1] == "global.amazon.nova-2-lite-v1:0"
        # Fable (tier 1) sits above DeepSeek (tier 2) above Nova (tier 3).
        assert ids.index("global.anthropic.claude-fable-5") < ids.index("deepseek.v3.2")
        assert ids.index("deepseek.v3.2") < ids.index("global.amazon.nova-2-lite-v1:0")

    def test_global_first_preserved_within_family(self):
        # Regression guard mirroring test_global_profiles_sorted_first:
        # within the SAME family tier, the global. profile must precede the
        # bare regional/foundation id.
        from unittest.mock import patch
        from agent.bedrock_adapter import discover_bedrock_models, reset_discovery_cache
        reset_discovery_cache()
        client = self._client_with(
            foundation_ids=["anthropic.claude-opus-4-8"],
            profile_ids=["global.anthropic.claude-opus-4-8"],
        )
        with patch("agent.bedrock_adapter._get_bedrock_control_client", return_value=client):
            models = discover_bedrock_models("us-east-1")
        ids = [m["id"] for m in models]
        assert ids.index("global.anthropic.claude-opus-4-8") < ids.index("anthropic.claude-opus-4-8")
