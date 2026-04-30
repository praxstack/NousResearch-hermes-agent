"""Tests for Bedrock Converse cache-telemetry pass-through.

Bedrock Converse returns ``usage.cacheReadInputTokens`` /
``usage.cacheWriteInputTokens`` (camelCase) when cachePoint markers
fired on the request. Hermes must:
  1. Read them in normalize_converse_response (surface on SimpleNamespace)
  2. Propagate into the Usage dataclass via BedrockTransport.normalize_response
  3. Expose via BedrockTransport.extract_cache_stats for telemetry callers

These tests pin each link in the chain.
"""

from __future__ import annotations

import pytest

from agent.bedrock_adapter import normalize_converse_response
from agent.transports.bedrock import BedrockTransport
from agent.transports.types import Usage


def _mock_response(*, cache_read=None, cache_write=None):
    """Build a minimal Converse response dict, optionally with cache fields."""
    usage = {"inputTokens": 120, "outputTokens": 45}
    if cache_read is not None:
        usage["cacheReadInputTokens"] = cache_read
    if cache_write is not None:
        usage["cacheWriteInputTokens"] = cache_write
    return {
        "output": {
            "message": {"role": "assistant", "content": [{"text": "hello"}]}
        },
        "stopReason": "end_turn",
        "usage": usage,
        "modelId": "us.anthropic.claude-sonnet-4-6-20250929-v1:0",
    }


class TestNormalizeConverseResponseCacheFields:
    def test_missing_cache_fields_yield_zero(self):
        resp = normalize_converse_response(_mock_response())
        assert resp.usage.cache_read_input_tokens == 0
        assert resp.usage.cache_creation_input_tokens == 0
        # CamelCase aliases are also zero.
        assert resp.usage.cacheReadInputTokens == 0
        assert resp.usage.cacheWriteInputTokens == 0

    def test_cache_read_field_surfaces_in_snake_case(self):
        resp = normalize_converse_response(_mock_response(cache_read=2500))
        assert resp.usage.cache_read_input_tokens == 2500

    def test_cache_write_field_surfaces_in_snake_case(self):
        resp = normalize_converse_response(_mock_response(cache_write=800))
        assert resp.usage.cache_creation_input_tokens == 800

    def test_camel_case_aliases_populated(self):
        """Both camelCase (Bedrock-native) and snake_case (Anthropic-style)
        aliases are populated. This lets downstream consumers use whichever
        convention they already read."""
        resp = normalize_converse_response(
            _mock_response(cache_read=1000, cache_write=300)
        )
        assert resp.usage.cacheReadInputTokens == 1000
        assert resp.usage.cacheWriteInputTokens == 300
        assert resp.usage.cache_read_input_tokens == 1000
        assert resp.usage.cache_creation_input_tokens == 300

    def test_input_tokens_independent_of_cache(self):
        # Per AWS docs, inputTokens represents NEW uncached tokens — the
        # cache read/write counts are separate and NOT included in it.
        resp = normalize_converse_response(
            _mock_response(cache_read=5000, cache_write=0)
        )
        assert resp.usage.prompt_tokens == 120
        assert resp.usage.cache_read_input_tokens == 5000


class TestBedrockTransportUsagePropagation:
    """Transport normalization must carry cache fields into Usage."""

    def test_usage_with_cache_fields_propagated(self):
        raw = _mock_response(cache_read=1500, cache_write=200)
        t = BedrockTransport()
        norm = t.normalize_response(raw)
        assert isinstance(norm.usage, Usage)
        assert norm.usage.cached_tokens == 1500
        assert norm.usage.cache_creation_tokens == 200
        assert norm.usage.prompt_tokens == 120
        assert norm.usage.completion_tokens == 45
        assert norm.usage.total_tokens == 165

    def test_usage_defaults_to_zero_without_cache(self):
        raw = _mock_response()
        t = BedrockTransport()
        norm = t.normalize_response(raw)
        assert norm.usage.cached_tokens == 0
        assert norm.usage.cache_creation_tokens == 0


class TestExtractCacheStats:
    """BedrockTransport.extract_cache_stats must mirror Anthropic shape."""

    def test_none_when_no_cache_fields(self):
        raw = _mock_response()
        t = BedrockTransport()
        assert t.extract_cache_stats(raw) is None
        # Also on the normalized SimpleNamespace:
        norm = normalize_converse_response(raw)
        assert t.extract_cache_stats(norm) is None

    def test_returns_dict_from_raw_boto3_dict(self):
        raw = _mock_response(cache_read=4096, cache_write=1024)
        t = BedrockTransport()
        stats = t.extract_cache_stats(raw)
        assert stats == {"cached_tokens": 4096, "creation_tokens": 1024}

    def test_returns_dict_from_normalized_response(self):
        # After the dispatch site calls normalize_converse_response, the
        # returned object is a SimpleNamespace with a .usage attribute —
        # extract_cache_stats must read from that shape too.
        norm = normalize_converse_response(_mock_response(cache_read=800, cache_write=0))
        t = BedrockTransport()
        stats = t.extract_cache_stats(norm)
        assert stats == {"cached_tokens": 800, "creation_tokens": 0}

    def test_zero_values_return_none(self):
        # Matches Anthropic transport semantics: 0+0 → None, not a dict
        # of zeros. Keeps downstream "if stats is not None" checks honest.
        raw = _mock_response(cache_read=0, cache_write=0)
        t = BedrockTransport()
        assert t.extract_cache_stats(raw) is None

    def test_shape_matches_anthropic_transport(self):
        # Both transports must return dicts with the same keys so
        # telemetry consumers can be transport-agnostic.
        from agent.transports.anthropic import AnthropicTransport
        from types import SimpleNamespace

        anth_usage = SimpleNamespace(
            cache_read_input_tokens=500, cache_creation_input_tokens=100,
        )
        anth_resp = SimpleNamespace(usage=anth_usage)
        anth_t = AnthropicTransport()
        anth_stats = anth_t.extract_cache_stats(anth_resp)

        bed_raw = _mock_response(cache_read=500, cache_write=100)
        bed_stats = BedrockTransport().extract_cache_stats(bed_raw)

        assert set(anth_stats.keys()) == set(bed_stats.keys())
        assert anth_stats == bed_stats
