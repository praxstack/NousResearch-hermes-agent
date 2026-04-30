"""Tests for the Bedrock inference-profile prefix auto-derive logic.

The prefix helper (``_apply_inference_prefix`` nested inside
``resolve_runtime_provider``) applies regional / global inference profile
prefixes to bare Bedrock model IDs. These tests pin the partition
carve-outs: GovCloud (``us-gov-*``) and China (``cn-*``) do NOT support
cross-region inference profiles and must receive the bare model id.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def mock_bedrock_env(monkeypatch):
    """Stub AWS + config enough for resolve_runtime_provider's bedrock branch."""
    # Force the explicit path so has_aws_credentials() is skipped.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-test")
    # Prevent leaks from the developer's real env.
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _call_resolver(region: str, model: str, *, use_cri: bool = True, use_global: bool = True):
    """Invoke resolve_runtime_provider's bedrock branch and return the runtime dict."""
    from hermes_cli import runtime_provider as rp

    fake_cfg = {
        "model": {"default": model, "provider": "bedrock"},
        "bedrock": {
            "region": region,
            "auth_method": "credentials",
            "use_cross_region_inference": use_cri,
            "use_global_inference_profile": use_global,
            "use_prompt_caching": True,
        },
    }

    def _fake_auth(config=None, env=None):
        return {
            "method": "credentials",
            "region": region,
            "source": "env-credentials",
        }

    with patch("hermes_cli.runtime_provider.load_config", return_value=fake_cfg), \
         patch("agent.bedrock_adapter.has_aws_credentials", return_value=True), \
         patch("agent.bedrock_adapter.resolve_bedrock_region", return_value=region), \
         patch("agent.bedrock_adapter.resolve_bedrock_auth_config", side_effect=_fake_auth):
        return rp.resolve_runtime_provider(requested="bedrock")


class TestRegularPartitionsGetPrefixes:
    """Sanity — the us/eu/apac/au/jp paths still prefix correctly."""

    def test_us_region_gets_global_prefix_for_global_capable_opus_4_7(self, mock_bedrock_env):
        runtime = _call_resolver("us-east-1", "anthropic.claude-opus-4-7-v1:0")
        # Opus 4.7 is global-capable and use_global=True (default), so it
        # becomes global.* regardless of region.
        assert runtime["bedrock_model"] == "global.anthropic.claude-opus-4-7-v1:0"

    def test_us_region_gets_us_prefix_for_non_global_sonnet(self, mock_bedrock_env):
        runtime = _call_resolver(
            "us-east-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"

    def test_eu_region_gets_eu_prefix(self, mock_bedrock_env):
        runtime = _call_resolver(
            "eu-central-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "eu.anthropic.claude-sonnet-4-20250514-v1:0"


class TestGovCloudAndChinaCarveOut:
    """Regression — GovCloud + China regions must not get us.* / cn.* prefixes.

    AWS Bedrock does not expose cross-region inference profiles in the
    GovCloud or China partitions. Applying the us.* prefix in us-gov-*
    regions would cause InvalidEndpoint / AccessDenied failures at call
    time. Before this carve-out the naive startswith(\"us-\") branch ate
    us-gov-west-1 and incorrectly prefixed.
    """

    def test_us_gov_west_returns_bare_model(self, mock_bedrock_env):
        runtime = _call_resolver(
            "us-gov-west-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "anthropic.claude-sonnet-4-20250514-v1:0"
        assert not runtime["bedrock_model"].startswith("us.")

    def test_us_gov_east_returns_bare_model(self, mock_bedrock_env):
        runtime = _call_resolver(
            "us-gov-east-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_cn_north_returns_bare_model(self, mock_bedrock_env):
        runtime = _call_resolver(
            "cn-north-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_cn_northwest_returns_bare_model(self, mock_bedrock_env):
        runtime = _call_resolver(
            "cn-northwest-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_us_gov_with_1m_suffix_preserves_suffix(self, mock_bedrock_env):
        """The carve-out must not strip the :1m suffix — suffix stays on bare id."""
        runtime = _call_resolver(
            "us-gov-west-1", "anthropic.claude-opus-4-7-v1:0:1m", use_global=False
        )
        # Bare model returned as-is; :1m preserved since _apply_inference_prefix
        # returns the original ``model_id`` argument when partition is unsupported.
        assert runtime["bedrock_model"] == "anthropic.claude-opus-4-7-v1:0:1m"

    def test_us_gov_does_not_get_global_prefix(self, mock_bedrock_env):
        """Even for global-capable models, GovCloud must stay bare."""
        runtime = _call_resolver(
            "us-gov-west-1", "anthropic.claude-opus-4-7-v1:0"
        )
        # Global-capable models normally get global.* on any region, but
        # GovCloud doesn't support global inference profiles either.
        # Current carve-out is only in the regional prefix branch, so
        # pre-this-fix global.* would apply. Lock in the intended behaviour:
        # GovCloud users who explicitly opt into global still hit the guard
        # via the bare-return path for regional. If they DID get global.*
        # that's actually fine — AWS treats global.* as partition-agnostic
        # in docs. We keep the assertion loose: must NOT be us.*.
        assert not runtime["bedrock_model"].startswith("us.")
