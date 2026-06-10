"""Tests for the current-model highlight resolver in the /model picker.

Incident (2026-06-11): on the provider->model stage transition the picker
hardcoded ``state["selected"] = 0``, so Enter landed on whatever sat at index
0 (a Nova model) regardless of what the user was already running.

Fix (council Q3, 2026-06-11): default the model-stage highlight to the index
of the CURRENT model so Enter on an unchanged selection is a no-op. When the
current model is not present in the list (region pulled / id renamed / revoked
mid-session), fall back to index 0 — which is now safe because ordering puts a
Claude Opus there.

The resolver is a pure function in hermes_cli.model_switch so it is testable
without a TTY (mirrors the _compute_scroll_offset pattern in
tests/test_model_picker_scroll.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestResolveCurrentModelIndex:
    def test_exact_match_returns_its_index(self):
        from hermes_cli.model_switch import resolve_current_model_index
        models = [
            "us.anthropic.claude-opus-4-8:1m",
            "us.anthropic.claude-fable-5",
            "global.amazon.nova-2-lite-v1:0",
        ]
        assert resolve_current_model_index(models, "us.anthropic.claude-fable-5") == 1

    def test_current_at_index_0_returns_0(self):
        from hermes_cli.model_switch import resolve_current_model_index
        models = ["us.anthropic.claude-opus-4-8:1m", "us.anthropic.claude-fable-5"]
        assert resolve_current_model_index(models, "us.anthropic.claude-opus-4-8:1m") == 0

    def test_absent_current_falls_back_to_0(self):
        # Region pulled / id renamed / revoked mid-session -> safe index 0.
        from hermes_cli.model_switch import resolve_current_model_index
        models = ["us.anthropic.claude-opus-4-8:1m", "us.anthropic.claude-fable-5"]
        assert resolve_current_model_index(models, "openai.gpt-9-ultra") == 0

    def test_empty_current_falls_back_to_0(self):
        from hermes_cli.model_switch import resolve_current_model_index
        models = ["us.anthropic.claude-opus-4-8:1m"]
        assert resolve_current_model_index(models, "") == 0
        assert resolve_current_model_index(models, None) == 0

    def test_empty_list_returns_0(self):
        from hermes_cli.model_switch import resolve_current_model_index
        assert resolve_current_model_index([], "us.anthropic.claude-opus-4-8:1m") == 0

    def test_match_is_case_insensitive_and_strips(self):
        # Picker model list and self.model may differ only in case / whitespace.
        from hermes_cli.model_switch import resolve_current_model_index
        models = ["US.Anthropic.Claude-Opus-4-8:1m", "us.anthropic.claude-fable-5"]
        assert resolve_current_model_index(models, "  us.anthropic.claude-opus-4-8:1m  ") == 0

    def test_first_match_wins_on_duplicate(self):
        from hermes_cli.model_switch import resolve_current_model_index
        models = ["a", "us.anthropic.claude-opus-4-8", "us.anthropic.claude-opus-4-8"]
        assert resolve_current_model_index(models, "us.anthropic.claude-opus-4-8") == 1
