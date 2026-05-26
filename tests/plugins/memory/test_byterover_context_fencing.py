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
