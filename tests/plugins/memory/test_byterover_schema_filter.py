"""Tests for the ByteRover plugin's schema v1.1 lifecycle filter.

Schema contract (locked 2026-05-26): see ~/.hermes/byterover/SCHEMA.md.

These tests exercise the post-`brv query` filter pipeline that drops
entries with `status: resolved | superseded | candidate_resolved` from
auto-recall unless the user query carries explicit historical-intent
signal. Triggered by the 12-turn dralexmorganbot stale-recall incident.

Tests are pure-Python — no brv CLI subprocess, no filesystem state
beyond a tmp_path for filter-decision audit log.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest

from plugins.memory.byterover import (
    _FILTERED_STATUSES,
    _HISTORICAL_INTENT_TOKENS,
    _apply_schema_filter,
    _has_historical_intent,
    _parse_entry_status,
    _parse_entry_updated_at,
    _split_entries,
    _filter_resolved_entries,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic brv-shaped query output
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_from_v14_gate(monkeypatch):
    """Disable the v1.4 query-relevance gate for THIS file.

    This file targets the v1.1 (resolved/superseded) and recency layers, which
    are orthogonal to the v1.4 relevance gate. The synthetic fixtures here use
    slugs/bodies that deliberately don't lexically match their neutral test
    queries (e.g. slug 'old_one' vs query 'some query'), so the v1.4 gate would
    correctly empty them and mask the layer under test. The gate has its own
    dedicated coverage in test_byterover_relevance_gate.py. Standard
    layer-isolation, not behavior-masking.
    """
    monkeypatch.setenv("BRV_RELEVANCE_GATE", "0")


def _entry(slug: str, status: str | None, updated_at: str = "2026-05-25T10:00:00.000Z",
           summary: str = "test entry") -> str:
    """Build a synthetic per-entry block matching brv query output shape."""
    fm_status = f"\nstatus: {status}" if status else ""
    return f"""### {slug}

---
title: '{slug} title'
summary: '{summary}'{fm_status}
createdAt: '2026-05-01T00:00:00.000Z'
updatedAt: '{updated_at}'
---

## Body

