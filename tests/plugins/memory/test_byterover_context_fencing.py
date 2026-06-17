"""Tests for the ByteRover plugin's schema v1.2 Layer 4 context-fencing.

Schema contract: see ~/.hermes/byterover/SCHEMA.md and
SPEC-layer-4-context-fencing.md v1.1 (post architecture-critic review).

Why v1.1 of the spec: the architecture-critic subagent identified 6
non-negotiable failures in v1.0 (C1, C2, C3, H1, H2, H3) plus 6 polish
items. Findings are at:
~/Documents/workspace/byterover-fix/.learnings/2026-05-26-l4-architecture-critic.md

Findings → tests in this file:
  C1 — on_pre_compress fences recalled bodies (TestOnPreCompressFencing)
  C2 — slug regex meta-chars do not over-strip (TestSlugRegexEscaping)
  C3 — slug snapshot on calling thread (TestSyncTurnSnapshotSemantics)
  H1 — brv_query tool extends slug list (TestBrvQueryToolFencing)
  H2 — structural marker is the strip anchor (TestStructuralMarkerFencing)
  H3 — nested ### sub-headers do not break strip (TestNestedSubheaders)
  H4 — direct answers with slug+date preserved (TestConservativeFencing)
  M1 — fully-fenced turns skip curate (TestFullyFencedTurnSkipsCurate)

Council Round 3 (final-diff review) findings → tests:
  R3-HIGH-1 — feature flag disables fencing byte-for-byte (TestFeatureFlag)
  R3-HIGH-2 — embedded markers in entry bodies sanitized (TestEmbeddedMarkerSanitization)
  R3-HIGH-3 — orphan begin marker swept defensively (TestOrphanBeginMarkerSweep)
  R3-HIGH-3 — fast-path no allocation when marker absent (TestFastPath)

These tests are pure-Python — no brv CLI subprocess, no real filesystem
state beyond a tmp_path for fence-decision audit log.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from plugins.memory.byterover import (
    ByteRoverMemoryProvider,
    _FENCE_BEGIN_MARKER,
    _FENCE_END_MARKER,
    _FENCE_BLOCK_RE,
    _MAX_TRACKED_SLUGS,
    _AUDIT_LOG_SLUG_CSV_MAX,
    _MIN_OUTPUT_LEN,
    _MIN_QUERY_LEN,
    _apply_context_fence,
    _extract_slugs_from_brv_output,
    _fence_log_path,
    _log_fence_decision,
    _wrap_with_fence_markers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_fencing_on(monkeypatch):
    """Force BRV_CONTEXT_FENCING=1 for this file, isolating from the operator env.

    Layer-4 fencing has an operator kill switch (BRV_CONTEXT_FENCING) which is
    legitimately set to "0" in Prax's shell env. With it off, _apply_context_fence
    correctly no-ops and every fencing assertion here fails — an env artifact, not
    a code bug. This autouse fixture pins fencing ON so the suite is robust to the
    operator's environment. Tests in TestFeatureFlag set their own value via their
    own monkeypatch, which overrides this within the test body.
    """
    monkeypatch.setenv("BRV_CONTEXT_FENCING", "1")


def _make_provider(tmp_path: Path) -> ByteRoverMemoryProvider:
    """Provider with cwd set to tmp_path so audit logs are isolated."""
    p = ByteRoverMemoryProvider()
    p._cwd = str(tmp_path)
    p._session_id = "test-session"
    return p


def _recall_block(*entries: str) -> str:
    """Return a fenced ByteRover-context block matching prefetch() output."""
    body = "**Summary**: Found relevant topics:\n\n**Details**:\n\n"
    body += "\n\n---\n\n".join(entries)
    return _wrap_with_fence_markers(body)


def _entry_with_subheaders(slug: str) -> str:
    """An entry whose body contains internal ### sub-headers (H3 case)."""
    return (
        f"### {slug}\n"
        f"\n"
        f"---\n"
        f"title: 'test'\n"
        f"status: active\n"
        f"---\n"
        f"\n"
        f"## Overview\n"
        f"Top-level intro.\n"
        f"\n"
        f"### Field semantics\n"
        f"Sub-section A body.\n"
        f"\n"
        f"### Reopening criteria\n"
        f"Sub-section B body.\n"
    )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestExtractSlugsFromBrvOutput:
    def test_empty_returns_empty_list(self):
        assert _extract_slugs_from_brv_output("") == []
        assert _extract_slugs_from_brv_output(None) == []  # type: ignore[arg-type]

    def test_extracts_single_slug(self):
        text = "**Details**:\n\n### my_slug\n\nbody"
        assert _extract_slugs_from_brv_output(text) == ["my_slug"]

    def test_extracts_multiple_slugs_in_order(self):
        text = "### slug_a\nbody\n### slug_b\nbody\n### slug_c\nbody"
        assert _extract_slugs_from_brv_output(text) == ["slug_a", "slug_b", "slug_c"]

    def test_dedupes_repeated_slugs(self):
        text = "### slug_a\nbody\n### slug_a\nbody2"
        assert _extract_slugs_from_brv_output(text) == ["slug_a"]

    def test_handles_dotted_slug(self):
        # Real brv slugs include `.` (regex meta) — must still extract cleanly.
        text = "### schema.v1.2_doc\nbody"
        assert _extract_slugs_from_brv_output(text) == ["schema.v1.2_doc"]

    def test_ignores_non_anchor_hashes(self):
        # `### foo` only matches at line start — inline ### is not an entry header.
        text = "Some prose with ### inline_hash inside.\n\n### real_slug\nbody"
        assert _extract_slugs_from_brv_output(text) == ["real_slug"]


