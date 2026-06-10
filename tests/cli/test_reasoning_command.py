"""Tests for the combined /reasoning command.

Covers both reasoning effort level management and reasoning display toggle,
plus the reasoning extraction and display pipeline from run_agent through CLI.

Combines functionality from:
- PR #789 (Aum08Desai): reasoning effort level management
- PR #790 (0xbyt4): reasoning display toggle and rendering
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import re


# ---------------------------------------------------------------------------
# Effort level parsing
# ---------------------------------------------------------------------------

class TestParseReasoningConfig(unittest.TestCase):
    """Verify _parse_reasoning_config handles all effort levels."""

    def _parse(self, effort):
        from cli import _parse_reasoning_config
        return _parse_reasoning_config(effort)

    def test_none_disables(self):
        result = self._parse("none")
        self.assertEqual(result, {"enabled": False})

    def test_valid_levels(self):
        for level in ("low", "medium", "high", "xhigh", "minimal"):
            result = self._parse(level)
            self.assertIsNotNone(result)
            self.assertTrue(result.get("enabled"))
            self.assertEqual(result["effort"], level)

    def test_empty_returns_none(self):
        self.assertIsNone(self._parse(""))
        self.assertIsNone(self._parse("  "))

    def test_unknown_returns_none(self):
        self.assertIsNone(self._parse("ultra"))
        self.assertIsNone(self._parse("turbo"))

    def test_case_insensitive(self):
        result = self._parse("HIGH")
        self.assertIsNotNone(result)
        self.assertEqual(result["effort"], "high")


# ---------------------------------------------------------------------------
# /reasoning command handler (combined effort + display)
# ---------------------------------------------------------------------------

class TestHandleReasoningCommand(unittest.TestCase):
    """Test the combined _handle_reasoning_command method."""

    def _make_cli(self, reasoning_config=None, show_reasoning=False):
        """Create a minimal CLI stub with the reasoning attributes."""
        stub = SimpleNamespace(
            reasoning_config=reasoning_config,
            show_reasoning=show_reasoning,
            agent=MagicMock(),
        )
        return stub

    def test_show_enables_display(self):
        stub = self._make_cli(show_reasoning=False)
        # Simulate /reasoning show
        arg = "show"
        if arg in {"show", "on"}:
            stub.show_reasoning = True
            stub.agent.reasoning_callback = lambda x: None
        self.assertTrue(stub.show_reasoning)

    def test_hide_disables_display(self):
        stub = self._make_cli(show_reasoning=True)
        # Simulate /reasoning hide
        arg = "hide"
        if arg in {"hide", "off"}:
            stub.show_reasoning = False
            stub.agent.reasoning_callback = None
        self.assertFalse(stub.show_reasoning)
        self.assertIsNone(stub.agent.reasoning_callback)

    def test_on_enables_display(self):
        stub = self._make_cli(show_reasoning=False)
        arg = "on"
        if arg in {"show", "on"}:
            stub.show_reasoning = True
        self.assertTrue(stub.show_reasoning)

    def test_off_disables_display(self):
        stub = self._make_cli(show_reasoning=True)
        arg = "off"
        if arg in {"hide", "off"}:
            stub.show_reasoning = False
        self.assertFalse(stub.show_reasoning)

    def test_effort_level_sets_config(self):
        """Setting an effort level should update reasoning_config."""
        from cli import _parse_reasoning_config
        stub = self._make_cli()
        arg = "high"
        parsed = _parse_reasoning_config(arg)
        stub.reasoning_config = parsed
        self.assertEqual(stub.reasoning_config, {"enabled": True, "effort": "high"})

    def test_effort_none_disables_reasoning(self):
        from cli import _parse_reasoning_config
        stub = self._make_cli()
        parsed = _parse_reasoning_config("none")
        stub.reasoning_config = parsed
        self.assertEqual(stub.reasoning_config, {"enabled": False})

    def test_invalid_argument_rejected(self):
        """Invalid arguments should be rejected (parsed returns None)."""
        from cli import _parse_reasoning_config
        parsed = _parse_reasoning_config("turbo")
        self.assertIsNone(parsed)

    def test_no_args_shows_status(self):
        """With no args, should show current state (no crash)."""
        stub = self._make_cli(reasoning_config=None, show_reasoning=False)
        rc = stub.reasoning_config
        if rc is None:
            level = "medium (default)"
        elif rc.get("enabled") is False:
            level = "none (disabled)"
        else:
            level = rc.get("effort", "medium")
        display_state = "on" if stub.show_reasoning else "off"
        self.assertEqual(level, "medium (default)")
        self.assertEqual(display_state, "off")

    def test_status_with_disabled_reasoning(self):
        stub = self._make_cli(reasoning_config={"enabled": False}, show_reasoning=True)
        rc = stub.reasoning_config
        if rc is None:
            level = "medium (default)"
        elif rc.get("enabled") is False:
            level = "none (disabled)"
        else:
            level = rc.get("effort", "medium")
        self.assertEqual(level, "none (disabled)")

    def test_status_with_explicit_level(self):
        stub = self._make_cli(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            show_reasoning=True,
        )
        rc = stub.reasoning_config
        level = rc.get("effort", "medium")
        self.assertEqual(level, "xhigh")


# ---------------------------------------------------------------------------
# Reasoning extraction and result dict
# ---------------------------------------------------------------------------

class TestLastReasoningInResult(unittest.TestCase):
    """Verify reasoning extraction from the messages list."""

    def _build_messages(self, reasoning=None):
        return [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "Hi there!",
                "reasoning": reasoning,
                "finish_reason": "stop",
            },
        ]

    def test_reasoning_present(self):
        messages = self._build_messages(reasoning="Let me think...")
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertEqual(last_reasoning, "Let me think...")

    def test_reasoning_none(self):
        messages = self._build_messages(reasoning=None)
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertIsNone(last_reasoning)

    def test_picks_last_assistant(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "...", "reasoning": "first thought"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "done!", "reasoning": "final thought"},
        ]
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertEqual(last_reasoning, "final thought")

    def test_empty_reasoning_treated_as_none(self):
        messages = self._build_messages(reasoning="")
        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break
        self.assertIsNone(last_reasoning)


# ---------------------------------------------------------------------------
# Reasoning display collapse
# ---------------------------------------------------------------------------

class TestReasoningCollapse(unittest.TestCase):
    """Verify long reasoning is collapsed to 10 lines in the box."""

    def test_short_reasoning_not_collapsed(self):
        reasoning = "\n".join(f"Line {i}" for i in range(5))
        lines = reasoning.strip().splitlines()
        self.assertLessEqual(len(lines), 10)

    def test_long_reasoning_collapsed(self):
        reasoning = "\n".join(f"Line {i}" for i in range(25))
        lines = reasoning.strip().splitlines()
        self.assertTrue(len(lines) > 10)
        if len(lines) > 10:
            display = "\n".join(lines[:10])
            display += f"\n  ... ({len(lines) - 10} more lines)"
        display_lines = display.splitlines()
        self.assertEqual(len(display_lines), 11)
        self.assertIn("15 more lines", display_lines[-1])

    def test_exactly_10_lines_not_collapsed(self):
        reasoning = "\n".join(f"Line {i}" for i in range(10))
        lines = reasoning.strip().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertFalse(len(lines) > 10)

    def test_intermediate_callback_collapses_to_5(self):
        """_on_reasoning shows max 5 lines."""
        reasoning = "\n".join(f"Step {i}" for i in range(12))
        lines = reasoning.strip().splitlines()
        if len(lines) > 5:
            preview = "\n".join(lines[:5])
            preview += f"\n  ... ({len(lines) - 5} more lines)"
        else:
            preview = reasoning.strip()
        preview_lines = preview.splitlines()
        self.assertEqual(len(preview_lines), 6)
        self.assertIn("7 more lines", preview_lines[-1])


# ---------------------------------------------------------------------------
# Reasoning callback
# ---------------------------------------------------------------------------

class TestReasoningCallback(unittest.TestCase):
    """Verify reasoning_callback invocation."""

    def test_callback_invoked_with_reasoning(self):
        captured = []
        agent = MagicMock()
        agent.reasoning_callback = lambda t: captured.append(t)
        agent._extract_reasoning = MagicMock(return_value="deep thought")

        reasoning_text = agent._extract_reasoning(MagicMock())
        if reasoning_text and agent.reasoning_callback:
            agent.reasoning_callback(reasoning_text)
        self.assertEqual(captured, ["deep thought"])

    def test_callback_not_invoked_without_reasoning(self):
        captured = []
        agent = MagicMock()
        agent.reasoning_callback = lambda t: captured.append(t)
        agent._extract_reasoning = MagicMock(return_value=None)

        reasoning_text = agent._extract_reasoning(MagicMock())
        if reasoning_text and agent.reasoning_callback:
            agent.reasoning_callback(reasoning_text)
        self.assertEqual(captured, [])

    def test_callback_none_does_not_crash(self):
        reasoning_text = "some thought"
        callback = None
        if reasoning_text and callback:
            callback(reasoning_text)
        # No exception = pass


class TestReasoningPreviewBuffering(unittest.TestCase):
    def _make_cli(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.verbose = True
        cli._spinner_text = ""
        cli._reasoning_preview_buf = ""
        cli._invalidate = lambda *args, **kwargs: None
        return cli

    @patch("cli._cprint")
    def test_streamed_reasoning_chunks_wait_for_boundary(self, mock_cprint):
        cli = self._make_cli()

        cli._on_reasoning("Let")
        cli._on_reasoning(" me")
        cli._on_reasoning(" think")

        self.assertEqual(mock_cprint.call_count, 0)

        cli._on_reasoning(" about this.\n")

        self.assertEqual(mock_cprint.call_count, 1)
        rendered = mock_cprint.call_args[0][0]
        self.assertIn("[thinking] Let me think about this.", rendered)

    @patch("cli._cprint")
    def test_pending_reasoning_flushes_when_thinking_stops(self, mock_cprint):
        cli = self._make_cli()

        cli._on_reasoning("see")
        cli._on_reasoning(" how")
        cli._on_reasoning(" this")
        cli._on_reasoning(" plays")
        cli._on_reasoning(" out")

        self.assertEqual(mock_cprint.call_count, 0)

        cli._on_thinking("")

        self.assertEqual(mock_cprint.call_count, 1)
        rendered = mock_cprint.call_args[0][0]
        self.assertIn("[thinking] see how this plays out", rendered)

    @patch("cli._cprint")
    @patch("cli.shutil.get_terminal_size", return_value=SimpleNamespace(columns=50))
    def test_reasoning_preview_compacts_newlines_and_wraps_to_terminal(self, _mock_term, mock_cprint):
        cli = self._make_cli()

        cli._emit_reasoning_preview(
            "First line\nstill same thought\n\n\nSecond paragraph with more detail here."
        )

        rendered = mock_cprint.call_args[0][0]
        plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)
        normalized = " ".join(plain.split())
        self.assertIn("[thinking] First line still same thought", plain)
        self.assertIn("Second paragraph with more detail here.", normalized)
        self.assertNotIn("\n\n\n", plain)

    @patch("cli.shutil.get_terminal_size", return_value=SimpleNamespace(columns=60))
    def test_reasoning_flush_threshold_tracks_terminal_width(self, _mock_term):
        cli = self._make_cli()

        cli._reasoning_preview_buf = "a" * 30
        cli._flush_reasoning_preview(force=False)
        self.assertEqual(cli._reasoning_preview_buf, "a" * 30)


class TestReasoningDisplayModeSelection(unittest.TestCase):
    def _make_cli(self, *, show_reasoning=False, streaming_enabled=False, verbose=False):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli.show_reasoning = show_reasoning
        cli.streaming_enabled = streaming_enabled
        cli.verbose = verbose
        cli._stream_reasoning_delta = lambda text: ("stream", text)
        cli._on_reasoning = lambda text: ("preview", text)
        return cli

    def test_show_reasoning_non_streaming_uses_final_box_only(self):
        cli = self._make_cli(show_reasoning=True, streaming_enabled=False, verbose=False)

        self.assertIsNone(cli._current_reasoning_callback())

    def test_show_reasoning_streaming_uses_live_reasoning_box(self):
        cli = self._make_cli(show_reasoning=True, streaming_enabled=True, verbose=False)

        callback = cli._current_reasoning_callback()
        self.assertIsNotNone(callback)
        self.assertEqual(callback("x"), ("stream", "x"))

    def test_verbose_without_show_reasoning_uses_preview_callback(self):
        cli = self._make_cli(show_reasoning=False, streaming_enabled=False, verbose=True)

        callback = cli._current_reasoning_callback()
        self.assertIsNotNone(callback)
        self.assertEqual(callback("x"), ("preview", "x"))


# ---------------------------------------------------------------------------
# Real provider format extraction
# ---------------------------------------------------------------------------

class TestExtractReasoningFormats(unittest.TestCase):
    """Test _extract_reasoning with real provider response formats."""

    def _get_extractor(self):
        from run_agent import AIAgent
        return AIAgent._extract_reasoning

    def test_openrouter_reasoning_details(self):
        extract = self._get_extractor()
        msg = SimpleNamespace(
            reasoning=None,
            reasoning_content=None,
            reasoning_details=[
                {"type": "reasoning.summary", "summary": "Analyzing Python lists."},
            ],
        )
        result = extract(None, msg)
        self.assertIn("Python lists", result)

    def test_deepseek_reasoning_field(self):
        extract = self._get_extractor()
        msg = SimpleNamespace(
            reasoning="Solving step by step.\nx + y = 8.",
            reasoning_content=None,
        )
        result = extract(None, msg)
        self.assertIn("x + y = 8", result)

    def test_moonshot_reasoning_content(self):
        extract = self._get_extractor()
        msg = SimpleNamespace(
            reasoning_content="Explaining async/await.",
        )
        result = extract(None, msg)
        self.assertIn("async/await", result)

    def test_no_reasoning_returns_none(self):
        extract = self._get_extractor()
        msg = SimpleNamespace(content="Hello!")
        result = extract(None, msg)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Inline <think> block extraction fallback
# ---------------------------------------------------------------------------

class TestInlineThinkBlockExtraction(unittest.TestCase):
    """Test _build_assistant_message extracts inline <think> blocks as reasoning
    when no structured API-level reasoning fields are present."""

    def _build_msg(self, content, reasoning=None, reasoning_content=None, reasoning_details=None, tool_calls=None):
        """Create a mock API response message."""
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        if reasoning is not None:
            msg.reasoning = reasoning
        if reasoning_content is not None:
            msg.reasoning_content = reasoning_content
        if reasoning_details is not None:
            msg.reasoning_details = reasoning_details
        return msg

    def _make_agent(self):
        """Create a minimal agent with _build_assistant_message."""
        from run_agent import AIAgent
        agent = MagicMock(spec=AIAgent)
        agent._build_assistant_message = AIAgent._build_assistant_message.__get__(agent)
        agent._extract_reasoning = AIAgent._extract_reasoning.__get__(agent)
        agent.verbose_logging = False
        agent.reasoning_callback = None
        agent.stream_delta_callback = None  # non-streaming by default
        agent._stream_callback = None  # non-streaming by default
        return agent

    def test_single_think_block_extracted(self):
        agent = self._make_agent()
        api_msg = self._build_msg("<think>Let me calculate 2+2=4.</think>The answer is 4.")
        result = agent._build_assistant_message(api_msg, "stop")
        self.assertEqual(result["reasoning"], "Let me calculate 2+2=4.")

    def test_multiple_think_blocks_extracted(self):
        agent = self._make_agent()
        api_msg = self._build_msg("<think>First thought.</think>Some text<think>Second thought.</think>More text")
        result = agent._build_assistant_message(api_msg, "stop")
        self.assertIn("First thought.", result["reasoning"])
        self.assertIn("Second thought.", result["reasoning"])

    def test_no_think_blocks_no_reasoning(self):
        agent = self._make_agent()
        api_msg = self._build_msg("Just a plain response.")
        result = agent._build_assistant_message(api_msg, "stop")
        # No structured reasoning AND no inline think blocks → None
        self.assertIsNone(result["reasoning"])

    def test_structured_reasoning_takes_priority(self):
        """When structured API reasoning exists, inline think blocks should NOT override."""
        agent = self._make_agent()
        api_msg = self._build_msg(
            "<think>Inline thought.</think>Response text.",
            reasoning="Structured reasoning from API.",
        )
        result = agent._build_assistant_message(api_msg, "stop")
        self.assertEqual(result["reasoning"], "Structured reasoning from API.")

    def test_empty_think_block_ignored(self):
        agent = self._make_agent()
        api_msg = self._build_msg("<think></think>Hello!")
        result = agent._build_assistant_message(api_msg, "stop")
        # Empty think block should not produce reasoning
        self.assertIsNone(result["reasoning"])

    def test_multiline_think_block(self):
        agent = self._make_agent()
        api_msg = self._build_msg("<think>\nStep 1: Analyze.\nStep 2: Solve.\n</think>Done.")
        result = agent._build_assistant_message(api_msg, "stop")
        self.assertIn("Step 1: Analyze.", result["reasoning"])
        self.assertIn("Step 2: Solve.", result["reasoning"])

    def test_callback_fires_for_inline_think(self):
        """Reasoning callback should fire when reasoning is extracted from inline think blocks."""
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        api_msg = self._build_msg("<think>Deep analysis here.</think>Answer.")
        agent._build_assistant_message(api_msg, "stop")
        self.assertEqual(len(captured), 1)
        self.assertIn("Deep analysis", captured[0])


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestConfigDefault(unittest.TestCase):
    """Verify config default for show_reasoning."""

    def test_default_config_has_show_reasoning(self):
        from hermes_cli.config import DEFAULT_CONFIG
        display = DEFAULT_CONFIG.get("display", {})
        self.assertIn("show_reasoning", display)
        self.assertFalse(display["show_reasoning"])


class TestCommandRegistered(unittest.TestCase):
    """Verify /reasoning is in the COMMANDS dict."""

    def test_reasoning_in_commands(self):
        from hermes_cli.commands import COMMANDS
        self.assertIn("/reasoning", COMMANDS)


# ---------------------------------------------------------------------------
# End-to-end pipeline
# ---------------------------------------------------------------------------

class TestEndToEndPipeline(unittest.TestCase):
    """Simulate the full pipeline: extraction -> result dict -> display."""

    def test_openrouter_claude_pipeline(self):
        from run_agent import AIAgent

        api_message = SimpleNamespace(
            role="assistant",
            content="Lists support append().",
            tool_calls=None,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=[
                {"type": "reasoning.summary", "summary": "Python list methods."},
            ],
        )

        reasoning = AIAgent._extract_reasoning(None, api_message)
        self.assertIsNotNone(reasoning)

        messages = [
            {"role": "user", "content": "How do I add items?"},
            {"role": "assistant", "content": api_message.content, "reasoning": reasoning},
        ]

        last_reasoning = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("reasoning"):
                last_reasoning = msg["reasoning"]
                break

        result = {
            "final_response": api_message.content,
            "last_reasoning": last_reasoning,
        }

        self.assertIn("last_reasoning", result)
        self.assertIn("Python list methods", result["last_reasoning"])

    def test_no_reasoning_model_pipeline(self):
        from run_agent import AIAgent

        api_message = SimpleNamespace(content="Paris.", tool_calls=None)
        reasoning = AIAgent._extract_reasoning(None, api_message)
        self.assertIsNone(reasoning)

        result = {"final_response": api_message.content, "last_reasoning": reasoning}
        self.assertIsNone(result["last_reasoning"])


# ---------------------------------------------------------------------------
# Duplicate reasoning box prevention (Bug fix: 3 boxes for 1 reasoning)
# ---------------------------------------------------------------------------

class TestReasoningDeltasFiredFlag(unittest.TestCase):
    """_build_assistant_message should not re-fire reasoning_callback when
    reasoning was already streamed via _fire_reasoning_delta."""

    def _make_agent(self):
        from run_agent import AIAgent
        agent = AIAgent.__new__(AIAgent)
        agent.reasoning_callback = None
        agent.stream_delta_callback = None
        agent._stream_callback = None
        agent.verbose_logging = False
        return agent

    def test_fire_reasoning_delta_calls_callback(self):
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        agent._fire_reasoning_delta("thinking...")
        self.assertEqual(captured, ["thinking..."])

    def test_build_assistant_message_skips_callback_when_already_streamed(self):
        """When streaming already fired reasoning deltas, the post-stream
        _build_assistant_message should NOT re-fire the callback."""
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        agent.stream_delta_callback = lambda t: None  # streaming is active

        # Simulate streaming having already fired reasoning

        msg = SimpleNamespace(
            content="I'll merge that.",
            tool_calls=None,
            reasoning_content="Let me merge the PR.",
            reasoning=None,
            reasoning_details=None,
        )
        agent._build_assistant_message(msg, "stop")

        # Callback should NOT have been fired again
        self.assertEqual(captured, [])

    def test_build_assistant_message_skips_callback_when_streaming_active(self):
        """When streaming is active, callback should NEVER fire from
        _build_assistant_message — reasoning was already displayed during the
        stream (either via reasoning_content deltas or content tag extraction).
        Any missed reasoning is caught by the CLI post-response fallback."""
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        agent.stream_delta_callback = lambda t: None  # streaming active

        # Reasoning came through content tags, not reasoning_content deltas.
        # Callback should not fire since streaming is active.

        msg = SimpleNamespace(
            content="I'll merge that.",
            tool_calls=None,
            reasoning_content="Let me merge the PR.",
            reasoning=None,
            reasoning_details=None,
        )
        agent._build_assistant_message(msg, "stop")

        # Callback should NOT fire — streaming is active
        self.assertEqual(captured, [])

    def test_build_assistant_message_fires_callback_without_streaming(self):
        """When no streaming is active, callback always fires for structured
        reasoning."""
        agent = self._make_agent()
        captured = []
        agent.reasoning_callback = lambda t: captured.append(t)
        # No streaming
        agent.stream_delta_callback = None

        msg = SimpleNamespace(
            content="I'll merge that.",
            tool_calls=None,
            reasoning_content="Let me merge the PR.",
            reasoning=None,
            reasoning_details=None,
        )
        agent._build_assistant_message(msg, "stop")

        self.assertEqual(captured, ["Let me merge the PR."])


class TestReasoningShownThisTurnFlag(unittest.TestCase):
    """Post-response reasoning display should be suppressed when reasoning
    was already shown during streaming in a tool-calling loop."""

    def _make_cli(self):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.show_reasoning = True
        cli.streaming_enabled = True
        cli._stream_box_opened = False
        cli._reasoning_box_opened = False
        cli._reasoning_stream_started = False
        cli._reasoning_shown_this_turn = False
        cli._reasoning_buf = ""
        cli._stream_buf = ""
        cli._stream_started = False
        cli._stream_text_ansi = ""
        cli._stream_prefilt = ""
        cli._in_reasoning_block = False
        cli._reasoning_preview_buf = ""
        return cli

    @patch("cli._cprint")
    def test_streaming_reasoning_sets_turn_flag(self, mock_cprint):
        cli = self._make_cli()
        self.assertFalse(cli._reasoning_shown_this_turn)
        cli._stream_reasoning_delta("Thinking about it...")
        self.assertTrue(cli._reasoning_shown_this_turn)

    @patch("cli._cprint")
    def test_turn_flag_survives_reset_stream_state(self, mock_cprint):
        """_reasoning_shown_this_turn must NOT be cleared by
        _reset_stream_state (called at intermediate turn boundaries)."""
        cli = self._make_cli()
        cli._stream_reasoning_delta("Thinking...")
        self.assertTrue(cli._reasoning_shown_this_turn)

        # Simulate intermediate turn boundary (tool call)
        cli._reset_stream_state()

        # Flag must persist
        self.assertTrue(cli._reasoning_shown_this_turn)

    @patch("cli._cprint")
    def test_turn_flag_cleared_before_new_turn(self, mock_cprint):
        """The turn flag should be reset at the start of a new user turn.
        This happens outside _reset_stream_state, at the call site."""
        cli = self._make_cli()
        cli._reasoning_shown_this_turn = True

        # Simulate new user turn setup
        cli._reset_stream_state()
        cli._reasoning_shown_this_turn = False  # done by process_input

        self.assertFalse(cli._reasoning_shown_this_turn)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Family-aware /reasoning validation (PR-2 Atom A, 2026-06-10)
# ---------------------------------------------------------------------------

class TestReasoningFamilyAwareValidation(unittest.TestCase):
    """The /reasoning handler must reject an effort the ACTIVE model would 400 on.

    Exercises the REAL _handle_reasoning_command bound onto a stub, with
    cli._cprint and cli.save_config_value patched, asserting that:
      - xhigh on Sonnet 4.6 is rejected (not applied)
      - any thinking effort on Haiku 4.5 is rejected
      - 'minimal' is rejected on the Bedrock adaptive path
      - valid effort on Opus 4.8 / Fable 5 is accepted (config set)
      - non-Claude models are unconstrained (no regression)
    """

    def _make_cli(self, model):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli.model = model
        cli.provider = "bedrock"
        cli.reasoning_config = None
        cli.show_reasoning = False
        cli.agent = MagicMock()
        return cli

    def _run(self, model, arg):
        """Run /reasoning <arg> against the real handler; return (applied, output)."""
        cli = self._make_cli(model)
        captured = []
        with patch("cli._cprint", side_effect=lambda *a, **k: captured.append(a[0] if a else "")), \
             patch("cli.save_config_value", return_value=False):
            cli._handle_reasoning_command(f"/reasoning {arg}")
        applied = cli.reasoning_config is not None
        return applied, "\n".join(str(c) for c in captured)

    def test_xhigh_rejected_on_sonnet(self):
        applied, out = self._run("global.anthropic.claude-sonnet-4-6", "xhigh")
        self.assertFalse(applied, "xhigh must NOT be applied on Sonnet 4.6")
        self.assertIn("not supported", out.lower())

    def test_high_rejected_on_haiku(self):
        applied, out = self._run("us.anthropic.claude-haiku-4-5-20251001-v1:0", "high")
        self.assertFalse(applied, "thinking must NOT be applied on Haiku 4.5")
        self.assertIn("none", out.lower())

    def test_minimal_coerced_not_rejected_on_opus(self):
        # Council C5: 'minimal' is a legacy alias (ADAPTIVE_EFFORT_MAP minimal->low).
        # It must be ACCEPTED (coerced to low), NOT rejected — rejecting it would
        # regress existing configs/scripts. (Superseded the original reject assertion.)
        applied, out = self._run("us.anthropic.claude-opus-4-8", "minimal")
        self.assertTrue(applied, "'minimal' must be accepted (coerced to low), not rejected")
        self.assertNotIn("not supported", out.lower())

    def test_xhigh_accepted_on_opus(self):
        applied, _ = self._run("us.anthropic.claude-opus-4-8", "xhigh")
        self.assertTrue(applied, "xhigh must be accepted on Opus 4.8")

    def test_high_accepted_on_fable(self):
        applied, _ = self._run("us.anthropic.claude-fable-5", "high")
        self.assertTrue(applied, "high must be accepted on Fable 5")

    def test_none_accepted_on_haiku(self):
        applied, _ = self._run("us.anthropic.claude-haiku-4-5-20251001-v1:0", "none")
        self.assertTrue(applied, "none must be accepted on Haiku 4.5")

    def test_non_claude_unconstrained(self):
        # gpt model: valid_efforts_for_model returns None -> no family gate
        applied, _ = self._run("gpt-5.5", "xhigh")
        self.assertTrue(applied, "non-Claude models must not be constrained by the family gate")


class TestReasoningMinimalAliasCoercion(unittest.TestCase):
    """PR-2 council C5 fix: 'minimal' must remain accepted (coerced to 'low')
    on Bedrock-Claude, not rejected by the new family-aware guard.

    Before PR-2, /reasoning minimal parsed OK and the wire mapped minimal->low.
    The family guard must normalize the legacy 'minimal' alias to 'low' BEFORE
    validating, so existing configs / muscle memory / scripts don't break.
    """

    def _make_cli(self, model):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli.model = model
        cli.provider = "bedrock"
        cli.reasoning_config = None
        cli.show_reasoning = False
        cli.agent = MagicMock()
        return cli

    def _run(self, model, arg):
        cli = self._make_cli(model)
        captured = []
        with patch("cli._cprint", side_effect=lambda *a, **k: captured.append(a[0] if a else "")), \
             patch("cli.save_config_value", return_value=False):
            cli._handle_reasoning_command(f"/reasoning {arg}")
        return cli.reasoning_config, "\n".join(str(c) for c in captured)

    def test_minimal_coerced_to_low_on_opus(self):
        rc, out = self._run("us.anthropic.claude-opus-4-8", "minimal")
        self.assertIsNotNone(rc, "minimal must NOT be rejected (legacy alias)")
        self.assertEqual(rc.get("effort"), "low", "minimal must coerce to low")
        self.assertNotIn("not supported", out.lower())

    def test_minimal_coerced_to_low_on_sonnet(self):
        rc, _ = self._run("global.anthropic.claude-sonnet-4-6", "minimal")
        self.assertIsNotNone(rc)
        self.assertEqual(rc.get("effort"), "low")

    def test_minimal_rejected_on_haiku(self):
        # Haiku only supports 'none'. minimal->low is still invalid -> reject.
        rc, out = self._run("us.anthropic.claude-haiku-4-5-20251001-v1:0", "minimal")
        self.assertIsNone(rc, "minimal->low is still unsupported on Haiku")
        self.assertIn("none", out.lower())


class TestReasoningEmptyModelEarlyExit(unittest.TestCase):
    """PR-2 council C3 fix: guard must not misfire when no model is set yet."""

    def _make_cli(self, model):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = CLICommandsMixin.__new__(CLICommandsMixin)
        cli.model = model
        cli.provider = ""
        cli.reasoning_config = None
        cli.show_reasoning = False
        cli.agent = MagicMock()
        return cli

    def test_empty_model_accepts_any_effort(self):
        # No active model -> unconstrained (wire enforces). Must not crash/reject.
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = self._make_cli("")
        with patch("cli._cprint", side_effect=lambda *a, **k: None), \
             patch("cli.save_config_value", return_value=False):
            cli._handle_reasoning_command("/reasoning xhigh")
        self.assertIsNotNone(cli.reasoning_config, "empty model must not block effort set")