Some content here for {slug}.
"""


def _wrap(*entries: str) -> str:
    """Wrap entries into the brv `**Summary**:` shape."""
    n = len([e for e in entries])
    out = f"**Summary**: Found {n} relevant topics:\n\n**Details**:\n\n"
    out += "\n---\n\n".join(entries)
    return out


# ---------------------------------------------------------------------------
# _has_historical_intent
# ---------------------------------------------------------------------------


class TestHistoricalIntent:
    def test_empty_query_returns_false(self):
        assert _has_historical_intent("") is False
        assert _has_historical_intent(None) is False  # type: ignore[arg-type]

    def test_neutral_query_returns_false(self):
        assert _has_historical_intent("fix this bug") is False
        assert _has_historical_intent("how does the daemon work") is False
        assert _has_historical_intent("Explain the C3 wire-in") is False

    @pytest.mark.parametrize("token", [
        "previously",
        "history",
        "historical",
        "historically",
        "in the past",
        "already fixed",
        "already shipped",
        "show resolved",
        "show all",
        "include history",
        "rca history",
        "when did we",
        "archived",
    ])
    def test_historical_tokens_trigger_bypass(self, token):
        assert _has_historical_intent(f"please tell me {token} about the daemon") is True

    def test_case_insensitive(self):
        assert _has_historical_intent("PREVIOUSLY we fixed this") is True
        assert _has_historical_intent("Show RESOLVED entries") is True
        assert _has_historical_intent("RCA HISTORY for daemon") is True

    def test_partial_substring_matches(self):
        # "resolved entries" → matches "resolved entr" prefix
        assert _has_historical_intent("show me resolved entries") is True
        # "resolved entry" → matches too
        assert _has_historical_intent("look up the resolved entry") is True

    def test_active_word_does_not_falsely_match(self):
        # Conservative bypass — short non-historical text must not trigger.
        # `historian` contains the substring `histor` but not the full token
        # `history` / `historical` / `historically`, so it correctly does NOT
        # bypass the filter. This is by design — we lean toward false-negative
        # bypass over false-positive bypass.
        assert _has_historical_intent("this is fine") is False
        assert _has_historical_intent("the historian wrote it") is False
        assert _has_historical_intent("the actively-running daemon") is False
        assert _has_historical_intent("histogram of latency") is False


# ---------------------------------------------------------------------------
# _parse_entry_status / _parse_entry_updated_at
# ---------------------------------------------------------------------------


class TestParseStatus:
    def test_no_frontmatter_returns_none(self):
        body = "## Just a body\n\nNo frontmatter here.\n"
        assert _parse_entry_status(body) is None

    def test_no_status_field_returns_none(self):
        body = "---\ntitle: foo\nsummary: bar\n---\n\n## Body\n"
        assert _parse_entry_status(body) is None

    def test_unquoted_status(self):
        body = "---\nstatus: resolved\n---\n\nbody\n"
        assert _parse_entry_status(body) == "resolved"

    def test_single_quoted_status(self):
        body = "---\nstatus: 'resolved'\n---\n\nbody\n"
        assert _parse_entry_status(body) == "resolved"

    def test_double_quoted_status(self):
        body = '---\nstatus: "active"\n---\n\nbody\n'
        assert _parse_entry_status(body) == "active"

    def test_status_lowercase_normalization(self):
        body = "---\nstatus: 'RESOLVED'\n---\n\nbody\n"
        assert _parse_entry_status(body) == "resolved"

    def test_all_filtered_statuses_recognized(self):
        for status in ("resolved", "superseded", "candidate_resolved"):
            body = f"---\nstatus: {status}\n---\n\nbody\n"
            assert _parse_entry_status(body) == status
            assert _parse_entry_status(body) in _FILTERED_STATUSES

    def test_active_status_not_in_filter_set(self):
        body = "---\nstatus: active\n---\n\nbody\n"
        assert _parse_entry_status(body) == "active"
        assert _parse_entry_status(body) not in _FILTERED_STATUSES

    def test_garbage_status_value_returns_none_or_garbage(self):
        # Non-identifier garbage doesn't match the regex.
        body = "---\nstatus: !!!\n---\n\nbody\n"
        assert _parse_entry_status(body) is None


class TestParseUpdatedAt:
    def test_no_field_returns_none(self):
        body = "---\ntitle: foo\n---\n"
        assert _parse_entry_updated_at(body) is None

    def test_iso_with_z_returns_utc_aware(self):
        body = "---\nupdatedAt: '2026-05-26T10:00:00.000Z'\n---\n"
        ts = _parse_entry_updated_at(body)
        assert ts is not None
        assert ts.tzinfo is not None
        assert ts.year == 2026 and ts.month == 5 and ts.day == 26

    def test_iso_date_only(self):
        body = "---\nupdatedAt: '2026-05-26'\n---\n"
        ts = _parse_entry_updated_at(body)
        assert ts is not None
        assert ts.year == 2026

    def test_garbage_returns_none(self):
        body = "---\nupdatedAt: 'not-a-date'\n---\n"
        # Regex requires only [\dT:.\-Z+] characters — "not-a-date" fails the regex.
        # If regex did match, fromisoformat raises and we return None.
        assert _parse_entry_updated_at(body) is None


# ---------------------------------------------------------------------------
# _split_entries
# ---------------------------------------------------------------------------


class TestSplitEntries:
    def test_single_entry(self):
        out = _wrap(_entry("foo", "active"))
        entries = _split_entries(out)
        slugs = [s for s, _ in entries]
        assert "__preamble__" in slugs
        assert "foo" in slugs

    def test_multiple_entries_preserve_order(self):
        out = _wrap(
            _entry("alpha", "active"),
            _entry("beta", "resolved"),
            _entry("gamma", None),
        )
        entries = _split_entries(out)
        slugs = [s for s, _ in entries if s != "__preamble__"]
        assert slugs == ["alpha", "beta", "gamma"]

    def test_no_headers_returns_empty(self):
        # Output without `### slug` headers — caller treats as un-splittable.
        assert _split_entries("just a paragraph with no structure") == []
        assert _split_entries("") == []

    def test_slug_with_dots_and_slashes(self):
        # Slugs may include category paths like ai_ml/agent_evolution_loop
        out = "### ai_ml/agent_evolution_loop\n\n---\nstatus: active\n---\nbody\n"
        entries = _split_entries(out)
        slugs = [s for s, _ in entries if s != "__preamble__"]
        assert "ai_ml/agent_evolution_loop" in slugs


# ---------------------------------------------------------------------------
# _filter_resolved_entries
# ---------------------------------------------------------------------------


class TestFilterResolvedEntries:
    def test_filters_resolved(self):
        out = _wrap(
            _entry("active_one", "active"),
            _entry("resolved_one", "resolved"),
        )
        entries = _split_entries(out)
        kept, hidden = _filter_resolved_entries(entries, historical_intent=False)
        kept_slugs = [s for s, _ in kept]
        hidden_slugs = [s for s, _ in hidden]
        assert "active_one" in kept_slugs
        assert "resolved_one" in hidden_slugs

    def test_filters_all_three_status_values(self):
        out = _wrap(
            _entry("active_one", "active"),
            _entry("resolved_one", "resolved"),
            _entry("superseded_one", "superseded"),
            _entry("candidate_one", "candidate_resolved"),
        )
        entries = _split_entries(out)
        kept, hidden = _filter_resolved_entries(entries, historical_intent=False)
        kept_slugs = [s for s, _ in kept if s != "__preamble__"]
        hidden_slugs = [s for s, _ in hidden]
        assert kept_slugs == ["active_one"]
        assert set(hidden_slugs) == {"resolved_one", "superseded_one", "candidate_one"}

    def test_legacy_entries_without_status_kept(self):
        # Pre-schema-v1.1 entries have no `status:` field. They MUST be kept
        # (treated as `active`) so the filter is backward-compatible.
        out = _wrap(
            _entry("legacy_one", None),
            _entry("active_explicit", "active"),
        )
        entries = _split_entries(out)
        kept, hidden = _filter_resolved_entries(entries, historical_intent=False)
        kept_slugs = [s for s, _ in kept if s != "__preamble__"]
        assert set(kept_slugs) == {"legacy_one", "active_explicit"}
        assert hidden == []

    def test_historical_intent_disables_filter(self):
        out = _wrap(
            _entry("active_one", "active"),
            _entry("resolved_one", "resolved"),
            _entry("superseded_one", "superseded"),
        )
        entries = _split_entries(out)
        kept, hidden = _filter_resolved_entries(entries, historical_intent=True)
        kept_slugs = [s for s, _ in kept if s != "__preamble__"]
        assert set(kept_slugs) == {"active_one", "resolved_one", "superseded_one"}
        assert hidden == []

    def test_preamble_always_kept(self):
        out = _wrap(_entry("resolved_one", "resolved"))
        entries = _split_entries(out)
        kept, hidden = _filter_resolved_entries(entries, historical_intent=False)
        kept_slugs = [s for s, _ in kept]
        assert "__preamble__" in kept_slugs


# ---------------------------------------------------------------------------
# Full pipeline — _apply_schema_filter
# ---------------------------------------------------------------------------


class TestApplySchemaFilter:
    def test_drops_resolved_for_neutral_query(self, tmp_path: Path):
        """The dralexmorganbot regression test.

        A `[RESOLVED]` entry MUST NOT appear in recall when the query is the
        neutral phrase the user originally typed. This is the exact pattern
        that caused 12 turns of stale RCA injection.
        """
        out = _wrap(
            _entry(
                "dralexmorganbot_daemon_status",
                "resolved",
                summary="[RESOLVED 2026-05-14 commit bbb013d] Historical RCA",
            ),
            _entry("active_one", "active"),
        )
        filtered = _apply_schema_filter(
            raw_output=out,
            query="please fix this bug",
            brv_cwd=str(tmp_path),
        )
        assert "dralexmorganbot_daemon_status" not in filtered
        assert "active_one" in filtered
        assert "1 resolved" in filtered  # hidden-count note

    def test_keeps_resolved_when_historical_intent(self, tmp_path: Path):
        out = _wrap(
            _entry("dralexmorganbot_daemon_status", "resolved"),
            _entry("active_one", "active"),
        )
        filtered = _apply_schema_filter(
            raw_output=out,
            query="show me the history of dr alex daemon",
            brv_cwd=str(tmp_path),
        )
        assert "dralexmorganbot_daemon_status" in filtered
        assert "active_one" in filtered
        # No hidden-count note (filter bypassed).
        assert "hidden from this turn" not in filtered

    def test_unsplittable_output_returned_as_is(self, tmp_path: Path):
        """When output has no `### slug` headers (older brv versions, single result),
        return it unmodified — defensive backward compat."""
        raw = "Just a single blob of text without slug headers.\nMore content.\n"
        filtered = _apply_schema_filter(
            raw_output=raw,
            query="fix this",
            brv_cwd=str(tmp_path),
        )
        assert filtered == raw

    def test_filter_log_written_for_neutral_query(self, tmp_path: Path):
        out = _wrap(
            _entry("hidden_one", "resolved"),
            _entry("active_one", "active"),
        )
        _apply_schema_filter(
            raw_output=out,
            query="fix the bug",
            brv_cwd=str(tmp_path),
        )
        log_path = tmp_path / ".brv" / "filtered.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "hidden_one" in content
        assert "filter" in content
        assert "fix the bug" in content

    def test_filter_log_records_bypass_when_historical(self, tmp_path: Path):
        out = _wrap(
            _entry("entry_a", "resolved"),
            _entry("entry_b", "active"),
        )
        _apply_schema_filter(
            raw_output=out,
            query="show me the history",
            brv_cwd=str(tmp_path),
        )
        log_path = tmp_path / ".brv" / "filtered.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "bypass:historical" in content
        # No `filter\t` rows because nothing was filtered.
        assert "\tfilter\t" not in content

    def test_no_resolved_entries_no_log_writes(self, tmp_path: Path):
        out = _wrap(
            _entry("entry_a", "active"),
            _entry("entry_b", None),
        )
        _apply_schema_filter(
            raw_output=out,
            query="fix the bug",
            brv_cwd=str(tmp_path),
        )
        log_path = tmp_path / ".brv" / "filtered.log"
        # Nothing to log — no filter, no bypass.
        assert not log_path.exists() or log_path.read_text() == ""

    def test_log_truncates_long_query(self, tmp_path: Path):
        out = _wrap(_entry("hidden_one", "resolved"))
        long_query = "fix the daemon " * 200  # > 200 chars
        _apply_schema_filter(
            raw_output=out,
            query=long_query,
            brv_cwd=str(tmp_path),
        )
        log_path = tmp_path / ".brv" / "filtered.log"
        content = log_path.read_text()
        # Query column is truncated to ≤200 chars.
        # Check by parsing the TSV: ts<TAB>reason<TAB>query<TAB>slug<TAB>status
        parts = content.strip().split("\n")[0].split("\t")
        assert len(parts[2]) <= 200

    def test_recency_decay_promotes_recent_active_over_old_active(self, tmp_path: Path):
        """When two active entries match, the recent one should rank first.

        This protects against a 3-month-old similar-text match burying a
        freshly-amended entry.
        """
        recent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
        old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=120)
        out = _wrap(
            _entry("old_one", "active", updated_at=old.isoformat().replace("+00:00", "Z")),
            _entry("recent_one", "active", updated_at=recent.isoformat().replace("+00:00", "Z")),
        )
        filtered = _apply_schema_filter(
            raw_output=out,
            query="some query",
            brv_cwd=str(tmp_path),
        )
        # `recent_one` should appear before `old_one` in output.
        assert filtered.index("recent_one") < filtered.index("old_one")


# ---------------------------------------------------------------------------
# Regression smoke — actual dralexmorganbot pattern (mini-version)
# ---------------------------------------------------------------------------


class TestDralexmorganbotRegression:
    """The 12-turn stale RCA scenario this whole filter exists to prevent."""

    def test_neutral_bug_query_does_not_inject_resolved_rca(self, tmp_path: Path):
        # Synthetic but structurally identical to the real
        # dralexmorganbot_daemon_status.md entry after amendment.
        rca = """### dralexmorganbot_daemon_status

