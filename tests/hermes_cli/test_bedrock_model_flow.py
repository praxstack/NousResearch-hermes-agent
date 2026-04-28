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

    main_mod._model_flow_bedrock_api_key({}, "us-east-1")

    assert saved_cfg["model"]["provider"] == "bedrock"
    assert (
        saved_cfg["model"]["base_url"]
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    assert saved_cfg["bedrock"]["region"] == "us-east-1"
    assert saved_cfg["bedrock"]["auth_method"] == "api_key"
    assert "OPENAI_API_KEY" not in saved_env
    assert "OPENAI_BASE_URL" not in saved_env


def test_repair_legacy_bedrock_custom_config_rewrites_native_provider(monkeypatch):
    import hermes_cli.config as config_mod

    saved_cfg: dict[str, object] = {}
    saved_env: dict[str, str] = {}
    initial_cfg = {
        "model": {
            "provider": "custom",
            "base_url": "https://bedrock-mantle.us-east-1.api.aws/v1",
            "default": "anthropic.claude-opus-4-7:1m",
        },
        "bedrock": {},
    }

    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod,
        "save_config",
        lambda cfg: saved_cfg.update(deepcopy(cfg)),
    )
    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: {
            "AWS_BEARER_TOKEN_BEDROCK": "absk-test-token",
            "OPENAI_BASE_URL": "https://bedrock-mantle.us-east-1.api.aws/v1",
            "OPENAI_API_KEY": "absk-test-token",
        }.get(key, ""),
    )
    monkeypatch.setattr(
        config_mod,
        "save_env_value",
        lambda key, value: saved_env.__setitem__(key, value),
    )

    repaired = config_mod.repair_legacy_bedrock_custom_config()

    assert repaired is True
    assert saved_cfg["model"]["provider"] == "bedrock"
    assert (
        saved_cfg["model"]["base_url"]
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    assert saved_cfg["bedrock"]["region"] == "us-east-1"
    assert saved_cfg["bedrock"]["auth_method"] == "api_key"
    assert saved_env["OPENAI_BASE_URL"] == ""
    assert saved_env["OPENAI_API_KEY"] == ""


def test_repair_legacy_bedrock_native_proxy_config(monkeypatch):
    import hermes_cli.config as config_mod

    saved_cfg: dict[str, object] = {}
    saved_env: dict[str, str] = {}
    initial_cfg = {
        "model": {
            "provider": "bedrock-native",
            "base_url": "http://localhost:8881",
            "default": "claude-opus-4.7",
            "api_mode": "anthropic_messages",
        },
        "bedrock": {"region": "us-east-1", "auth_method": "default_chain"},
        "providers": {
            "bedrock-native": {
                "api": "http://localhost:8881",
                "name": "bedrock-native",
            },
            "bedrock-mantle": {
                "api": "https://bedrock-mantle.us-east-1.api.aws/v1",
                "name": "bedrock-mantle",
            },
        },
    }

    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod,
        "save_config",
        lambda cfg: saved_cfg.update(deepcopy(cfg)),
    )
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

    repaired = config_mod.repair_legacy_bedrock_custom_config()

    assert repaired is True
    assert saved_cfg["model"]["provider"] == "bedrock"
    assert saved_cfg["model"]["default"] == "global.anthropic.claude-opus-4-7"
    assert (
        saved_cfg["model"]["base_url"]
        == "https://bedrock-runtime.us-east-1.amazonaws.com"
    )
    assert "api_mode" not in saved_cfg["model"]
    assert saved_cfg["bedrock"]["auth_method"] == "api_key"
    assert "bedrock-native" not in saved_cfg["providers"]
    assert "bedrock-mantle" not in saved_cfg["providers"]


