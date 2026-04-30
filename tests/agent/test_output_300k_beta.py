"""Tests for the output-300k-2026-03-24 batch-API beta wiring.

Per Anthropic release notes (2026-03-30), the Message Batches API raises
max_tokens to 300k on Opus 4.7, Opus 4.6, and Sonnet 4.6 when the
``output-300k-2026-03-24`` beta header is sent. Sonnet 3.7/4.5, Haiku,
and older Opus retain their sync-API ceilings.

These tests pin:
  * Model-capability detection (which models accept the beta)
  * max_tokens ceiling differs by mode (sync vs batch)
  * Beta header only appears under batch_mode=True AND supported model
  * Beta merges cleanly with existing extra_headers (1M context, fast mode)
  * Sync path completely unaffected (no header leak)
"""

from __future__ import annotations

import pytest

from agent.anthropic_adapter import (
    _OUTPUT_300K_BETA,
    _get_anthropic_max_output,
    _supports_output_300k,
    build_anthropic_kwargs,
)


class TestSupportsOutput300k:
    @pytest.mark.parametrize("model", [
        "claude-opus-4-7",
        "claude-opus-4-7-20260416",
        "claude-opus-4-6",
        "claude-opus-4-6-20260101",
        "claude-sonnet-4-6",
        "claude-sonnet-4-6-20250929",
        "anthropic.claude-opus-4-7",
        "global.anthropic.claude-opus-4-7",
        "us.anthropic.claude-sonnet-4-6-20250929",
        "anthropic/claude-opus-4.7",  # dot form — normalizer handles it
    ])
    def test_supported_models(self, model):
        assert _supports_output_300k(model) is True

    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-opus-4-5",
        "claude-opus-4",
        "claude-3-7-sonnet",
        "claude-3-5-sonnet",
        "claude-3-opus",
        "",
        "gpt-5.4",
        "random-string",
    ])
    def test_unsupported_models(self, model):
        assert _supports_output_300k(model) is False


class TestMaxOutputBatchMode:
    def test_opus_4_7_sync_stays_at_128k(self):
        assert _get_anthropic_max_output("claude-opus-4-7") == 128_000
        assert _get_anthropic_max_output("claude-opus-4-7", batch_mode=False) == 128_000

    def test_opus_4_7_batch_unlocks_300k(self):
        assert _get_anthropic_max_output("claude-opus-4-7", batch_mode=True) == 300_000

    def test_sonnet_4_6_batch_unlocks_300k(self):
        assert _get_anthropic_max_output("claude-sonnet-4-6", batch_mode=True) == 300_000

    def test_haiku_4_5_batch_keeps_sync_ceiling(self):
        # Haiku isn't in the 300k support list — batch_mode must fall
        # through to the sync ceiling (64k), not silently unlock 300k.
        assert _get_anthropic_max_output("claude-haiku-4-5", batch_mode=True) == 64_000

    def test_sonnet_3_7_batch_keeps_sync_ceiling(self):
        assert _get_anthropic_max_output("claude-3-7-sonnet", batch_mode=True) == 128_000

    def test_bedrock_model_id_with_prefix_resolves_correctly(self):
        # Bedrock IDs like "us.anthropic.claude-opus-4-7" must also hit
        # the 300k branch under batch_mode.
        assert _get_anthropic_max_output(
            "us.anthropic.claude-opus-4-7", batch_mode=True
        ) == 300_000


class TestBuildKwargsBetaInjection:
    """build_anthropic_kwargs must inject the beta header correctly."""

    def _minimal_kwargs(self, model, *, batch_mode=False, max_tokens=None):
        return build_anthropic_kwargs(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=max_tokens,
            reasoning_config=None,
            batch_mode=batch_mode,
        )

    def _extract_betas(self, kwargs):
        hdrs = kwargs.get("extra_headers") or {}
        beta_str = hdrs.get("anthropic-beta", "") or ""
        return [b.strip() for b in beta_str.split(",") if b.strip()]

    def test_sync_mode_never_attaches_output_300k_beta(self):
        kw = self._minimal_kwargs("claude-opus-4-7", batch_mode=False)
        assert _OUTPUT_300K_BETA not in self._extract_betas(kw)

    def test_batch_mode_attaches_beta_for_supported_model(self):
        kw = self._minimal_kwargs("claude-opus-4-7", batch_mode=True)
        assert _OUTPUT_300K_BETA in self._extract_betas(kw)

    def test_batch_mode_does_not_attach_beta_for_unsupported_model(self):
        kw = self._minimal_kwargs("claude-haiku-4-5", batch_mode=True)
        betas = self._extract_betas(kw)
        assert _OUTPUT_300K_BETA not in betas
        # ... and max_tokens should fall back to the sync ceiling.
        assert kw["max_tokens"] == 64_000

    def test_batch_mode_lifts_max_tokens_for_supported_model(self):
        kw = self._minimal_kwargs("claude-opus-4-7", batch_mode=True)
        assert kw["max_tokens"] == 300_000

    def test_explicit_max_tokens_still_wins_in_batch_mode(self):
        # Caller that passes max_tokens=50000 must get 50000 even when
        # batch_mode is True — the beta only changes the FALLBACK.
        kw = self._minimal_kwargs(
            "claude-opus-4-7", batch_mode=True, max_tokens=50_000
        )
        assert kw["max_tokens"] == 50_000

    def test_beta_merges_with_1m_context_extra_headers(self):
        # Model with the :1m suffix already attaches the 1M beta via
        # extra_headers. Adding output-300k must merge, not overwrite.
        kw = build_anthropic_kwargs(
            model="claude-opus-4-7:1m",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=None,
            reasoning_config=None,
            batch_mode=True,
        )
        betas = self._extract_betas(kw)
        assert "context-1m-2025-08-07" in betas
        assert _OUTPUT_300K_BETA in betas

    def test_beta_is_idempotent_on_rebuild(self):
        # Double-invocation shouldn't duplicate the header. (Guards a
        # future regression where the header merge logic appends without
        # dedup.)
        kw1 = self._minimal_kwargs("claude-opus-4-7", batch_mode=True)
        kw2 = self._minimal_kwargs("claude-opus-4-7", batch_mode=True)
        # Freshly-built kwargs each have exactly one output-300k entry.
        for kw in (kw1, kw2):
            betas = self._extract_betas(kw)
            assert betas.count(_OUTPUT_300K_BETA) == 1