---
title: '[RESOLVED 2026-05-14] DrAlexMorganBot Daemon Status — Historical RCA'
summary: '[RESOLVED 2026-05-14 commit bbb013d] Historical RCA — daemon used to return canned string. Fixed by wiring C3LegacyAdapter. DO NOT TREAT AS ACTIVE BUG.'
status: resolved
resolution_commit: bbb013d
resolution_date: '2026-05-14'
updatedAt: '2026-05-26T16:20:00.000Z'
---

## RESOLUTION

Fix shipped in commit bbb013d. Body kept as historical.
"""
        active = """### unrelated_active_entry

---
title: Active fact about Bedrock
summary: Bedrock primary chain is opus-4-7
status: active
updatedAt: '2026-05-26T10:00:00.000Z'
---

body
"""
        raw = "**Summary**: Found 2 relevant topics:\n\n**Details**:\n\n" + rca + "\n---\n\n" + active

        for neutral_query in [
            "please fix this bug",
            "what's wrong with the daemon",
            "MockLegacyHandler issue",
            "explain the C3 wire-in",
            "fix",
        ]:
            filtered = _apply_schema_filter(
                raw_output=raw,
                query=neutral_query,
                brv_cwd=str(tmp_path),
            )
            assert "[RESOLVED" not in filtered, (
                f"Filter LEAKED resolved entry on query: {neutral_query!r}\n"
                f"Got: {filtered[:500]}"
            )
            assert "dralexmorganbot_daemon_status" not in filtered, (
                f"Filter LEAKED resolved slug on query: {neutral_query!r}"
            )
            assert "unrelated_active_entry" in filtered

    def test_historical_query_recovers_the_rca(self, tmp_path: Path):
        rca = """### dralexmorganbot_daemon_status

---
status: resolved
resolution_commit: bbb013d
updatedAt: '2026-05-26T16:20:00.000Z'
---

Historical body.
"""
        raw = "**Summary**: Found 1 relevant topic:\n\n**Details**:\n\n" + rca

        filtered = _apply_schema_filter(
            raw_output=raw,
            query="what's the history on the dralexmorganbot daemon issue",
            brv_cwd=str(tmp_path),
        )
        assert "dralexmorganbot_daemon_status" in filtered
        assert "bbb013d" in filtered