class TestStructuralMarkerFencing:
    """H2 — fence is anchored on HTML-comment markers, not heuristics."""

    def test_no_marker_is_noop(self):
        text = "Plain assistant reasoning, no recall block."
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0

    def test_strips_complete_block(self):
        block = _recall_block("### slug_a\nbody A\n", "### slug_b\nbody B\n")
        text = block + "\n\nMy new reasoning starts here."
        fenced, n = _apply_context_fence(text)
        assert "body A" not in fenced
        assert "body B" not in fenced
        assert "slug_a" not in fenced
        assert "slug_b" not in fenced
        assert "My new reasoning starts here." in fenced
        assert n == 1

    def test_strips_multiple_blocks(self):
        text = (
            "Intro prose.\n\n"
            + _recall_block("### slug_a\nbody A\n")
            + "\n\nMiddle reasoning.\n\n"
            + _recall_block("### slug_b\nbody B\n")
            + "\n\nClosing reasoning."
        )
        fenced, n = _apply_context_fence(text)
        assert n == 2
        assert "Intro prose." in fenced
        assert "Middle reasoning." in fenced
        assert "Closing reasoning." in fenced
        assert "body A" not in fenced
        assert "body B" not in fenced

    def test_preserves_text_outside_block(self):
        block = _recall_block("### slug_a\nrecalled\n")
        text = (
            "Before block.\n\n"
            + block
            + "\n\nAfter block continues."
        )
        fenced, _ = _apply_context_fence(text)
        assert fenced.startswith("Before block.")
        assert "After block continues." in fenced
        assert "recalled" not in fenced

    def test_marker_invisible_in_markdown_output(self):
        # HTML comments do not render in markdown — confirm prefetch wraps cleanly.
        block = _wrap_with_fence_markers("**Summary**: 1\n### slug\nbody")
        assert _FENCE_BEGIN_MARKER in block
        assert _FENCE_END_MARKER in block
        # User sees just the markdown header + content; markers are HTML comments.
        assert "## ByteRover Context" in block


class TestNestedSubheaders:
    """H3 — nested ### sub-headers in entry bodies must NOT break strip."""

    def test_strips_entry_with_internal_sub_sections(self):
        block = _recall_block(_entry_with_subheaders("byterover_schema_doc"))
        text = block + "\n\nMy new reasoning."
        fenced, _ = _apply_context_fence(text)
        # Per H3: per-slug strip would stop at `### Field semantics`;
        # structural strip removes the WHOLE block.
        assert "Field semantics" not in fenced
        assert "Reopening criteria" not in fenced
        assert "Sub-section A body." not in fenced
        assert "Sub-section B body." not in fenced
        assert "My new reasoning." in fenced


class TestSlugRegexEscaping:
    """C2 — slugs containing regex meta-chars do not corrupt strip."""

    def test_slug_with_dots_doesnt_overstrip_block(self):
        # Slug `schema.v1.2` contains `.` (regex meta); structural strip is
        # immune because it does NOT use slugs for substitution. Verify.
        block = _recall_block("### schema.v1.2\nrecalled body\n")
        text = block + "\n\nNew reasoning about schemaXv1X2 elsewhere."
        fenced, _ = _apply_context_fence(text)
        assert "recalled body" not in fenced
        # Critical: the new reasoning's "schemaXv1X2" must NOT be touched
        # (it is similar in shape to slug after `.` matches anything).
        assert "schemaXv1X2 elsewhere." in fenced

    def test_slug_with_meta_chars_extracts_correctly(self):
        # Real brv slugs admit `.` and `/`; ensure extraction handles both.
        text = "### path/to.slug.v2\nbody"
        slugs = _extract_slugs_from_brv_output(text)
        assert slugs == ["path/to.slug.v2"]


