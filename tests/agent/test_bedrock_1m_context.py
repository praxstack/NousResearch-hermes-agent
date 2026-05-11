"""Tests for the 1M-context beta header on AWS Bedrock Claude models.

Claude Opus 4.6/4.7 and Sonnet 4.6 support a 1M context window, but on AWS
Bedrock (and Azure AI Foundry) that window is still gated behind the
``context-1m-2025-08-07`` beta header as of 2026-04. Without it, Bedrock
caps these models at 200K even though ``model_metadata.py`` advertises 1M.

These tests guard the invariant that the header is always emitted on the
Bedrock client path, and that it survives the MiniMax bearer-auth strip.
"""

from unittest.mock import MagicMock, patch


class TestBedrockContext1MBeta:
    """``context-1m-2025-08-07`` must reach Bedrock Claude requests."""



    def test_common_betas_strips_1m_for_minimax(self):
        """MiniMax bearer-auth endpoints host their own models — strip 1M beta."""
        from agent.anthropic_adapter import (
            _common_betas_for_base_url,
            _CONTEXT_1M_BETA,
        )

        for url in (
            "https://api.minimax.io/anthropic",
            "https://api.minimaxi.com/anthropic",
        ):
            betas = _common_betas_for_base_url(url)
            assert _CONTEXT_1M_BETA not in betas, (
                f"1M beta must be stripped for MiniMax bearer endpoint {url}"
            )
            # Other betas still present
            assert "interleaved-thinking-2025-05-14" in betas

    def test_build_anthropic_bedrock_client_sends_1m_beta(self):
        """AnthropicBedrock client must carry the 1M beta in default_headers.

        This is the load-bearing assertion for the reported bug:
        without this header Bedrock serves Opus 4.6/4.7 with a 200K cap.
        """
        import agent.anthropic_adapter as adapter

        fake_sdk = MagicMock()
        fake_sdk.AnthropicBedrock = MagicMock()

        with patch.object(adapter, "_anthropic_sdk", fake_sdk):
            adapter.build_anthropic_bedrock_client(region="us-west-2")

        call_kwargs = fake_sdk.AnthropicBedrock.call_args.kwargs
        assert call_kwargs["aws_region"] == "us-west-2"

        default_headers = call_kwargs.get("default_headers") or {}
        beta_header = default_headers.get("anthropic-beta", "")
        assert "context-1m-2025-08-07" in beta_header, (
            "Bedrock client must send context-1m-2025-08-07 or Opus 4.6/4.7 "
            "silently caps at 200K context"
        )
        # Other common betas still present — no regression.
        assert "interleaved-thinking-2025-05-14" in beta_header
        assert "fine-grained-tool-streaming-2025-05-14" in beta_header

    def test_build_anthropic_kwargs_includes_1m_for_bedrock_fastmode(self):
        """Fast-mode requests on a Bedrock :1m model carry every beta they need.

        Per-request ``extra_headers`` OVERRIDE client-level ``default_headers``
        in the Anthropic SDK. So when fast-mode is triggered (Opus 4.6 only —
        see ``_FAST_MODE_SUPPORTED_SUBSTRINGS``) on a Bedrock call that also
        wants 1M context, the adapter must rebuild extra_headers with:
          - Everything in ``_COMMON_BETAS`` (interleaved-thinking,
            fine-grained-tool-streaming)
          - The fast-mode beta (``fast-mode-2026-02-01``)
          - The 1M-context beta (``context-1m-2025-08-07``, attached via the
            ``:1m`` suffix path at line ~2300 of anthropic_adapter.py)

        Without the :1m suffix on the model string the 1M beta would NOT
        appear — and that's correct: plain ``claude-opus-4-6`` Bedrock calls
        don't opt into 1M context, so the fast-mode extra_headers correctly
        omit the 1M beta. See the earlier RCA note (2026-05-11) for why the
        original form of this test (without :1m) was semantically invalid:
        it asserted a wire path the adapter can never produce.

        base_url=None mirrors the AnthropicBedrock SDK path (no HTTP URL).
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6:1m",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            reasoning_config=None,
            is_oauth=False,
            # Empty base_url mirrors AnthropicBedrock (no HTTP base URL)
            base_url=None,
            fast_mode=True,
        )
        beta_header = kwargs.get("extra_headers", {}).get("anthropic-beta", "")
        assert "context-1m-2025-08-07" in beta_header, (
            "fast-mode extra_headers on a :1m Bedrock model must carry the "
            "1M beta or Bedrock caps the call at 200K context"
        )
        # Fast-mode beta too — this is the whole reason extra_headers exists
        # on this path (client-level default_headers doesn't carry it).
        assert "fast-mode-2026-02-01" in beta_header, (
            "fast-mode path must attach fast-mode-2026-02-01 beta"
        )
        # Other common betas still present — extra_headers must not erase them.
        assert "interleaved-thinking-2025-05-14" in beta_header
        assert "fine-grained-tool-streaming-2025-05-14" in beta_header

    def test_build_anthropic_kwargs_omits_1m_for_bedrock_fastmode_without_1m_suffix(self):
        """Fast-mode on plain (non-:1m) Opus 4.6 correctly omits 1M beta.

        Regression guard: the original form of the test above asserted that
        ``model="claude-opus-4-6"`` (no :1m) should still carry the 1M beta
        in fast-mode extra_headers. That's wrong — if the caller didn't opt
        into 1M via the ``:1m`` suffix, we shouldn't force the beta onto the
        wire (some Anthropic subscriptions 400 on the long-context beta for
        accounts that don't have it enabled). The fast-mode path correctly
        returns only _COMMON_BETAS + fast-mode beta in that case.

        This test locks the correct behaviour in so a future well-meaning
        contributor doesn't "fix" the no-1M case by always-injecting the
        beta.
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        kwargs = build_anthropic_kwargs(
            model="claude-opus-4-6",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_tokens=1024,
            reasoning_config=None,
            is_oauth=False,
            base_url=None,
            fast_mode=True,
        )
        beta_header = kwargs.get("extra_headers", {}).get("anthropic-beta", "")
        assert "context-1m-2025-08-07" not in beta_header, (
            "plain opus-4-6 (no :1m) must NOT auto-inject 1M beta — the "
            "subscription gate is opt-in via the :1m suffix only"
        )
        # Everything else fast-mode needs still present.
        assert "fast-mode-2026-02-01" in beta_header
        assert "interleaved-thinking-2025-05-14" in beta_header
        assert "fine-grained-tool-streaming-2025-05-14" in beta_header
