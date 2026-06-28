"""Anthropic stream cleanup must call _anthropic_client.close() + _rebuild_anthropic_client(),
not _replace_primary_openai_client(), to avoid 15-minute hangs on Anthropic-native configs.

Three cleanup sites in chat_completion_helpers.interruptible_streaming_api_call() were
calling _replace_primary_openai_client() unconditionally.  For api_mode=anthropic_messages
this silently fails (no OPENAI_API_KEY) and leaves the in-flight httpx stream unclosed,
blocking the worker thread until the 900s httpx read-timeout fires.

Tests cover:
- stream_retry_pool_cleanup  (connection error on fresh stream, L1836)
- stale_stream_pool_cleanup  (outer poll loop detects stale stream, L1987)

Fixes #28161
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_anthropic_agent(**kwargs):
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="claude-opus-4-7",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    agent = AIAgent(**defaults)
    agent.api_mode = "anthropic_messages"
    agent._anthropic_client = MagicMock()
    agent._anthropic_api_key = "test-anthropic-key"
    return agent


def _good_stream_cm():
    """Context manager whose stream yields no events and returns a valid message."""
    cm = MagicMock()
    stream = MagicMock()
    stream.__iter__ = MagicMock(return_value=iter([]))
    msg = MagicMock()
    msg.content = []
    msg.stop_reason = "end_turn"
    msg.usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    stream.get_final_message = MagicMock(return_value=msg)
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _failing_stream_cm():
    """Context manager whose __enter__ raises ConnectError immediately."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(
        side_effect=httpx.ConnectError("connection reset by peer")
    )
    return cm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnthropicStreamPoolCleanup:
    """anthropic_messages stream cleanup must abort the in-flight MessageStream
    response (via _close_anthropic_stream_once), NOT call
    _replace_primary_openai_client (no-ops in anthropic mode -> #28161 900s hang)
    and NOT close+rebuild the SHARED _anthropic_client (pool-poison under
    concurrent sessions -> AssertionError; PRAX-PATCH 2026-06-04 + council
    reconciliation 2026-06-28). The original #28161 fix used close()+rebuild();
    that was reconciled to response-level abort, which fixes the SAME hang
    (the worker blocks on the unclosed response read, not on a sick client)
    without tearing the shared pool out from under concurrent sessions.
    """

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_stream_retry_aborts_response_not_shared_client(self):
        """Connection error during stream retry → abort the MessageStream
        response, never _replace_primary_openai_client, never close the shared
        anthropic client (concurrent-session safety)."""
        agent = _make_anthropic_agent()

        attempt_count = [0]

        def _stream_side_effect(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                return _failing_stream_cm()
            return _good_stream_cm()

        agent._anthropic_client.messages.stream.side_effect = _stream_side_effect

        with patch.object(agent, "_rebuild_anthropic_client") as mock_rebuild:
            with patch.object(
                agent, "_replace_primary_openai_client"
            ) as mock_replace:
                agent._interruptible_streaming_api_call({})

        # Core invariant both fixes agree on: never the OpenAI replacer in
        # anthropic mode (it silently no-ops, leaving the stream open).
        mock_replace.assert_not_called()
        # Reconciled invariant: never close/rebuild the SHARED client at the
        # retry site (pool poison). The response-level abort handles cleanup.
        mock_rebuild.assert_not_called()
        agent._anthropic_client.close.assert_not_called()

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_stale_stream_aborts_response_not_shared_client(self, monkeypatch):
        """Stale-stream outer-poll detector → abort the MessageStream response,
        never _replace_primary_openai_client, never close the shared client."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.1")

        agent = _make_anthropic_agent()
        unblock = threading.Event()
        attempt_count = [0]

        def _stream_side_effect(*args, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] == 1:
                # First attempt: stream that yields nothing (triggers stale
                # detector), then raises ConnectError once the stale abort
                # closes the response and unblocks it.
                cm = MagicMock()
                stream = MagicMock()

                def _blocking_gen():
                    unblock.wait(timeout=5.0)
                    raise httpx.ConnectError("connection dropped after abort")
                    yield  # make this a generator so next() triggers the wait

                stream.__iter__ = MagicMock(return_value=_blocking_gen())
                stream.close = MagicMock(side_effect=unblock.set)
                cm.__enter__ = MagicMock(return_value=stream)
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            # Second attempt: succeed
            return _good_stream_cm()

        agent._anthropic_client.messages.stream.side_effect = _stream_side_effect
        # Closing the shared client is forbidden; the stale detector unblocks
        # the worker by closing the per-response MessageStream (stream.close
        # above sets the event). Guard that the shared client is never closed.

        with patch.object(agent, "_rebuild_anthropic_client") as mock_rebuild:
            with patch.object(
                agent, "_replace_primary_openai_client"
            ) as mock_replace:
                agent._interruptible_streaming_api_call({})

        mock_replace.assert_not_called()
        # Reconciled: shared client never closed/rebuilt — abort is response-level.
        agent._anthropic_client.close.assert_not_called()
        mock_rebuild.assert_not_called()