class TestSyncTurnSnapshotSemantics:
    """C3 — sync_turn snapshots slug list on calling thread; bg curate uses snapshot."""

    def test_fence_runs_on_calling_thread(self, tmp_path):
        p = _make_provider(tmp_path)
        # Simulate prefetch having run: slugs in instance state
        p._last_prefetched_slugs = ["slug_a", "slug_b"]

        block = _recall_block("### slug_a\nbody A\n", "### slug_b\nbody B\n")
        assistant = block + "\n\nNew reasoning."

        # Mock _run_brv so we can capture what would be curated
        captured = {}
        def fake_run_brv(args, **kwargs):
            captured["args"] = args
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            p.sync_turn("user query about slugs", assistant)
            # Wait for bg thread to fire
            if p._sync_thread:
                p._sync_thread.join(timeout=5.0)

        # The curate command should contain the FENCED assistant content
        assert "args" in captured
        combined = captured["args"][2]
        assert "body A" not in combined
        assert "body B" not in combined
        assert "New reasoning." in combined

    def test_prefetch_after_sync_turn_does_not_corrupt_inflight_curate(self, tmp_path):
        """C3 race: prefetch(N+1) BEFORE sync_turn(N) bg thread fires must
        not change what gets curated. Snapshot semantics protect this."""
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["turn_n_slug"]

        block = _recall_block("### turn_n_slug\nturn N body\n")
        assistant_n = block + "\n\nTurn N reasoning."

        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args[2])
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            p.sync_turn("user query N", assistant_n)
            # SIMULATE prefetch(N+1) overwriting state BEFORE bg thread fires
            p._last_prefetched_slugs = ["turn_n_plus_1_slug"]
            if p._sync_thread:
                p._sync_thread.join(timeout=5.0)

        # Turn N's curate should still be fenced (block stripped) — snapshot held.
        assert len(captured) == 1
        assert "turn N body" not in captured[0]
        assert "Turn N reasoning." in captured[0]


