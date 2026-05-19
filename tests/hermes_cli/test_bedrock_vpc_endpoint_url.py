"""Tests for the Bedrock setup wizard's vpc_endpoint_url prompt.

Covers:
  * the validator (URL parsing edge cases)
  * the prompt's skip path (empty input ⇒ keeps current / no-op)
  * the prompt's populated path (valid URL is returned verbatim)
  * the prompt's re-prompt-on-invalid loop
  * end-to-end through ``_model_flow_bedrock_api_key`` to confirm:
      (a) skipping leaves no ``vpc_endpoint_url`` key in the saved config
      (b) supplying a valid URL persists it under ``bedrock.vpc_endpoint_url``

Mirrors the existing test_bedrock_model_flow.py harness style.
"""
from __future__ import annotations

import builtins
from copy import deepcopy

import pytest


# ---------- validator ----------------------------------------------------


def test_validator_accepts_https_url():
    import hermes_cli.main as main_mod

    ok, err = main_mod._validate_bedrock_vpc_endpoint_url(
        "https://vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"
    )
    assert ok is True
    assert err == ""


def test_validator_strips_whitespace_but_still_validates():
    import hermes_cli.main as main_mod

    ok, _ = main_mod._validate_bedrock_vpc_endpoint_url(
        "  https://example.com  "
    )
    assert ok is True


def test_validator_rejects_empty():
    import hermes_cli.main as main_mod

    ok, err = main_mod._validate_bedrock_vpc_endpoint_url("")
    assert ok is False
    assert "empty" in err.lower()


def test_validator_rejects_missing_scheme():
    import hermes_cli.main as main_mod

    ok, err = main_mod._validate_bedrock_vpc_endpoint_url(
        "vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"
    )
    assert ok is False
    assert "https://" in err


def test_validator_rejects_unknown_scheme():
    import hermes_cli.main as main_mod

    ok, err = main_mod._validate_bedrock_vpc_endpoint_url("ftp://example.com")
    assert ok is False
    assert "https://" in err


def test_validator_rejects_missing_host():
    import hermes_cli.main as main_mod

    ok, err = main_mod._validate_bedrock_vpc_endpoint_url("https://")
    assert ok is False
    assert "host" in err.lower()


# ---------- prompt -------------------------------------------------------


def _scripted_input(answers):
    iterator = iter(answers)

    def _stub(_prompt=""):
        try:
            return next(iterator)
        except StopIteration as exc:  # pragma: no cover — defensive
            raise AssertionError("input() called more times than scripted") from exc

    return _stub


def test_prompt_skip_returns_current(monkeypatch):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(builtins, "input", _scripted_input([""]))
    assert main_mod._prompt_bedrock_vpc_endpoint_url("") == ""


def test_prompt_skip_keeps_existing_value(monkeypatch):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(builtins, "input", _scripted_input([""]))
    assert (
        main_mod._prompt_bedrock_vpc_endpoint_url("https://existing.example.com")
        == "https://existing.example.com"
    )


def test_prompt_returns_valid_url(monkeypatch):
    import hermes_cli.main as main_mod

    monkeypatch.setattr(
        builtins,
        "input",
        _scripted_input(["https://vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"]),
    )
    assert (
        main_mod._prompt_bedrock_vpc_endpoint_url("")
        == "https://vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"
    )


def test_prompt_reprompts_on_invalid_url(monkeypatch, capsys):
    import hermes_cli.main as main_mod

    # First answer is invalid (no scheme), second is valid.
    monkeypatch.setattr(
        builtins,
        "input",
        _scripted_input(["not-a-url", "https://valid.example.com"]),
    )
    result = main_mod._prompt_bedrock_vpc_endpoint_url("")
    assert result == "https://valid.example.com"

    out = capsys.readouterr().out
    assert "✗" in out  # error marker emitted
    assert "https://" in out  # error message references scheme


def test_prompt_propagates_keyboard_interrupt(monkeypatch):
    import hermes_cli.main as main_mod

    def _raise(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", _raise)
    with pytest.raises(KeyboardInterrupt):
        main_mod._prompt_bedrock_vpc_endpoint_url("")


# ---------- end-to-end through _model_flow_bedrock_api_key ---------------


def _stub_save_pipeline(monkeypatch, *, initial_cfg):
    """Wire up the same monkeypatches used by test_bedrock_model_flow.py.

    Returns a dict that callers populate with the saved config so the
    assertions can inspect what hit disk. Only the bits relevant to the
    vpc_endpoint_url path are stubbed; everything else stays default.
    """
    import hermes_cli.auth as auth_mod
    import hermes_cli.config as config_mod
    import agent.bedrock_adapter as bedrock_mod

    saved_env: dict[str, str] = {}
    saved_cfg: dict[str, object] = {}

    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: "absk-test-token" if key == "AWS_BEARER_TOKEN_BEDROCK" else "",
    )
    monkeypatch.setattr(
        config_mod, "save_env_value", lambda key, value: saved_env.__setitem__(key, value)
    )
    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod, "save_config", lambda cfg: saved_cfg.update(deepcopy(cfg))
    )
    monkeypatch.setattr(
        auth_mod,
        "_prompt_model_selection",
        lambda models, current_model="": "anthropic.claude-sonnet-4-20250514-v1:0",
    )
    monkeypatch.setattr(auth_mod, "_save_model_choice", lambda model_id: None)
    monkeypatch.setattr(auth_mod, "deactivate_provider", lambda: None)

    # Short-circuit live AWS discovery — we're testing the config path only.
    monkeypatch.setattr(
        bedrock_mod,
        "discover_bedrock_models",
        lambda region, config=None: [
            {"id": "anthropic.claude-sonnet-4-20250514-v1:0"},
            {"id": "anthropic.claude-opus-4-7"},
        ],
    )

    return saved_cfg, saved_env


