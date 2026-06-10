from __future__ import annotations

from copy import deepcopy


def test_bedrock_api_key_flow_saves_native_bedrock_provider(monkeypatch):
    import hermes_cli.auth as auth_mod
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    saved_env: dict[str, str] = {}
    saved_cfg: dict[str, object] = {}
    initial_cfg = {"model": {"default": "existing-model"}}

    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: "absk-test-token" if key == "AWS_BEARER_TOKEN_BEDROCK" else "",
    )
    monkeypatch.setattr(
        config_mod,
        "save_env_value",
        lambda key, value: saved_env.__setitem__(key, value),
    )
    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod,
        "save_config",
        lambda cfg: saved_cfg.update(deepcopy(cfg)),
    )
    monkeypatch.setattr(
        auth_mod,
        "_prompt_model_selection",
        lambda models, current_model="": "anthropic.claude-sonnet-4-20250514-v1:0",
    )
    monkeypatch.setattr(auth_mod, "_save_model_choice", lambda model_id: None)
    monkeypatch.setattr(auth_mod, "deactivate_provider", lambda: None)

    # Stub input() so the new cross-region / caching toggle prompts don't block.
    # Default (empty) → accept the default value (True for all three).
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    # The auth-flow calls _discover_bedrock_model_list which ultimately calls
    # discover_bedrock_models(region, config=config_preview) on the real boto3
    # client. That requires a live AWS_BEARER_TOKEN_BEDROCK in os.environ,
    # which the test harness doesn't set. Short-circuit by returning a
    # fabricated model list — the point of this test is the config-save
    # flow, not live AWS discovery.
    import agent.bedrock_adapter as bedrock_mod
    monkeypatch.setattr(
        bedrock_mod,
        "discover_bedrock_models",
        lambda region, config=None: [
            {"id": "anthropic.claude-sonnet-4-20250514-v1:0"},
            {"id": "anthropic.claude-opus-4-7"},
        ],
    )

    # Upstream extracted the _model_flow_* functions out of hermes_cli.main
    # into hermes_cli.model_setup_flows; call the canonical location.
    import hermes_cli.model_setup_flows as flows_mod
    flows_mod._model_flow_bedrock_api_key({}, "us-east-1")

    assert saved_cfg["model"]["provider"] == "bedrock"
    assert (
        saved_cfg["model"]["base_url"]
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    assert saved_cfg["bedrock"]["region"] == "us-east-1"
    assert saved_cfg["bedrock"]["auth_method"] == "api_key"
    # Cline-parity toggles — must be persisted
    assert saved_cfg["bedrock"]["use_cross_region_inference"] is True
    assert saved_cfg["bedrock"]["use_global_inference_profile"] is True
    assert saved_cfg["bedrock"]["use_prompt_caching"] is True
    assert "OPENAI_API_KEY" not in saved_env
    assert "OPENAI_BASE_URL" not in saved_env



def test_bedrock_model_picker_dedupes_all_inference_profile_prefixes(monkeypatch):
    import agent.bedrock_adapter as bedrock_adapter
    import hermes_cli.auth as auth_mod
    import hermes_cli.main as main_mod

    seen_models: list[str] = []

    monkeypatch.setattr(
        bedrock_adapter,
        "discover_bedrock_models",
        lambda region, config=None: [
            {"id": "apac.anthropic.claude-haiku-4-5"},
            {"id": "anthropic.claude-haiku-4-5"},
            {"id": "au.anthropic.claude-sonnet-4-6"},
            {"id": "anthropic.claude-sonnet-4-6"},
        ],
    )

    def fake_prompt(models, current_model=""):
        seen_models.extend(models)
        return models[0]

    monkeypatch.setattr(auth_mod, "_prompt_model_selection", fake_prompt)

    # Upstream extracted _discover_bedrock_model_list out of hermes_cli.main
    # into hermes_cli.model_setup_flows; call the canonical location.
    import hermes_cli.model_setup_flows as flows_mod
    selected = flows_mod._discover_bedrock_model_list("us-east-1")

    assert selected == "apac.anthropic.claude-haiku-4-5"
    assert "apac.anthropic.claude-haiku-4-5" in seen_models
    assert "au.anthropic.claude-sonnet-4-6" in seen_models
    assert "anthropic.claude-haiku-4-5" not in seen_models
    assert "anthropic.claude-sonnet-4-6" not in seen_models


def test_bedrock_reasoning_choices_family_aware():
    """PR-2 Atom B: the Bedrock model flow offers family-aware reasoning levels.

    bedrock_reasoning_effort_choices(model) returns the GRADED levels (no 'none'
    — the picker has a separate Disable option) valid for that family, so the
    composite model->reasoning picker never offers an effort the model 400s on.
    """
    from hermes_cli.model_setup_flows import bedrock_reasoning_effort_choices

    # Opus 4.8 / Fable: low..xhigh..max (no none in the graded list, no minimal)
    opus = bedrock_reasoning_effort_choices("us.anthropic.claude-opus-4-8")
    assert "xhigh" in opus and "max" in opus
    assert "none" not in opus and "minimal" not in opus

    fable = bedrock_reasoning_effort_choices("us.anthropic.claude-fable-5")
    assert "xhigh" in fable and "max" in fable

    # Sonnet 4.6: no xhigh
    sonnet = bedrock_reasoning_effort_choices("global.anthropic.claude-sonnet-4-6")
    assert "xhigh" not in sonnet
    assert "high" in sonnet and "max" in sonnet

    # Haiku 4.5: no graded levels at all (none-only family) -> empty list
    haiku = bedrock_reasoning_effort_choices("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert haiku == [], "Haiku has no adaptive thinking — no graded effort choices"