def test_bedrock_setup_prompts_before_legacy_repair(monkeypatch):
    import builtins
    import hermes_cli.config as config_mod
    import hermes_cli.main as main_mod

    saved_cfg: dict[str, object] = {}
    saved_env: dict[str, str] = {}
    prompts: list[str] = []
    initial_cfg = {
        "model": {
            "provider": "custom",
            "base_url": "https://bedrock-mantle.us-east-1.api.aws/v1",
            "default": "anthropic.claude-opus-4-7:1m",
        },
        "bedrock": {},
    }

    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(initial_cfg))
    monkeypatch.setattr(
        config_mod,
        "save_config",
        lambda cfg: saved_cfg.update(deepcopy(cfg)),
    )
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

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr(builtins, "input", fake_input)

    assert main_mod._repair_legacy_bedrock_custom_config_interactive() is False
    assert any("Repair existing Bedrock API-key setup" in prompt for prompt in prompts)
    assert saved_cfg == {}
    assert saved_env == {}


def test_runtime_rejects_legacy_bedrock_mantle_custom_config(monkeypatch):
    import pytest
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as runtime_provider
    from hermes_cli.auth import AuthError

    legacy_cfg = {
        "model": {
            "provider": "custom",
            "base_url": "https://bedrock-mantle.us-east-1.api.aws/v1",
            "default": "anthropic.claude-opus-4-7:1m",
        },
        "bedrock": {},
    }

    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(legacy_cfg))
    monkeypatch.setattr(runtime_provider, "load_config", lambda: deepcopy(legacy_cfg))
    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: "absk-test-token" if key == "AWS_BEARER_TOKEN_BEDROCK" else "",
    )

    with pytest.raises(AuthError, match="legacy Bedrock API-key setup"):
        runtime_provider.resolve_runtime_provider()


def test_runtime_rejects_legacy_bedrock_native_proxy_config(monkeypatch):
    import pytest
    import hermes_cli.config as config_mod
    import hermes_cli.runtime_provider as runtime_provider
    from hermes_cli.auth import AuthError

    legacy_cfg = {
        "model": {
            "provider": "bedrock-native",
            "base_url": "http://localhost:8881",
            "default": "claude-opus-4.7",
        },
        "bedrock": {"region": "us-east-1"},
    }

    monkeypatch.setattr(config_mod, "load_config", lambda: deepcopy(legacy_cfg))
    monkeypatch.setattr(runtime_provider, "load_config", lambda: deepcopy(legacy_cfg))
    monkeypatch.setattr(
        config_mod,
        "get_env_value",
        lambda key: "absk-test-token" if key == "AWS_BEARER_TOKEN_BEDROCK" else "",
    )

    with pytest.raises(AuthError, match="legacy Bedrock API-key setup"):
        runtime_provider.resolve_runtime_provider()


def test_doctor_flags_legacy_bedrock_custom_config(monkeypatch):
    import hermes_cli.config as config_mod
    import hermes_cli.doctor as doctor_mod

    warnings: list[str] = []
    issues: list[str] = []

    monkeypatch.setattr(config_mod, "is_legacy_bedrock_custom_config", lambda: True)
    monkeypatch.setattr(doctor_mod, "check_warn", lambda text, detail="": warnings.append(text))

    fixed = doctor_mod._check_legacy_bedrock_custom_config(False, issues)

    assert fixed == 0
    assert any("Legacy Bedrock API-key config" in warning for warning in warnings)
    assert any("hermes doctor --fix" in issue for issue in issues)


def test_doctor_fixes_legacy_bedrock_custom_config(monkeypatch):
    import hermes_cli.config as config_mod
    import hermes_cli.doctor as doctor_mod

    oks: list[str] = []
    issues: list[str] = []

    monkeypatch.setattr(config_mod, "is_legacy_bedrock_custom_config", lambda: True)
    monkeypatch.setattr(config_mod, "repair_legacy_bedrock_custom_config", lambda: True)
    monkeypatch.setattr(doctor_mod, "check_warn", lambda text, detail="": None)
    monkeypatch.setattr(doctor_mod, "check_ok", lambda text, detail="": oks.append(text))

    fixed = doctor_mod._check_legacy_bedrock_custom_config(True, issues)

    assert fixed == 1
    assert issues == []
    assert "Repaired Bedrock API-key config to native provider" in oks


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