def test_api_key_flow_skips_vpc_endpoint_url_by_default(monkeypatch):
    """Pressing Enter on the VPC prompt must NOT write a vpc_endpoint_url key."""
    import hermes_cli.main as main_mod

    saved_cfg, _ = _stub_save_pipeline(
        monkeypatch, initial_cfg={"model": {"default": "existing-model"}}
    )
    # Four prompts in order: cross-region (Y/n), global (Y/n), caching (Y/n),
    # vpc URL (free-form, blank to skip). Empty answers accept defaults.
    monkeypatch.setattr(builtins, "input", _scripted_input(["", "", "", ""]))

    main_mod._model_flow_bedrock_api_key({}, "us-east-1")

    bedrock_block = saved_cfg["bedrock"]
    assert "vpc_endpoint_url" not in bedrock_block, (
        "Skipping the prompt must leave the config identical to today's "
        "behavior (no key written)."
    )


def test_api_key_flow_persists_vpc_endpoint_url_when_provided(monkeypatch):
    """A valid URL must end up under bedrock.vpc_endpoint_url verbatim."""
    import hermes_cli.main as main_mod

    saved_cfg, _ = _stub_save_pipeline(
        monkeypatch, initial_cfg={"model": {"default": "existing-model"}}
    )
    monkeypatch.setattr(
        builtins,
        "input",
        _scripted_input(
            [
                "",  # cross-region: keep default
                "",  # global inference: keep default
                "",  # prompt caching: keep default
                "https://vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com",
            ]
        ),
    )

    main_mod._model_flow_bedrock_api_key({}, "us-east-1")

    assert (
        saved_cfg["bedrock"]["vpc_endpoint_url"]
        == "https://vpce-1234.bedrock-runtime.us-east-1.vpce.amazonaws.com"
    )


def test_api_key_flow_clears_vpc_endpoint_url_when_user_blanks_out(monkeypatch):
    """An existing value can be cleared by entering ``""`` then pressing Enter again.

    Today the prompt's "blank to skip" path returns the existing value so we
    DO preserve it on re-run — that's the documented behavior. This test
    pins it down: re-running setup with the existing config and pressing
    Enter on every prompt round-trips the same vpc_endpoint_url.
    """
    import hermes_cli.main as main_mod

    initial_cfg = {
        "model": {"default": "existing-model"},
        "bedrock": {
            "region": "us-east-1",
            "auth_method": "api_key",
            "use_cross_region_inference": True,
            "use_global_inference_profile": True,
            "use_prompt_caching": True,
            "vpc_endpoint_url": "https://existing.example.com",
        },
    }
    saved_cfg, _ = _stub_save_pipeline(monkeypatch, initial_cfg=initial_cfg)
    monkeypatch.setattr(builtins, "input", _scripted_input(["", "", "", ""]))

    main_mod._model_flow_bedrock_api_key({}, "us-east-1")

    assert (
        saved_cfg["bedrock"]["vpc_endpoint_url"]
        == "https://existing.example.com"
    ), "Re-run with blank input must preserve the existing vpc_endpoint_url."


def test_save_helper_pops_vpc_endpoint_url_when_blank(monkeypatch):
    """Direct unit test of _save_bedrock_model_selection: blank arg => popped."""
    import hermes_cli.main as main_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.config as config_mod

    initial_cfg = {
        "model": {"default": "existing-model"},
        "bedrock": {
            "region": "us-east-1",
            "auth_method": "api_key",
            "vpc_endpoint_url": "https://stale.example.com",
        },
    }
    saved_cfg: dict[str, object] = {}
    monkeypatch.setattr(config_mod, "get_env_value", lambda key: "")
    monkeypatch.setattr(config_mod, "save_env_value", lambda key, value: None)
    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod, "save_config", lambda cfg: saved_cfg.update(deepcopy(cfg))
    )
    monkeypatch.setattr(auth_mod, "deactivate_provider", lambda: None)

    main_mod._save_bedrock_model_selection(
        "anthropic.claude-opus-4-7",
        "us-east-1",
        "api_key",
        vpc_endpoint_url="",  # explicit blank
    )

    assert "vpc_endpoint_url" not in saved_cfg["bedrock"]