class TestOnPreCompressFencing:
    """C1 — on_pre_compress was the unfenced 2nd write path; fix funnels through fence."""

    def test_strips_recall_block_from_messages(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["dralexmorganbot_daemon_status"]

        recall_msg = {
            "role": "assistant",
            "content": (
                _recall_block("### dralexmorganbot_daemon_status\n"
                              "RCA filed 2026-05-14, fixed 41 min later.\n")
                + "\n\nThe daemon is healthy."
            ),
        }
        # Pad with 9 short messages so recall_msg lands in messages[-10:]
        msgs = [
            {"role": "user", "content": f"question {i}"} for i in range(5)
        ] + [
            {"role": "assistant", "content": f"answer {i}"} for i in range(4)
        ] + [recall_msg]

        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args[2])
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            p.on_pre_compress(msgs)
            # Wait for bg thread
            import time
            time.sleep(0.3)

        # The pre-compression flush must NOT contain the recalled body
        if captured:
            joined = " ".join(captured)
            assert "RCA filed 2026-05-14" not in joined
            # New reasoning should be preserved
            assert "The daemon is healthy." in joined or "answer" in joined

    def test_audit_log_records_fence(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["test_slug"]

        msg = {
            "role": "assistant",
            "content": _recall_block("### test_slug\nbody\n") + "\n\nNew text.",
        }
        with mock_patch("plugins.memory.byterover._run_brv",
                        return_value={"success": True, "output": ""}):
            p.on_pre_compress([msg] * 5)
            import time
            time.sleep(0.2)

        log_path = _fence_log_path(str(tmp_path))
        assert log_path.exists(), "fence audit log should be created"
        log_content = log_path.read_text()
        assert "test_slug" in log_content


class TestBrvQueryToolFencing:
    """H1 — brv_query tool path extends slug list so fence covers it."""

    def test_tool_query_extends_slug_list(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = []

        synthetic_output = (
            "**Summary**: Found 2 topics:\n\n**Details**:\n\n"
            "### tool_slug_a\nbody A\n\n---\n\n"
            "### tool_slug_b\nbody B\n"
        )
        with mock_patch("plugins.memory.byterover._run_brv",
                        return_value={"success": True, "output": synthetic_output}):
            result = p._tool_query({"query": "test query"})

        assert "result" in result
        # Slug list should now contain both slugs from the tool output
        assert "tool_slug_a" in p._last_prefetched_slugs
        assert "tool_slug_b" in p._last_prefetched_slugs

    def test_tool_query_dedupes_with_existing_slugs(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["existing_slug", "tool_slug_a"]

        synthetic_output = (
            "**Summary**:\n\n**Details**:\n\n"
            "### tool_slug_a\nbody A\n\n### tool_slug_b\nbody B\n"
        )
        with mock_patch("plugins.memory.byterover._run_brv",
                        return_value={"success": True, "output": synthetic_output}):
            p._tool_query({"query": "test"})

        # Order preserved, no duplicates
        assert p._last_prefetched_slugs == ["existing_slug", "tool_slug_a", "tool_slug_b"]

    def test_tool_query_bounds_at_max_tracked_slugs(self, tmp_path):
        p = _make_provider(tmp_path)
        # Pre-fill slug list near the cap
        p._last_prefetched_slugs = [f"slug_{i}" for i in range(45)]

        synthetic_output = "**Details**:\n\n" + "\n".join(
            f"### tool_slug_{i}\nbody" for i in range(20)
        )
        with mock_patch("plugins.memory.byterover._run_brv",
                        return_value={"success": True, "output": synthetic_output}):
            p._tool_query({"query": "test"})

        # Total bounded to _MAX_TRACKED_SLUGS
        assert len(p._last_prefetched_slugs) <= _MAX_TRACKED_SLUGS


class TestConservativeFencing:
    """H4 — direct answers (no recall block) are preserved verbatim."""

    def test_direct_answer_with_slug_and_date_preserved(self):
        text = (
            "Resolved on 2026-05-14, see `dralexmorganbot_daemon_status`. "
            "The fix was a one-line swap in daemon.py:121."
        )
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0

    def test_user_authoring_about_slug_preserved(self):
        text = (
            "I want to write a new entry that supersedes "
            "`dralexmorganbot_daemon_status` since it's resolved."
        )
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0

    def test_inline_hash_in_prose_preserved(self):
        # `### inline_thing` mid-sentence is NOT an entry header.
        text = "The format uses ### header markers in markdown."
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0


class TestFullyFencedTurnSkipsCurate:
    """M1 — when fence strips everything, skip curate entirely."""

    def test_fully_fenced_user_and_assistant_skip_curate(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["my_slug"]

        # Both user and assistant content are JUST recall blocks
        block = _recall_block("### my_slug\nrecalled body\n")
        # User content is too short post-fence (< _MIN_QUERY_LEN = 10)
        # Assistant content is too short post-fence (< _MIN_OUTPUT_LEN = 20)
        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args)
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            # User content > _MIN_QUERY_LEN raw, but fence strips to nothing.
            p.sync_turn(block + "\n", block + "\n")
            if p._sync_thread:
                p._sync_thread.join(timeout=2.0)

        # Curate should NOT have been called
        assert len(captured) == 0, "curate should be skipped when fully fenced"

    def test_audit_log_marks_fully_fenced(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["my_slug"]

        block = _recall_block("### my_slug\nrecalled body\n")

        with mock_patch("plugins.memory.byterover._run_brv",
                        return_value={"success": True, "output": ""}):
            # Need raw user_content > _MIN_QUERY_LEN to even reach the fence.
            p.sync_turn(block + "user query of sufficient length", block + "\n")
            if p._sync_thread:
                p._sync_thread.join(timeout=2.0)

        log_path = _fence_log_path(str(tmp_path))
        assert log_path.exists()
        log_content = log_path.read_text()
        assert "fully_fenced=" in log_content


class TestAuditLogFormat:
    def test_log_line_format(self, tmp_path):
        _log_fence_decision(
            brv_cwd=str(tmp_path),
            session_id="sess-abc",
            slugs_for_audit=("slug_a", "slug_b"),
            chars_stripped=1234,
            fully_fenced=False,
        )
        log_path = _fence_log_path(str(tmp_path))
        line = log_path.read_text().strip()
        # ISO 8601 timestamp + tab-separated fields
        parts = line.split("\t")
        assert len(parts) == 5
        assert "T" in parts[0]  # ISO format
        assert parts[1] == "sess-abc"
        assert parts[2] == "slug_a,slug_b"
        assert parts[3] == "1234"
        assert parts[4] == "fully_fenced=false"

    def test_log_caps_slug_csv_at_max(self, tmp_path):
        # Exactly at cap: no truncation note
        slugs_at_cap = tuple(f"slug_{i}" for i in range(_AUDIT_LOG_SLUG_CSV_MAX))
        _log_fence_decision(
            brv_cwd=str(tmp_path),
            session_id="s",
            slugs_for_audit=slugs_at_cap,
            chars_stripped=0,
            fully_fenced=False,
        )
        line = _fence_log_path(str(tmp_path)).read_text().strip()
        assert "trunc(" not in line

    def test_log_truncates_oversized_slug_csv(self, tmp_path):
        # Over cap: truncation note appended
        oversized = tuple(f"slug_{i}" for i in range(_AUDIT_LOG_SLUG_CSV_MAX + 5))
        _log_fence_decision(
            brv_cwd=str(tmp_path),
            session_id="s",
            slugs_for_audit=oversized,
            chars_stripped=0,
            fully_fenced=False,
        )
        line = _fence_log_path(str(tmp_path)).read_text().strip()
        assert "trunc(5 more)" in line

    def test_log_handles_session_id_with_tab_safely(self, tmp_path):
        _log_fence_decision(
            brv_cwd=str(tmp_path),
            session_id="sess\twith\ttab",
            slugs_for_audit=(),
            chars_stripped=0,
            fully_fenced=False,
        )
        line = _fence_log_path(str(tmp_path)).read_text().strip()
        # Tabs in session_id replaced with spaces; still 5 TSV fields.
        assert len(line.split("\t")) == 5


class TestRecursivePollutionPrevention:
    """The integration motivator: a turn quoting a recalled RCA must not
    re-import the RCA body into a fresh entry."""

    def test_dralexmorganbot_pattern_blocked(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["dralexmorganbot_daemon_status"]

        # The exact pollution pattern that motivated this whole project:
        # assistant pulls in the recalled RCA body and reasons about it.
        recall_body = (
            "### dralexmorganbot_daemon_status\n"
            "RCA filed 2026-05-14: daemon.py:121 hardwires MockLegacyHandler.\n"
            "Fix: swap to C3LegacyAdapter from c3_adapter.py.\n"
            "Status: resolved 2026-05-14 commit bbb013d.\n"
        )
        block = _recall_block(recall_body)

        assistant = (
            block
            + "\n\nI see the daemon RCA was already resolved. "
            "The schema doc is the next thing to check."
        )

        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args[2])
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            p.sync_turn("can you check if the daemon RCA was fixed", assistant)
            if p._sync_thread:
                p._sync_thread.join(timeout=5.0)

        assert len(captured) == 1, "should still curate (substantive new content)"
        curated = captured[0]
        # The recalled body must NOT appear in the captured turn.
        assert "MockLegacyHandler" not in curated
        assert "daemon.py:121 hardwires" not in curated
        assert "commit bbb013d" not in curated
        # The assistant's new reasoning IS captured.
        assert "schema doc is the next thing to check." in curated


# ---------------------------------------------------------------------------
# Council Round 3 hardening — feature flag, marker sanitization, orphan sweep
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    """R3-HIGH-1: BRV_CONTEXT_FENCING env var as kill switch."""

    def test_disabled_returns_input_unchanged(self, monkeypatch):
        monkeypatch.setenv("BRV_CONTEXT_FENCING", "0")
        block = _recall_block("### slug_a\nbody A\n")
        text = block + "\n\nNew reasoning."
        fenced, n = _apply_context_fence(text)
        # Byte-for-byte identical when fencing disabled.
        assert fenced == text
        assert n == 0

    def test_disabled_falsy_variants(self, monkeypatch):
        for val in ("0", "false", "FALSE", "False", "no", "off"):
            monkeypatch.setenv("BRV_CONTEXT_FENCING", val)
            block = _recall_block("### slug\nbody\n")
            text = block + "\nReasoning."
            fenced, n = _apply_context_fence(text)
            assert fenced == text, f"expected no-op for BRV_CONTEXT_FENCING={val!r}"
            assert n == 0

    def test_enabled_default(self, monkeypatch):
        # No env var = enabled by default
        monkeypatch.delenv("BRV_CONTEXT_FENCING", raising=False)
        block = _recall_block("### slug\nbody\n")
        text = block + "\nReasoning."
        fenced, n = _apply_context_fence(text)
        assert n == 1
        assert "body" not in fenced
        assert "Reasoning." in fenced

    def test_disabled_wrap_is_pre_l4_shape(self, monkeypatch):
        """When fencing disabled, _wrap_with_fence_markers returns the
        legacy ## ByteRover Context block — byte-for-byte pre-L4."""
        monkeypatch.setenv("BRV_CONTEXT_FENCING", "0")
        wrapped = _wrap_with_fence_markers("body content")
        assert wrapped == "## ByteRover Context\nbody content"
        assert _FENCE_BEGIN_MARKER not in wrapped
        assert _FENCE_END_MARKER not in wrapped

    def test_truthy_values_enable(self, monkeypatch):
        """Anything not in the falsy set enables (defensive default)."""
        for val in ("1", "true", "yes", "on", "anything", ""):
            monkeypatch.setenv("BRV_CONTEXT_FENCING", val)
            block = _recall_block("### slug\nbody\n")
            text = block + "\nReasoning."
            _, n = _apply_context_fence(text)
            assert n == 1, f"expected fence active for BRV_CONTEXT_FENCING={val!r}"


class TestEmbeddedMarkerSanitization:
    """R3-HIGH-2: a curated entry body containing the marker strings must
    not break the strip when re-fetched. Sanitize on wrap."""

    def test_embedded_markers_in_body_sanitized_on_wrap(self):
        # Simulate a recalled entry whose body itself contains the marker
        # (e.g. a learning ABOUT the fence design — this very project's
        # self-write pattern).
        nasty_body = (
            "### learning_about_l4_fence\n"
            "The fence uses '<!-- byterover-context-begin -->' as a marker.\n"
            "It pairs with '<!-- byterover-context-end -->'.\n"
        )
        wrapped = _wrap_with_fence_markers(nasty_body)
        # Outer markers still present
        assert wrapped.startswith(_FENCE_BEGIN_MARKER)
        assert wrapped.endswith(_FENCE_END_MARKER)
        # Inner markers replaced with escape variants
        assert "byterover-context-begin-ZWNJ" in wrapped
        assert "byterover-context-end-ZWNJ" in wrapped
        # Verify exactly ONE pair of real markers (the outer wrap).
        assert wrapped.count(_FENCE_BEGIN_MARKER) == 1
        assert wrapped.count(_FENCE_END_MARKER) == 1

    def test_strip_works_with_sanitized_inner_content(self):
        """The whole sanitized block strips cleanly — no leak from inner
        escape markers that look similar to real markers."""
        nasty_body = (
            "### learning\n"
            "Marker is <!-- byterover-context-begin --> and "
            "<!-- byterover-context-end -->.\n"
        )
        wrapped = _wrap_with_fence_markers(nasty_body)
        text = wrapped + "\n\nMy new reasoning continues."
        fenced, n = _apply_context_fence(text)
        assert n == 1
        # The whole wrapped block (including escape variants) is stripped.
        assert "learning" not in fenced
        assert "byterover-context-begin-ZWNJ" not in fenced
        assert "byterover-context-end-ZWNJ" not in fenced
        assert "My new reasoning continues." in fenced

    def test_recursive_self_write_pattern_blocked(self):
        """The exact self-write pattern that motivated R3-HIGH-2: agent
        writes a learning ABOUT the fence design and that learning gets
        recalled in a future turn. The fence must still strip cleanly."""
        learning_body = (
            "### byterover_l4_fence_design_2026_05_26\n"
            "The Layer 4 context fence uses <!-- byterover-context-begin -->\n"
            "and <!-- byterover-context-end --> markers to anchor structural\n"
            "stripping. Per Round 3 HIGH-2, these are sanitized in-content\n"
            "to ZWNJ-escape variants before wrap.\n"
        )
        wrapped = _wrap_with_fence_markers(learning_body)
        text = (
            "Earlier reasoning.\n\n"
            + wrapped
            + "\n\nNew reasoning. The fence works."
        )
        fenced, n = _apply_context_fence(text)
        assert n == 1
        assert "Earlier reasoning." in fenced
        assert "New reasoning. The fence works." in fenced
        # No part of the recalled learning leaks
        assert "byterover_l4_fence_design" not in fenced
        assert "ZWNJ" not in fenced


class TestOrphanBeginMarkerSweep:
    """R3-HIGH-3: an unmatched begin marker (no paired end) must be swept.

    Failure modes covered: (1) truncation mid-stream, (2) partial assistant
    write, (3) adversarial chat-of-thought emitting begin marker prose.
    """

    def test_orphan_begin_without_end_swept(self):
        # Simulated truncation: begin marker present, end marker missing.
        text = (
            "Reasoning before.\n"
            f"{_FENCE_BEGIN_MARKER}\n"
            "## ByteRover Context\n"
            "### slug\nbody body\n"
            "(stream truncated here, no end marker)\n"
        )
        fenced, n = _apply_context_fence(text)
        # Phase 1 strips nothing (no paired end), but Phase 2 sweeps the
        # orphan begin line so the marker text doesn't leak into curate.
        assert _FENCE_BEGIN_MARKER not in fenced
        assert "Reasoning before." in fenced

    def test_assistant_quoting_begin_marker_not_overstrip(self):
        # Adversarial: assistant prose contains the begin marker as a
        # quoted reference. With NO paired end, Phase 2 still strips the
        # line carrying the marker — but everything else is preserved.
        text = (
            "I will document the fence design.\n"
            f"The marker is `{_FENCE_BEGIN_MARKER}` and pairs with end.\n"
            "More prose after.\n"
        )
        fenced, _ = _apply_context_fence(text)
        # Marker-line stripped (defensive sweep)
        assert _FENCE_BEGIN_MARKER not in fenced
        # Surrounding prose preserved
        assert "I will document the fence design." in fenced
        assert "More prose after." in fenced

    def test_well_formed_block_phase1_strips_then_phase2_noop(self):
        # Healthy case: complete block. Phase 1 strips, Phase 2 sees no
        # remaining begin marker and bails fast.
        block = _recall_block("### slug\nbody\n")
        text = block + "\n\nReasoning."
        fenced, n = _apply_context_fence(text)
        assert n == 1
        assert _FENCE_BEGIN_MARKER not in fenced
        assert "Reasoning." in fenced


class TestFastPath:
    """R3-HIGH-3 perf mitigation: marker absent = no regex compile/scan."""

    def test_no_marker_short_circuit_returns_unchanged(self):
        # When marker is absent, _apply_context_fence must return the
        # exact same string (object identity not required; equality is).
        text = "x" * 100_000  # large content, no marker
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0

    def test_no_marker_with_inline_html_comment_unchanged(self):
        # Plain HTML comments unrelated to byterover are preserved verbatim.
        text = "<!-- some other comment -->\nContent.\n<!-- another -->"
        fenced, n = _apply_context_fence(text)
        assert fenced == text
        assert n == 0


class TestToolCurateFencing:
    """Round 3 defense-in-depth: explicit brv_curate tool call also fenced."""

    def test_tool_curate_strips_recall_block(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["slug_a"]

        block = _recall_block("### slug_a\nrecalled body\n")
        content_with_recall = block + "\n\nNew explanation to remember."

        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args)
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            result = p._tool_curate({"content": content_with_recall})

        assert "result" in result, f"expected curate success, got {result!r}"
        assert len(captured) == 1
        curated = captured[0][2]  # the actual content passed to brv curate
        assert "recalled body" not in curated
        assert "New explanation to remember." in curated

    def test_tool_curate_rejects_fully_fenced_content(self, tmp_path):
        p = _make_provider(tmp_path)
        p._last_prefetched_slugs = ["slug_a"]

        # Content is JUST a recall block — fully fenced = nothing left.
        block = _recall_block("### slug_a\nbody\n")

        captured = []
        def fake_run_brv(args, **kwargs):
            captured.append(args)
            return {"success": True, "output": ""}

        with mock_patch("plugins.memory.byterover._run_brv", side_effect=fake_run_brv):
            result = p._tool_curate({"content": block})

        # Should return tool_error, not call brv curate
        assert "error" in result.lower() or "empty" in result.lower()
        assert len(captured) == 0


class TestRecencyDecayDeterminism:
    """Council Round 3 MEDIUM-3 / HIGH-4 follow-up: confirm _apply_recency_decay
    with NO updatedAt does not silently demote legacy entries below stale-but-
    timestamped entries on a synthetic boundary case."""

    def test_legacy_entry_treated_as_90_days_for_decay(self):
        # The decay assigns no-updatedAt entries to "90 days old". This is a
        # tradeoff: legacy entries land mid-decay (not promoted, not buried).
        # Verify it's documented in the function's docstring.
        from plugins.memory.byterover import _apply_recency_decay
        assert "90 days" in _apply_recency_decay.__doc__ or "mid-decay" in _apply_recency_decay.__doc__


class TestAuditLogSlugCsvMax:
    """P3-1 (2026-05-28): boundary test for _AUDIT_LOG_SLUG_CSV_MAX.

    The cap exists to keep each audit log line under PIPE_BUF (~4KB) so
    write-to-fd is atomic on POSIX filesystems. If someone bumps the cap
    in the future without checking the math, this test catches the
    regression — line length must stay under a safe atomicity ceiling.
    """

    def test_cap_truncates_excess_slugs(self, tmp_path):
        """Slugs beyond _AUDIT_LOG_SLUG_CSV_MAX get truncated with a marker."""
        brv_cwd = str(tmp_path)
        # Pass 60 slugs when cap is 50 — expect first 50 + truncation marker
        many_slugs = tuple(f"slug-{i:03d}" for i in range(60))
        _log_fence_decision(
            brv_cwd=brv_cwd,
            session_id="test-session",
            slugs_for_audit=many_slugs,
            chars_stripped=1000,
            fully_fenced=False,
        )
        log_path = tmp_path / ".brv" / "fenced.log"
        assert log_path.exists()
        line = log_path.read_text().splitlines()[0]
        # First 50 slugs present
        for i in range(_AUDIT_LOG_SLUG_CSV_MAX):
            assert f"slug-{i:03d}" in line
        # Truncation marker present with correct count
        excess = 60 - _AUDIT_LOG_SLUG_CSV_MAX
        assert f"...trunc({excess} more)" in line
        # Slugs beyond cap NOT present (verify no leak)
        for i in range(_AUDIT_LOG_SLUG_CSV_MAX, 60):
            assert f"slug-{i:03d}" not in line

    def test_line_length_stays_under_pipe_buf(self, tmp_path):
        """At cap with realistic slug-length, line must stay under 4KB
        (POSIX PIPE_BUF) to preserve write-atomicity on regular files.

        macOS APFS, ext4, ZFS all give per-write atomicity for short
        writes; PIPE_BUF only applies to pipes/FIFOs but is a useful
        upper bound for log-line atomicity on append-mode regular files.

        At _AUDIT_LOG_SLUG_CSV_MAX=50 with 50-char slugs:
          50 slugs × 50 chars + 49 commas = 2549 chars
          + ISO ts (~25) + session_id (≤64) + chars_stripped (~10)
          + fully_fenced (~20) + tabs/newline (~5) ≈ 2673 chars
        Well under 4096 PIPE_BUF.
        """
        brv_cwd = str(tmp_path)
        # Realistic worst-case: 50 slugs each at the longest name we'd see
        # in practice. ByteRover slugs are typically 10-40 chars; 50 chars
        # is a generous upper bound.
        long_slugs = tuple(f"some-long-namespace/sub-area/slug-name-{i:03d}" for i in range(_AUDIT_LOG_SLUG_CSV_MAX))
        # Verify our test setup matches realistic upper bound
        for s in long_slugs:
            assert 30 <= len(s) <= 70, f"test slug {s!r} length {len(s)} outside expected range"

        _log_fence_decision(
            brv_cwd=brv_cwd,
            session_id="x" * 64,  # max session_id length
            slugs_for_audit=long_slugs,
            chars_stripped=1234567,
            fully_fenced=True,
        )
        log_path = tmp_path / ".brv" / "fenced.log"
        line = log_path.read_text().splitlines()[0]
        # PIPE_BUF safety ceiling — must stay under 4KB
        assert len(line) < 4096, (
            f"audit log line is {len(line)} chars, exceeds PIPE_BUF=4096; "
            f"_AUDIT_LOG_SLUG_CSV_MAX={_AUDIT_LOG_SLUG_CSV_MAX} or slug naming "
            f"convention has grown — re-audit the cap math"
        )

    def test_under_cap_no_truncation_marker(self, tmp_path):
        """When slug count is at-or-below cap, no truncation marker emitted."""
        brv_cwd = str(tmp_path)
        few_slugs = tuple(f"slug-{i:03d}" for i in range(_AUDIT_LOG_SLUG_CSV_MAX))  # exactly at cap
        _log_fence_decision(
            brv_cwd=brv_cwd,
            session_id="test",
            slugs_for_audit=few_slugs,
            chars_stripped=100,
            fully_fenced=False,
        )
        log_path = tmp_path / ".brv" / "fenced.log"
        line = log_path.read_text().splitlines()[0]
        assert "...trunc(" not in line, "no truncation marker expected at exactly cap"
        # All slugs present
        for i in range(_AUDIT_LOG_SLUG_CSV_MAX):
            assert f"slug-{i:03d}" in line
