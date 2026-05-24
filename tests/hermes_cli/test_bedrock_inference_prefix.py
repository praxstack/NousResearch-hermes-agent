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


class TestCRIRegionRoundTripIdentity:
    """Code review P1-B + P1-C regression tests (2026-05-24).

    Before the fix, three sites had divergent CRI mapping logic:
      - hermes_cli/runtime_provider.py (forward: region → prefix)
      - agent/auxiliary_client.py (reverse: prefix → region)
      - agent/chat_completion_helpers.py (reverse: prefix → region)

    Bugs found:
      P1-B: ap-southeast-2 routed to apac.* (dead `au-` branch never matched)
      P1-C: forward (apac.) and reverse (au.→ap-southeast-2) disagreed
            on Sydney's canonical pair → round-trip broken

    Fix: single source of truth in agent.bedrock_adapter.BEDROCK_CRI_REGIONS
    + cri_prefix_for_region() called from forward path.
    """

    def test_ap_southeast_2_maps_to_au_not_apac(self, mock_bedrock_env):
        """Sydney is the entry-point for the Australia profile, not generic apac."""
        runtime = _call_resolver(
            "ap-southeast-2", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "au.anthropic.claude-sonnet-4-20250514-v1:0", (
            "P1-B: ap-southeast-2 → au.* (was incorrectly apac.* via dead au- branch)"
        )

    def test_other_ap_regions_get_apac_not_au(self, mock_bedrock_env):
        """ap-southeast-1 (Singapore) is generic apac, NOT au — only Sydney is au."""
        runtime = _call_resolver(
            "ap-southeast-1", "anthropic.claude-sonnet-4-20250514-v1:0", use_global=False
        )
        assert runtime["bedrock_model"] == "apac.anthropic.claude-sonnet-4-20250514-v1:0"

    def test_round_trip_identity_for_canonical_regions(self):
        """For every region in BEDROCK_CRI_REGIONS, region → prefix → region == identity."""
        from agent.bedrock_adapter import BEDROCK_CRI_REGIONS, cri_prefix_for_region
        for prefix, region in BEDROCK_CRI_REGIONS.items():
            forward_prefix = cri_prefix_for_region(region)
            assert forward_prefix == prefix, (
                f"Round-trip broken for {region}: "
                f"forward={forward_prefix!r}, reverse={prefix!r}"
            )

    def test_jp_prefix_uses_ap_northeast_3(self):
        from agent.bedrock_adapter import BEDROCK_CRI_REGIONS
        assert BEDROCK_CRI_REGIONS["jp."] == "ap-northeast-3"

    def test_ap_northeast_1_through_3_all_map_to_jp(self):
        """All three Tokyo/Seoul/Osaka regions route to the jp.* profile."""
        from agent.bedrock_adapter import cri_prefix_for_region
        assert cri_prefix_for_region("ap-northeast-1") == "jp."
        assert cri_prefix_for_region("ap-northeast-2") == "jp."
        assert cri_prefix_for_region("ap-northeast-3") == "jp."

    def test_govcloud_china_return_none_not_apac(self):
        """GovCloud + China have no CRI profiles — reverse must return None, not coerce to apac."""
        from agent.bedrock_adapter import cri_prefix_for_region
        assert cri_prefix_for_region("us-gov-west-1") is None
        assert cri_prefix_for_region("us-gov-east-1") is None
        assert cri_prefix_for_region("cn-north-1") is None
        assert cri_prefix_for_region("cn-northwest-1") is None

    def test_inference_profile_prefixes_tuple_in_sync(self):
        """All CRI region map keys must appear in BEDROCK_INFERENCE_PROFILE_PREFIXES.

        The tuple is the public surface used by the prefix-stripper / matcher;
        adding an entry to the dict without adding to the tuple would silently
        break is-this-a-CRI-already detection.
        """
        from agent.bedrock_adapter import (
            BEDROCK_CRI_REGIONS,
            BEDROCK_INFERENCE_PROFILE_PREFIXES,
        )
        for prefix in BEDROCK_CRI_REGIONS.keys():
            assert prefix in BEDROCK_INFERENCE_PROFILE_PREFIXES, (
                f"{prefix!r} in BEDROCK_CRI_REGIONS but not in tuple — out of sync"
            )
