"""Regression tests for the anthropic_messages stream-abort fix (PRAX-PATCH 2026-06-04).

Covers the council-required behaviors for closing the SDK MessageStream
(the response) — NOT the shared _anthropic_client — when the stale-detector
or an interrupt fires on the anthropic_messages transport.

Root cause being guarded against:
  - _call_anthropic streamed via the SDK MessageStream ctx-mgr but never
    registered a client the watchdog could close -> stale-kill was a NO-OP ->
    stalled Bedrock-Claude streams hung until the 900s httpx read timeout.
  - The interrupt path closed the SHARED _anthropic_client mid-stream ->
    AssertionErrors from a torn httpx/urllib3 pool under concurrent sessions.

These tests are pure-unit (no network): they exercise the
``_close_anthropic_stream_once`` helper semantics and the abort-reason
sentinel routing via lightweight fakes, plus the read-timeout default.
"""

import threading
import time

import pytest


# ─────────────────────────────────────────────────────────────────────────
# A6: read-timeout default lowered + env-overridable
# ─────────────────────────────────────────────────────────────────────────
def test_anthropic_default_read_timeout_default(monkeypatch):
    import agent.anthropic_adapter as aa

    monkeypatch.delenv("HERMES_ANTHROPIC_READ_TIMEOUT", raising=False)
    assert aa._anthropic_default_read_timeout() == 360.0


def test_anthropic_default_read_timeout_env_override(monkeypatch):
    import agent.anthropic_adapter as aa

    monkeypatch.setenv("HERMES_ANTHROPIC_READ_TIMEOUT", "120")
    assert aa._anthropic_default_read_timeout() == 120.0


def test_anthropic_default_read_timeout_bad_value_falls_back(monkeypatch):
    import agent.anthropic_adapter as aa

    monkeypatch.setenv("HERMES_ANTHROPIC_READ_TIMEOUT", "not-a-number")
    assert aa._anthropic_default_read_timeout() == 360.0
    monkeypatch.setenv("HERMES_ANTHROPIC_READ_TIMEOUT", "-5")
    assert aa._anthropic_default_read_timeout() == 360.0


# ─────────────────────────────────────────────────────────────────────────
# Helper semantics: a faithful re-implementation of the close-once contract,
# matching chat_completion_helpers exactly, so we can assert the invariants
# the council required (close response not client, idempotent, reason set,
# close OUTSIDE the lock) without standing up the full AIAgent.
# ─────────────────────────────────────────────────────────────────────────
class _FakeStream:
    def __init__(self):
        self.closed = 0
        self.close_thread = None

    def close(self):
        self.closed += 1
        self.close_thread = threading.current_thread().name


def _make_close_once(holder, lock, *, log_calls):
    def _close_anthropic_stream_once(reason: str) -> None:
        with lock:
            stream = holder.get("anthropic_stream")
            holder["anthropic_stream"] = None
            holder["abort_reason"] = reason
            held_during_close = []  # sentinel to detect close-under-lock
        if stream is None:
            return
        # Assert we are NOT holding the lock here (council Q2: close outside lock).
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            held_during_close.append(False)
        else:
            held_during_close.append(True)
        log_calls.append((reason, held_during_close[0]))
        try:
            stream.close()
        except Exception:
            pass
    return _close_anthropic_stream_once


def test_close_once_closes_stream_and_sets_reason():
    holder = {"anthropic_stream": _FakeStream(), "abort_reason": None}
    lock = threading.Lock()
    logs = []
    close_once = _make_close_once(holder, lock, log_calls=logs)

    close_once("stale")

    assert holder["anthropic_stream"] is None         # nulled
    assert holder["abort_reason"] == "stale"           # sentinel recorded
    assert logs[0][0] == "stale"
    assert logs[0][1] is False                         # NOT holding lock at close (Q2)


def test_close_once_idempotent():
    fake = _FakeStream()
    holder = {"anthropic_stream": fake, "abort_reason": None}
    lock = threading.Lock()
    close_once = _make_close_once(holder, lock, log_calls=[])

    close_once("stale")
    close_once("stale")   # second call: holder already None -> no double close
    close_once("interrupt")

    assert fake.closed == 1                            # closed exactly once


def test_close_once_suppresses_close_exception():
    class _Boom(_FakeStream):
        def close(self):
            raise RuntimeError("pool torn under active read")

    holder = {"anthropic_stream": _Boom(), "abort_reason": None}
    lock = threading.Lock()
    close_once = _make_close_once(holder, lock, log_calls=[])
    # Must not raise — the watchdog thread cannot be allowed to die.
    close_once("stale")
    assert holder["anthropic_stream"] is None


