"""Tests for the ByteRover plugin's schema v1.4 query-relevance gate.

The v1.4 gate fixes the QUERY-INDEPENDENT brv ranker: an off-topic query
("how to cook pasta carbonara") must not surface high-salience cron-log rows.
Two synchronous layers (structural class filter + lexical floor) + an optional
local-embedding re-rank (off by default) + an empty-floor (return nothing
rather than top-1).

Pure-Python — no brv subprocess, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.memory.byterover import (
    _STRUCTURAL_NOISE_RE,
    _apply_relevance_gate,
    _apply_schema_filter,
    _lexical_relevance,
    _split_entries,
    _tokenize,
)


def _entry(slug: str, summary: str = "test entry", body: str = "Some body content.",
           status: str = "active") -> str:
    return f"""### {slug}

---
title: '{slug} title'
summary: '{summary}'
status: {status}
createdAt: '2026-05-01T00:00:00.000Z'
updatedAt: '2026-06-15T10:00:00.000Z'
---

## Body

{body}
"""


def _wrap(*entries: str) -> str:
    out = f"**Summary**: Found {len(entries)} relevant topics:\n\n**Details**:\n\n"
    return out + "\n---\n\n".join(entries)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv("BRV_RELEVANCE_GATE", "1")
    monkeypatch.setenv("BRV_EMBED_RERANK", "0")  # keep deterministic; no network


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------
class TestTokenize:
    def test_stopwords_removed(self):
        toks = _tokenize("how to cook the pasta")
        assert "how" not in toks and "the" not in toks
        assert "cook" in toks and "pasta" in toks

    def test_short_tokens_dropped(self):
        assert "t1" not in _tokenize("t1 do a bc")  # len<3 dropped
        assert "abc" in _tokenize("abc def")

    def test_empty(self):
        assert _tokenize("") == set()


# ---------------------------------------------------------------------------
# _lexical_relevance
# ---------------------------------------------------------------------------
class TestLexicalRelevance:
    def test_offtopic_scores_zero(self):
        qt = _tokenize("how to cook pasta carbonara")
        score = _lexical_relevance(qt, "parity_branch_rebase_session_state_43",
                                   "branch rebase parity session model state")
        assert score == 0.0

    def test_ontopic_scores_high(self):
        qt = _tokenize("gbrain two brain postgres architecture")
        score = _lexical_relevance(qt, "gbrain_postgres_architecture",
                                   "gbrain runs on postgres with pgvector architecture")
        assert score >= 0.15

    def test_slug_match_gets_bonus(self):
        qt = _tokenize("byterover relevance gate fix")
        with_slug = _lexical_relevance(qt, "byterover_relevance_gate", "the fix")
        without_slug = _lexical_relevance(qt, "unrelated_slug", "byterover relevance gate")
        assert with_slug >= without_slug

    def test_empty_query_defensive_keep(self):
        # No usable query tokens -> don't gate (return 1.0, keep brv order)
        assert _lexical_relevance(set(), "any_slug", "any body") == 1.0


# ---------------------------------------------------------------------------
# _STRUCTURAL_NOISE_RE — catches cron-noise class, spares legit dated notes
# ---------------------------------------------------------------------------
class TestStructuralNoiseRegex:
    @pytest.mark.parametrize("slug", [
        "sysdesign_profile_rubric_evaluation_result",
        "dr_alex_coder_profile_rubric_evaluator_code_correctness_only_jun_1_2026",
        "active_model_jun_11_2026_parity_branch_rebase_session_state_43",
        "active_model_jun_11_2026_fable5_config_state",
        "dralexmorganbot_daemon_status",
        "click_probability_grading_result_may_16_2026_aa3",
        "gbrain_phase15_council_round2_schema_v1_2_may_26_2026",
        "skill_library_update_session_may_14_2026_daemon_restart",
    ])
    def test_matches_cron_noise(self, slug):
        assert _STRUCTURAL_NOISE_RE.search(slug), f"should match noise: {slug}"

    @pytest.mark.parametrize("slug", [
        "praxvault_gbrain_obsidian_integration_adr_may_2026",   # legit ADR w/ date
        "therapy_stack_architecture_verified_inventory_may_2026",  # legit inventory w/ date
        "claude_opus_4_8_bedrock_migration_verified_may_29_2026",  # legit migration note
        "two_brain_architecture",
        "agent_skills/gbrain",
    ])
    def test_spares_legit_dated_notes(self, slug):
        assert not _STRUCTURAL_NOISE_RE.search(slug), f"should NOT match legit: {slug}"


# ---------------------------------------------------------------------------
# _apply_relevance_gate
# ---------------------------------------------------------------------------
class TestRelevanceGate:
    def test_drops_offtopic_keeps_preamble(self):
        kept = [
            ("__preamble__", "**Summary**: Found 2"),
            ("parity_branch_rebase_session_state_43", "branch rebase model"),
            ("dralexmorganbot_daemon_status", "daemon healthy"),
        ]
        relevant, dropped = _apply_relevance_gate(kept, "how to cook pasta carbonara")
        assert relevant == [("__preamble__", "**Summary**: Found 2")]
        assert len(dropped) == 2

    def test_keeps_relevant(self):
        kept = [
            ("__preamble__", "x"),
            ("gbrain_postgres_setup", "gbrain uses postgres and pgvector for the graph"),
            ("unrelated_cooking_note", "how to make risotto with parmesan"),
        ]
        relevant, dropped = _apply_relevance_gate(kept, "gbrain postgres pgvector graph")
        relevant_slugs = [s for s, _ in relevant]
        assert "gbrain_postgres_setup" in relevant_slugs
        assert "unrelated_cooking_note" not in relevant_slugs


# ---------------------------------------------------------------------------
# _split_entries — frontmatter discriminator (section-header bug fix)
# ---------------------------------------------------------------------------
class TestSplitEntriesFrontmatterDiscriminator:
    def test_section_subheaders_not_treated_as_entries(self):
        raw = _wrap(_entry("real_entry_one",
                           body="## Reason\nstuff\n\n### Structure\nThe design.\n\n### Rules\nScore 10."))
        entries = _split_entries(raw)
        slugs = [s for s, _ in entries if s != "__preamble__"]
        # Only the REAL entry header (followed by ---), NOT Structure/Rules
        assert slugs == ["real_entry_one"]

    def test_multiple_real_entries(self):
        raw = _wrap(_entry("entry_a", body="### Highlights\nfoo"),
                    _entry("entry_b", body="### Examples\nbar"))
        slugs = [s for s, _ in _split_entries(raw) if s != "__preamble__"]
        assert slugs == ["entry_a", "entry_b"]

    def test_no_real_headers_returns_empty(self):
        # headers present but none followed by frontmatter -> unsplittable
        raw = "### Structure\nprose\n\n### Rules\nmore prose\n"
        assert _split_entries(raw) == []


# ---------------------------------------------------------------------------
# _apply_schema_filter — end-to-end empty-floor
# ---------------------------------------------------------------------------
class TestEmptyFloor:
    def test_offtopic_query_returns_empty(self, tmp_path: Path):
        out = _wrap(
            _entry("parity_branch_rebase_session_state_43", body="branch rebase model state"),
            _entry("dralexmorganbot_daemon_status", body="daemon is healthy and running"),
        )
        filtered = _apply_schema_filter(raw_output=out, query="how to cook pasta carbonara",
                                        brv_cwd=str(tmp_path))
        assert filtered == "", "off-topic recall must collapse to empty (empty > top-1)"

    def test_relevant_query_keeps_match(self, tmp_path: Path):
        out = _wrap(
            _entry("gbrain_postgres_architecture",
                   summary="gbrain on postgres pgvector",
                   body="gbrain uses native postgres and pgvector for graph retrieval"),
            _entry("parity_branch_rebase_session_state_43", body="unrelated branch state"),
        )
        filtered = _apply_schema_filter(raw_output=out,
                                        query="gbrain postgres pgvector architecture",
                                        brv_cwd=str(tmp_path))
        assert "gbrain_postgres_architecture" in filtered
        assert "parity_branch_rebase" not in filtered

    def test_historical_intent_bypasses_gate(self, tmp_path: Path):
        out = _wrap(_entry("parity_branch_rebase_session_state_43", body="branch state"))
        # historical intent -> gate bypassed, entry surfaces even though off-topic
        filtered = _apply_schema_filter(raw_output=out,
                                        query="show me the history of what we did",
                                        brv_cwd=str(tmp_path))
        assert "parity_branch_rebase_session_state_43" in filtered

    def test_kill_switch_disables_gate(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BRV_RELEVANCE_GATE", "0")
        out = _wrap(_entry("totally_unrelated_note", body="nothing matches the query here"))
        filtered = _apply_schema_filter(raw_output=out, query="xyzzy plugh foobar quux",
                                        brv_cwd=str(tmp_path))
        # gate off -> entry still present (only resolved/noise filters apply)
        assert "totally_unrelated_note" in filtered
