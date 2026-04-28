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

    main_mod._model_flow_bedrock_api_key({}, "us-east-1")

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

    selected = main_mod._discover_bedrock_model_list("us-east-1")

    assert selected == "apac.anthropic.claude-haiku-4-5"
    assert "apac.anthropic.claude-haiku-4-5" in seen_models
    assert "au.anthropic.claude-sonnet-4-6" in seen_models
    assert "anthropic.claude-haiku-4-5" not in seen_models
    assert "anthropic.claude-sonnet-4-6" not in seen_models