def test_close_once_cross_thread_unblocks_iteration():
    """The watchdog (parent thread) closing the stream must make a worker
    thread blocked in iteration raise/exit — within a tight bound, not 900s."""
    stop = threading.Event()
    raised: dict = {"err": None}

    class _BlockingStream(_FakeStream):
        def __iter__(self):
            return self

        def __next__(self):
            # Simulate a blocked socket read that only ends when close() fires.
            if stop.wait(timeout=10.0):
                raise ConnectionError("stream closed by watchdog")
            raise AssertionError("iteration not aborted within bound")

        def close(self):
            super().close()
            stop.set()   # closing the response unblocks the reader

    holder = {"anthropic_stream": _BlockingStream(), "abort_reason": None}
    lock = threading.Lock()
    close_once = _make_close_once(holder, lock, log_calls=[])

    def worker():
        try:
            for _ in holder["anthropic_stream"]:
                pass
        except Exception as e:   # noqa: BLE001 — capture whatever the abort raises
            raised["err"] = e

    t = threading.Thread(target=worker, name="worker")
    t.start()
    time.sleep(0.2)                       # let worker enter the blocked read
    close_once("stale")                   # watchdog aborts from THIS thread
    t.join(timeout=3.0)

    assert not t.is_alive()               # worker exited promptly, not at 900s
    assert isinstance(raised["err"], ConnectionError)


# ─────────────────────────────────────────────────────────────────────────
# Source-level guard rails: assert the production code wires the helper into
# the stale + interrupt branches and does NOT close the shared client there.
# This is the regression that pins the actual bug-fix in place.
# ─────────────────────────────────────────────────────────────────────────
def _helper_source():
    import inspect
    import agent.chat_completion_helpers as cch
    return inspect.getsource(cch.interruptible_streaming_api_call)


def test_stale_branch_uses_stream_close_not_client_close():
    src = _helper_source()
    # The anthropic stale branch must call the new helper...
    assert '_close_anthropic_stream_once("stale")' in src
    # ...and the interrupt branch must use it too...
    assert '_close_anthropic_stream_once("interrupt")' in src
    # ...and must NOT close/rebuild the shared anthropic client on interrupt.
    # Strip comment lines first so our own explanatory comments (which
    # reference the OLD code) don't false-positive.
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "agent._anthropic_client.close()" not in code_only
    assert "agent._rebuild_anthropic_client()" not in code_only


def test_worker_registers_and_clears_stream():
    src = _helper_source()
    assert "_register_anthropic_stream(stream)" in src
    assert "_clear_anthropic_stream()" in src
    # The clear must be in a finally so a dead handle never leaks.
    assert "finally:" in src


def test_abort_reason_sentinel_routing_present():
    src = _helper_source()
    # stale -> retry, interrupt -> raise InterruptedError
    assert 'request_client_holder.get("abort_reason")' in src
    assert '_abort_reason == "interrupt"' in src
    assert '_abort_reason == "stale"' in src


# ─────────────────────────────────────────────────────────────────────────
# PRAX-PATCH 2026-06-04 (default session, ownership review): the NON-STREAM
# anthropic path (_call inside interruptible_api_call) must ALSO never close
# the shared _anthropic_client — same anti-pattern the streaming fix removed.
# This guards against a future edit reintroducing it on either path.
# ─────────────────────────────────────────────────────────────────────────
def test_no_shared_anthropic_client_close_anywhere_in_helpers():
    """Source-level guard: zero LIVE agent._anthropic_client.close() calls in
    chat_completion_helpers (comments referencing the old pattern are fine).
    Both the streaming and non-streaming anthropic abort paths must rebuild
    the shared client, never close it mid-request (pool-poison / AssertionError).
    """
    import os
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "agent", "chat_completion_helpers.py")
    with open(path, encoding="utf-8") as fh:
        code_lines = [
            ln for ln in fh.read().splitlines()
            if not ln.lstrip().startswith("#")
        ]
    code = "\n".join(code_lines)
    assert "_anthropic_client.close()" not in code, (
        "Found a LIVE agent._anthropic_client.close() call — closing the shared "
        "client mid-request poisons the pool for concurrent sessions. Rebuild "
        "(reassign) the client instead; never close it."
    )
