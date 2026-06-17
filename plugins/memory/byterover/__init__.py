"""ByteRover memory plugin — MemoryProvider interface.

Persistent memory via the ByteRover CLI (``brv``). Organizes knowledge into
a hierarchical context tree with tiered retrieval (fuzzy text → LLM-driven
search). Local-first with optional cloud sync.

Original PR #3499 by hieuntg81, adapted to MemoryProvider ABC.

Requires: ``brv`` CLI installed (npm install -g byterover-cli or
curl -fsSL https://byterover.dev/install.sh | sh).

Config via environment variables (profile-scoped via each profile's .env):
  BRV_API_KEY   — ByteRover API key (for cloud features, optional for local)

Config via config.yaml:
  memory:
    byterover:
      auto_extract: false  # disable automatic brv curate hooks

Working directory: $HERMES_HOME/byterover/ (profile-scoped context tree)

Schema v1.1 (locked 2026-05-26 — see ~/.hermes/byterover/SCHEMA.md):
  Each parent .md entry MAY carry frontmatter fields:
    kind:   incident | decision | fact | session-output | reference
    status: active | resolved | superseded | candidate_resolved
  prefetch() filters out resolved / superseded / candidate_resolved entries
  from auto-recall unless the user query contains a historical-intent signal.
  All filtering decisions are logged to ~/.hermes/byterover/.brv/filtered.log
  for 30-day audit. Triggered by 12-turn dralexmorganbot RCA stale-recall
  incident — full design in ~/Documents/workspace/byterover-fix/.learnings/
  2026-05-26-phase-C-council-outputs/.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Timeouts
_QUERY_TIMEOUT = 10   # brv query — should be fast
_CURATE_TIMEOUT = 120  # brv curate — may involve LLM processing

# Minimum lengths to filter noise
_MIN_QUERY_LEN = 10
_MIN_OUTPUT_LEN = 20

# ---------------------------------------------------------------------------
# Schema v1.1 — entry lifecycle filter (locked 2026-05-26)
# See ~/.hermes/byterover/SCHEMA.md for the full contract.
# ---------------------------------------------------------------------------

# Statuses that are filtered out of auto-recall unless query carries
# explicit historical-intent signal. Matches lowercase YAML scalar values.
_FILTERED_STATUSES = frozenset({"resolved", "superseded", "candidate_resolved"})

# Schema v1.3 (2026-06-15) — NOISE-CATEGORY auto-recall suppression.
# The status filter (v1.1) only drops resolved/superseded entries. But the tree is
# dominated by ACTIVE cron-curation noise from OTHER profiles (Dr Alex rubric evaluators,
# agent-evolution daemon-status logs, per-session parity-rebase state snapshots). These are
# `status: active` so v1.1 lets them through, and they keyword-match almost any technical
# message -> injected every turn as irrelevant <memory-context>. v1.3 drops entries whose
# SLUG matches a noise pattern from AUTO-recall (prefetch) only. They remain fully reachable
# via the explicit brv_query tool and via historical-intent queries. Kill switch:
# BRV_NOISE_FILTER=0. Patterns are substrings matched against the lowercased slug.
_NOISE_SLUG_PATTERNS = (
    "rubric_evaluator",          # dr_alex_coder_profile_rubric_evaluator_*
    "rubric_evaluation",         # *_rubric_evaluation_result / _may_20_2026
    "rubric_may", "rubric_jun",  # dated rubric snapshots
    "_profile_rubric",
    "quality_rubric",            # dsa_prep_socratic_quality_rubric_*
    "quality_score",             # dsa_prep_socratic_quality_score_*
    "quality_evaluator", "quality_evaluation",
    "socratic_quality",          # dr_alex_dsa_prep_socratic_quality_*
    "evaluator_prompt",          # *_rubric_evaluator_prompt
    "evaluation_result",
    "daemon_status",             # dralexmorganbot_daemon_status
    "session_state",             # active_model_*_session_state_NN
    "session_recap",
    "_canary_verdict",
    "agent_evolution_loop",      # evolve-cron dumps
    "evolve_run", "evolve_cycle", "evolve_session",
    "_shadow_run", "gepa_shadow",
)


def _noise_filter_enabled() -> bool:
    """Return False iff BRV_NOISE_FILTER explicitly disables the v1.3 noise filter."""
    val = os.environ.get("BRV_NOISE_FILTER", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def _is_noise_slug(slug: str) -> bool:
    """True if a slug is cron-curation noise that should not AUTO-recall."""
    s = (slug or "").lower()
    return any(pat in s for pat in _NOISE_SLUG_PATTERNS)

# Substrings (case-insensitive) that bypass the filter when present in
# the user's query. Conservative wordlist — false-positive bypass costs
# stale recall, false-negative bypass costs hidden history. Tuned toward
# the latter; users can always use the brv_query tool directly.
_HISTORICAL_INTENT_TOKENS = (
    "previously",
    "previously,",
    "previous run",
    "history",
    "historical",
    "historically",
    "in the past",
    "past run",
    "old ",                # "old daemon", "old bug" — natural historical phrasing
    "old rca",
    "already fixed",
    "already shipped",
    "resolved entr",       # "resolved entry" / "resolved entries"
    "archived",
    "archive ",
    "when did we",
    "what did we",         # complements "when did we" — past-tense intent
    "what did we ship",
    "what we tried",       # past-tense indicator
    "used to",             # "this used to work" — historical intent
    "earlier",             # "earlier version" / "earlier commit"
    "yesterday",
    "last week",
    "last month",
    "forgotten",           # "what did we forget" — past-perfect indicator
    "backed out",          # "we backed out the change"
    "reverted",            # "what got reverted"
    "show resolved",
    "show all",
    "include resolved",
    "include history",
    "rca history",
)

# Header pattern — entries in `brv query` output are separated by `### <slug>`.
# `slug` is fuzzy: alnum + underscore + dash + slash + period (matches
# typical generated slugs). Anchored to start-of-line.
_ENTRY_HEADER_RE = re.compile(r"^### (?P<slug>[\w./\-]+)\s*$", re.MULTILINE)

# YAML frontmatter block — opens after first triple-dash line, closes at next.
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<body>.*?)\n---\s*$", re.MULTILINE | re.DOTALL
)

# ---------------------------------------------------------------------------
# Schema v1.2 — Layer 4 context-fencing markers
# (introduced 2026-05-26 to plug recursive-pollution write-path)
# ---------------------------------------------------------------------------

# HTML-comment markers wrapped around prefetch() output. Invisible in markdown
# rendering, deterministic for fence regex anchoring. The architecture-critic's
# H2/H3 findings (block-terminator under/over-strip, nested ### sub-headers)
# are only solved by a structural marker, not by per-slug regex.
_FENCE_BEGIN_MARKER = "<!-- byterover-context-begin -->"
_FENCE_END_MARKER = "<!-- byterover-context-end -->"

# Sanitized escapes for marker-in-content collision (council Round 3 HIGH-2).
# When a curated entry body itself contains the begin/end markers (e.g. a
# learning ABOUT the fence design — exactly the project's own self-write
# pattern), wrapping in prefetch() would yield a malformed nested block where
# the strip regex's non-greedy match terminates on the embedded END marker
# and leaks the tail. We replace embedded markers with these zero-width
# escape variants BEFORE wrapping.
_FENCE_BEGIN_ESCAPE = "<!-- byterover-context-begin-ZWNJ -->"
_FENCE_END_ESCAPE = "<!-- byterover-context-end-ZWNJ -->"

# Strip the WHOLE fenced block in one operation. The block contains the recall
# preamble + entries; everything outside the markers is preserved verbatim.
_FENCE_BLOCK_RE = re.compile(
    re.escape(_FENCE_BEGIN_MARKER)
    + r"[\s\S]*?"
    + re.escape(_FENCE_END_MARKER)
    + r"\n?",
    re.MULTILINE,
)

# Defensive sweep regex: an unmatched begin marker (truncation, partial write,
# adversarial chat-of-thought) without a paired end marker. Strips the whole
# line containing the orphan begin marker so it doesn't leak through.
_ORPHAN_BEGIN_RE = re.compile(
    r".*" + re.escape(_FENCE_BEGIN_MARKER) + r".*\n?",
    re.MULTILINE,
)

# Bound on _last_prefetched_slugs to keep memory + audit-log line size finite.
# (Audit-only attribution; structural strip is fence source-of-truth.)
_MAX_TRACKED_SLUGS = 50

# Cap slug-csv field in audit log so each line stays bounded. POSIX
# write atomicity on regular O_APPEND files is filesystem-dependent (ext4,
# APFS, ZFS all give per-write atomicity for short writes; PIPE_BUF only
# applies to pipes/FIFOs). 50 slugs at ~50 chars each is ~2.5 KB, plus
# timestamp/session_id/chars-stripped/flag = under 3 KB total — well below
# any plausible filesystem write-atomicity boundary on macOS or Linux.
_AUDIT_LOG_SLUG_CSV_MAX = 50

# Feature flag: env-var kill switch (council Round 3 HIGH-1). When set to
# any of {"0", "false", "no", "off"} (case-insensitive), the fence is
# bypassed BYTE-FOR-BYTE: prefetch() does not inject markers, and
# _apply_context_fence() returns input unchanged. Use this to revert
# behavior without a code-revert during the 7-day rollout audit window.
def _fencing_enabled() -> bool:
    """Return False iff BRV_CONTEXT_FENCING explicitly disables fencing."""
    val = os.environ.get("BRV_CONTEXT_FENCING", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}

# Frontmatter scalar field — `status: <value>` on its own line. Tolerates
# single quotes, double quotes, or no quotes around the value.
_STATUS_FIELD_RE = re.compile(
    r"""^status:\s*['"]?(?P<value>[A-Za-z_][A-Za-z0-9_-]*)['"]?\s*$""",
    re.MULTILINE,
)

# Frontmatter `updatedAt` field — ISO 8601, optionally quoted.
_UPDATED_AT_RE = re.compile(
    r"""^updatedAt:\s*['"]?(?P<ts>[\dT:.\-Z+]+)['"]?\s*$""",
    re.MULTILINE,
)


def _filter_log_path(brv_cwd: str) -> Path:
    """Resolve the filter audit log path. Lives next to the context-tree."""
    return Path(brv_cwd) / ".brv" / "filtered.log"


def _has_historical_intent(query: str) -> bool:
    """Return True if the query carries an explicit historical-intent signal.

    Schema v1.1 contract: when this is True, the resolved/superseded filter
    is bypassed and the agent sees historical entries in recall.
    """
    if not query:
        return False
    q = query.lower()
    return any(tok in q for tok in _HISTORICAL_INTENT_TOKENS)


def _parse_entry_status(entry_body: str) -> Optional[str]:
    """Extract the lowercase `status:` value from a single entry's body.

    Returns None when no frontmatter or no `status:` field is present —
    treated as `active` (default) by callers.
    """
    fm_match = _FRONTMATTER_RE.search(entry_body)
    if not fm_match:
        return None
    fm = fm_match.group("body")
    status_match = _STATUS_FIELD_RE.search(fm)
    if not status_match:
        return None
    return status_match.group("value").strip().lower()


def _parse_entry_updated_at(entry_body: str) -> Optional[_dt.datetime]:
    """Extract the `updatedAt:` ISO timestamp. Returns None when absent or unparseable."""
    fm_match = _FRONTMATTER_RE.search(entry_body)
    if not fm_match:
        return None
    fm = fm_match.group("body")
    ts_match = _UPDATED_AT_RE.search(fm)
    if not ts_match:
        return None
    raw = ts_match.group("ts").strip()
    # Accept both `2026-05-14T06:07:33.194Z` and `2026-05-14`.
    try:
        if raw.endswith("Z"):
            return _dt.datetime.fromisoformat(raw[:-1]).replace(tzinfo=_dt.timezone.utc)
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def _split_entries(query_output: str) -> List[Tuple[str, str]]:
    """Split brv query output into [(slug, body)] tuples.

    Header lines `### <slug>` separate entries, BUT only when the header is a
    REAL entry header — byterover emits every entry as `### <slug>\\n---\\n<yaml
    frontmatter>\\n---\\n<body>`. Section SUB-headers inside an entry body
    (`### Structure`, `### Dependencies`, `### Highlights`, `### Rules`,
    `### Examples`, `### Narrative` — from the `.overview.md` companions) ALSO
    match `### <word>` but are followed by PROSE, not a `---` frontmatter block.
    Treating them as entry boundaries fragments a single entry into pieces (a
    pre-existing splitter bug, latent until the v1.4 relevance gate began
    scoring per-fragment). The discriminator is exact and verified across the
    tree: a real entry header is immediately followed by a `---` line; a
    sub-header is not.

    The first chunk before the first REAL header (typically
    `**Summary**: Found N relevant topics...`) is returned with slug
    `__preamble__` so callers can preserve it.

    Returns an empty list if no real headers found — caller should treat the
    whole output as a single un-splittable blob.
    """
    all_headers = list(_ENTRY_HEADER_RE.finditer(query_output))
    # Keep only headers immediately followed by a YAML frontmatter block.
    headers = []
    for m in all_headers:
        after = query_output[m.end():].lstrip("\n")
        if after.startswith("---"):
            headers.append(m)
    if not headers:
        return []

    entries: List[Tuple[str, str]] = []
    # Preamble (everything before first real ### header)
    preamble = query_output[: headers[0].start()].rstrip()
    if preamble:
        entries.append(("__preamble__", preamble))

    for idx, m in enumerate(headers):
        slug = m.group("slug")
        start = m.end()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(query_output)
        body = query_output[start:end].strip()
        entries.append((slug, body))

    return entries


def _apply_recency_decay(entries: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Re-rank entries by recency (newer entries first).

    `brv query` returns entries by similarity. Schema v1.1 contract layers
    recency on top: `effective_score = similarity * exp(-days_old / 30)`.
    Since we don't have raw similarity scores from brv (only an ordered list),
    we approximate by reversing order on tie and breaking ties with `updatedAt`.

    Conservative implementation: preserve brv's relative ordering, but
    promote entries with explicit `updatedAt` newer than 7 days, so that
    a freshly-amended `[RESOLVED]` entry doesn't get buried by a 3-month-old
    similar-text match.

    Legacy entries with NO `updatedAt` field are assigned a synthetic age
    of 90 days (mid-decay zone). This is intentional: a brand-new entry
    without a timestamp would otherwise rank either at the very top
    (treated as 0 days old) or the very bottom (treated as infinitely
    old) — neither is honest. 90 days lands in the middle of the decay
    curve, neither promoted nor buried.

    The preamble (slug=__preamble__) is preserved at index 0.
    """
    if not entries:
        return entries

    now = _dt.datetime.now(_dt.timezone.utc)

    def key(item: Tuple[str, str]) -> Tuple[int, float]:
        slug, body = item
        if slug == "__preamble__":
            # Always first.
            return (-1, 0.0)
        ts = _parse_entry_updated_at(body)
        if ts is None:
            # Unknown age → treat as 90 days old (mid-decay).
            days = 90.0
        else:
            days = max(0.0, (now - ts).total_seconds() / 86400.0)
        # Smaller key = better. We negate the decay factor.
        decay = math.exp(-days / 30.0)
        return (0, -decay)

    return sorted(entries, key=key)


def _filter_resolved_entries(
    entries: List[Tuple[str, str]],
    historical_intent: bool,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Partition entries into (kept, hidden).

    When `historical_intent` is True, NO filtering happens — all entries are
    kept. The audit log records the bypass.

    Otherwise, entries whose `status:` field is in _FILTERED_STATUSES are
    moved to `hidden`. Entries with no status field (legacy / pre-schema-v1.1)
    are kept (treated as `active`).
    """
    if historical_intent:
        return entries, []

    kept: List[Tuple[str, str]] = []
    hidden: List[Tuple[str, str]] = []

    for slug, body in entries:
        if slug == "__preamble__":
            kept.append((slug, body))
            continue
        status = _parse_entry_status(body)
        if status in _FILTERED_STATUSES:
            hidden.append((slug, body))
        else:
            kept.append((slug, body))

    return kept, hidden


def _reassemble_output(kept: List[Tuple[str, str]], hidden_count: int) -> str:
    """Rebuild brv-shaped markdown output from the kept entries.

    Appends a `(N hidden)` note when `hidden_count > 0` so the agent stays
    aware that history was filtered without auto-injecting stale content.
    """
    parts: List[str] = []
    for slug, body in kept:
        if slug == "__preamble__":
            parts.append(body)
        else:
            parts.append(f"### {slug}")
            parts.append(body)
        parts.append("")  # blank line between entries

    if hidden_count > 0:
        parts.append(
            f"\n_({hidden_count} resolved/superseded/candidate-resolved "
            f"{'entry' if hidden_count == 1 else 'entries'} hidden from this turn's "
            f"recall — include 'previously', 'history', 'archive', or 'show resolved' "
            f"in the query to surface.)_"
        )

    return "\n".join(parts).strip()


def _log_filter_decision(
    brv_cwd: str,
    query: str,
    hidden_entries: List[Tuple[str, str]],
    bypassed: bool,
) -> None:
    """Append-only filter-decision log for 30-day audit.

    Format (TSV per line):
      <ISO 8601>\t<reason>\t<query-truncated>\t<slug>\t<status>

    Reason is one of:
      filter            — entry filtered (status in _FILTERED_STATUSES)
      bypass:historical — query carried historical-intent signal, all entries kept

    Failures here are swallowed — logging is best-effort, must not break
    prefetch.
    """
    try:
        log_path = _filter_log_path(brv_cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        q_short = (query or "")[:200].replace("\t", " ").replace("\n", " ")

        lines = []
        if bypassed:
            lines.append(f"{ts}\tbypass:historical\t{q_short}\t-\t-")
        for slug, body in hidden_entries:
            status = _parse_entry_status(body) or "unknown"
            lines.append(f"{ts}\tfilter\t{q_short}\t{slug}\t{status}")

        if lines:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.debug("ByteRover filter-log write failed: %s", e)


# ---------------------------------------------------------------------------
# Schema v1.4 (2026-06-17) — QUERY-RELEVANCE GATE.
#
# Root cause this fixes: `brv query` is QUERY-INDEPENDENT. Empirically, an
# off-topic query ("how to cook pasta carbonara") returns the same high-salience
# cron-log rows (parity-rebase session state, daemon status) as any technical
# query. The v1.1 status filter and v1.3 noise denylist are partial — new
# cron-noise slug shapes (grading_result, council_round2, *_config_state,
# skill_library_update_session) keep outrunning the denylist.
#
# Two layers, both SYNCHRONOUS and INSTANT (no network on the hot prefetch path):
#   (a) STRUCTURAL pre-filter — a regex catching the cron-noise CLASS (dated
#       snapshots + profile-eval/result/state/status slug shapes), so we don't
#       chase individual slug strings forever. Defense-in-depth over v1.3.
#   (b) LEXICAL relevance gate — token overlap between the query and each
#       entry's (slug + body), stopworded. Drops entries with near-zero overlap.
#       This is the query-dependence the brv ranker lacks. Catches the
#       pasta-carbonara→parity-logs case with ZERO latency.
#
# OPTIONAL (off by default, BRV_EMBED_RERANK=1): a local-ollama embedding
# re-rank of the lexical survivors (nomic-embed-text, content-hash cached).
# Kept off by default because the prefetch path is synchronous and blocks the
# first LLM call — a per-turn network/subprocess embed hop (~0.1-1.5s) is a
# latency regression most turns don't need. The lexical gate already solves the
# total-irrelevance bug; embeddings only add synonym/paraphrase recall, which is
# an opt-in upgrade, not a default tax.
#
# Empty-floor: if NOTHING clears the gate, return preamble-only (effectively
# empty recall). Council 2/2 unanimous: empty beats top-1 (top-1 on an
# off-topic query is GUARANTEED to be the highest-salience cron row — i.e. it
# reproduces the exact bug). Kill switch: BRV_RELEVANCE_GATE=0.
# ---------------------------------------------------------------------------

# Structural noise: profile-eval/state slug shapes. We do NOT treat a bare
# date-stamp (_may_2026, _2026_05_28) as noise — many LEGITIMATE notes carry
# dates (ADRs, verified-inventory snapshots). Only the genuine cron-curation
# CLASS qualifies: rubric/eval/grading/score *results* and *evaluators*,
# session/config/daemon STATE+STATUS+RECAP rows, parity-rebase logs, council
# rounds, and skill-library session dumps. Verified against real entries
# 2026-06-17 (praxvault_gbrain_..._adr_may_2026 must NOT match).
_STRUCTURAL_NOISE_RE = re.compile(
    r"(?:"
    r"(?:rubric|grading|eval|evaluation|quality|score|scoring)_(?:result|evaluator|score|eval|evaluation)"
    r"|profile_rubric"                                             # *_profile_rubric_evaluation
    r"|rubric_(?:evaluation|evaluator|result)"
    r"|_(?:config_state|session_state|session_status|daemon_status|daemon_session_status|session_recap|canary_verdict)"
    r"|click_probability_grading"
    r"|parity_branch_rebase"
    r"|skill_library_(?:update|session)"
    r"|council_round\d"
    r")",
    re.IGNORECASE,
)

# Stopwords for the lexical gate — common English + brv/markdown structural tokens.
_GATE_STOPWORDS = frozenset("""
a an the and or but is are was were be been being to of in on at for with from by as it its
this that these those i you he she we they my your our their what which who how why when where
do does did done can could should would will shall may might must have has had not no yes if then
than into out up down over under about above below back more most some any all each every
result results structure dependencies highlights rules examples narrative reason summary task
changes flow timestamp author md status active jun may session state config
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _relevance_gate_enabled() -> bool:
    val = os.environ.get("BRV_RELEVANCE_GATE", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _embed_rerank_enabled() -> bool:
    val = os.environ.get("BRV_EMBED_RERANK", "0").strip().lower()
    return val in {"1", "true", "yes", "on"}


def _tokenize(text: str) -> set:
    """Lowercase alnum tokens, stopworded, length>=3 (drops 'a', 'to', ids like 't1')."""
    if not text:
        return set()
    toks = _TOKEN_RE.findall(text.lower())
    return {t for t in toks if len(t) >= 3 and t not in _GATE_STOPWORDS}


def _lexical_relevance(query_tokens: set, slug: str, body: str) -> float:
    """Overlap score in [0,1]: fraction of QUERY tokens present in the entry.

    Slug tokens count double-weight (a slug match is a strong signal). We score
    against the query's token set (not the entry's) so a long noisy entry can't
    dilute a real match, and a terse entry can't be unfairly penalized.
    """
    if not query_tokens:
        return 1.0  # no usable query signal -> don't gate (defensive: keep brv order)
    slug_tokens = _tokenize(slug.replace("/", " ").replace("-", " ").replace("_", " "))
    # Body: only the first ~600 chars (summary/highlights carry the signal; full
    # body is mostly boilerplate sections that we stopword out anyway).
    body_tokens = _tokenize(body[:600])
    entry_tokens = slug_tokens | body_tokens
    if not entry_tokens:
        return 0.0
    matched = query_tokens & entry_tokens
    slug_matched = query_tokens & slug_tokens
    # base = fraction of query tokens found anywhere; bonus for slug hits.
    base = len(matched) / len(query_tokens)
    slug_bonus = 0.25 * (len(slug_matched) / len(query_tokens))
    return min(1.0, base + slug_bonus)


# Lexical floor: an entry must share at least this fraction of query tokens.
# Tuned on the empirical probes: "pasta carbonara" vs cron rows scores 0.0;
# a real on-topic query scores >> 0.15. Conservative (low) to avoid dropping
# genuine-but-terse matches; the structural filter + status/noise filters do
# the heavy lifting, this gate just kills TOTAL irrelevance.
_LEXICAL_FLOOR = 0.15


def _apply_relevance_gate(
    kept: List[Tuple[str, str]],
    query: str,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """Partition kept entries into (relevant, irrelevant) by the lexical gate.

    Preamble is always retained in `relevant`. Structural-noise slugs are
    dropped first (cheap), then the lexical floor is applied. Returns the
    survivors and the dropped set (for audit + empty-floor accounting).
    """
    query_tokens = _tokenize(query)
    relevant: List[Tuple[str, str]] = []
    dropped: List[Tuple[str, str]] = []
    for slug, body in kept:
        if slug == "__preamble__":
            relevant.append((slug, body))
            continue
        # (a) structural class filter
        if _STRUCTURAL_NOISE_RE.search(slug):
            dropped.append((slug, body))
            continue
        # (b) lexical floor
        score = _lexical_relevance(query_tokens, slug, body)
        if score < _LEXICAL_FLOOR:
            dropped.append((slug, body))
        else:
            relevant.append((slug, body))
    return relevant, dropped


def _embed_rerank(relevant: List[Tuple[str, str]], query: str) -> List[Tuple[str, str]]:
    """OPTIONAL local-ollama embedding re-rank of lexical survivors.

    Off by default (BRV_EMBED_RERANK). Uses ollama nomic-embed-text (local, no
    network egress). Best-effort: on ANY failure (ollama down, timeout) returns
    the input order unchanged — never breaks prefetch. Preamble stays at index 0.
    """
    non_preamble = [(s, b) for s, b in relevant if s != "__preamble__"]
    preamble = [(s, b) for s, b in relevant if s == "__preamble__"]
    if len(non_preamble) < 2:
        return relevant
    try:
        import urllib.request

        def embed(text: str) -> Optional[List[float]]:
            payload = json.dumps({"model": "nomic-embed-text", "prompt": text[:2000]}).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/embeddings", data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read()).get("embedding")

        qv = embed(query)
        if not qv:
            return relevant

        def cos(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        scored = []
        for slug, body in non_preamble:
            ev = embed(f"{slug} {body[:600]}")
            scored.append((cos(qv, ev) if ev else 0.0, slug, body))
        scored.sort(key=lambda t: -t[0])
        return preamble + [(s, b) for _, s, b in scored]
    except Exception as e:
        logger.debug("ByteRover embed-rerank skipped: %s", e)
        return relevant


def _apply_schema_filter(
    raw_output: str,
    query: str,
    brv_cwd: str,
) -> str:
    """Run schema v1.1 filter pipeline on raw `brv query` markdown output.

    Pipeline:
      1. Split output into per-entry tuples.
      2. Apply recency decay (re-rank within kept set).
      3. Filter resolved / superseded / candidate_resolved unless historical-intent.
      4. Append `(N hidden)` note.
      5. Log decisions for audit.

    If the output is un-splittable (no `### <slug>` headers — older brv versions,
    truncated output, single-entry result), the original output is returned
    unmodified. This keeps the patch defensively backward-compatible.
    """
    entries = _split_entries(raw_output)
    if not entries:
        # Un-splittable; return as-is.
        return raw_output

    historical = _has_historical_intent(query)
    entries = _apply_recency_decay(entries)
    kept, hidden = _filter_resolved_entries(entries, historical_intent=historical)

    # Schema v1.3 — drop noise-category slugs from AUTO-recall (unless historical-intent
    # or kill-switched). These stay reachable via the explicit brv_query tool.
    noise_hidden: List[Tuple[str, str]] = []
    if _noise_filter_enabled() and not historical:
        filtered_kept: List[Tuple[str, str]] = []
        for slug, body in kept:
            if slug != "__preamble__" and _is_noise_slug(slug):
                noise_hidden.append((slug, body))
            else:
                filtered_kept.append((slug, body))
        kept = filtered_kept

    # Schema v1.4 — QUERY-RELEVANCE GATE (structural class filter + lexical floor).
    # This is the fix for the query-independent brv ranker: an off-topic query
    # must not surface high-salience cron rows. Empty-floor: if nothing clears
    # the gate, recall collapses to preamble-only (empty beats top-1). Bypassed
    # on historical-intent (the user explicitly wants history) or kill switch.
    gate_hidden: List[Tuple[str, str]] = []
    if _relevance_gate_enabled() and not historical:
        relevant, gate_hidden = _apply_relevance_gate(kept, query)
        # OPTIONAL: local-ollama embedding re-rank of survivors (default OFF).
        if _embed_rerank_enabled() and len(relevant) > 2:
            relevant = _embed_rerank(relevant, query)
        kept = relevant

    _log_filter_decision(
        brv_cwd=brv_cwd,
        query=query,
        hidden_entries=hidden + noise_hidden + gate_hidden,
        bypassed=historical,
    )

    # Empty-floor: if only the preamble survived (everything was irrelevant),
    # return empty so no <memory-context> block is injected. An empty recall is
    # strictly better than an irrelevant one — irrelevant context poisons the LLM.
    non_preamble = [e for e in kept if e[0] != "__preamble__"]
    if not non_preamble:
        return ""

    total_hidden = len(hidden) + len(noise_hidden) + len(gate_hidden)
    return _reassemble_output(kept, hidden_count=total_hidden)


# ---------------------------------------------------------------------------
# Schema v1.2 — Layer 4 context-fencing helpers
# (introduced 2026-05-26 to plug recursive-pollution write-path)
# ---------------------------------------------------------------------------


def _fence_log_path(brv_cwd: str) -> Path:
    """Resolve the fence audit log path. Lives next to the context-tree."""
    return Path(brv_cwd) / ".brv" / "fenced.log"


def _extract_slugs_from_brv_output(text: str) -> List[str]:
    """Return slugs (in order, deduped) found in brv-shaped markdown output.

    Matches `### <slug>` headers anchored at line start. Useful for both
    prefetch() output and explicit brv_query tool output, so the fence
    knows which slugs to log.
    """
    if not text:
        return []
    seen: Dict[str, None] = {}
    for m in _ENTRY_HEADER_RE.finditer(text):
        slug = m.group("slug")
        if slug not in seen:
            seen[slug] = None
    return list(seen.keys())


def _sanitize_embedded_markers(text: str) -> str:
    """Replace any embedded fence markers with escape variants.

    Council Round 3 HIGH-2: if a curated entry body contains the begin/end
    markers (e.g. a learning ABOUT the fence design — this very project's
    own self-write pattern), wrapping in prefetch() would yield a malformed
    nested block. We escape embedded markers BEFORE wrapping. Recall is
    semantically unchanged (the agent reading the markdown sees a hyphen-
    suffixed comment that's still invisible in render).
    """
    if not text:
        return text
    # Replace any literal occurrence of the markers (NOT regex, exact match).
    return text.replace(
        _FENCE_BEGIN_MARKER, _FENCE_BEGIN_ESCAPE,
    ).replace(
        _FENCE_END_MARKER, _FENCE_END_ESCAPE,
    )


def _wrap_with_fence_markers(filtered_output: str) -> str:
    """Wrap a brv-shaped output block with HTML-comment fence markers.

    These markers are invisible in markdown rendering but provide a
    deterministic anchor for the structural strip in
    `_apply_context_fence`. Embedded markers in the input are sanitized
    first to prevent the marker-in-content collision (council Round 3
    HIGH-2).

    Disabled when BRV_CONTEXT_FENCING env var is "0"/"false"/"no"/"off"
    — returns the prior un-marked block, byte-for-byte equivalent to
    pre-L4 behavior (council Round 3 HIGH-1).
    """
    if not _fencing_enabled():
        return f"## ByteRover Context\n{filtered_output}"
    return (
        f"{_FENCE_BEGIN_MARKER}\n"
        f"## ByteRover Context\n{_sanitize_embedded_markers(filtered_output)}\n"
        f"{_FENCE_END_MARKER}"
    )


def _apply_context_fence(text: str) -> Tuple[str, int]:
    """Strip ByteRover-context blocks from text. Returns (fenced_text, n_strips).

    The strip is STRUCTURAL: anchored on `_FENCE_BEGIN_MARKER` /
    `_FENCE_END_MARKER`. Per the architecture-critic's H3 finding, this is
    the only safe way to strip — per-slug regex breaks on nested `###`
    sub-headers inside entry bodies.

    Disabled when BRV_CONTEXT_FENCING env var is set to a falsy value
    (council Round 3 HIGH-1).

    Defensive sweep removes orphan begin markers (truncation, partial
    write, adversarial assistant content) before returning so the marker
    string itself never leaks through to brv curate (council Round 3
    HIGH-3).

    No-op fast path if no begin marker is found.
    """
    if not _fencing_enabled():
        return text, 0
    # Fast-path: if no begin marker is present at all, skip the regex entirely
    # (council Round 3 HIGH-3 perf mitigation — assistant content of 50-100KB
    # without any recall would otherwise pay the regex compile-and-scan cost).
    if not text or _FENCE_BEGIN_MARKER not in text:
        return text, 0
    # Phase 1: strip well-formed begin/end blocks.
    fenced, n_strips = _FENCE_BLOCK_RE.subn("", text)
    # Phase 2: any remaining orphan begin markers (no paired end) are stripped
    # line-by-line. This covers truncation, partial assistant writes, and
    # adversarial cases where the assistant emits a begin marker mid-stream.
    if _FENCE_BEGIN_MARKER in fenced:
        fenced = _ORPHAN_BEGIN_RE.sub("", fenced)
    return fenced, n_strips


def _log_fence_decision(
    brv_cwd: str,
    session_id: str,
    slugs_for_audit: Tuple[str, ...],
    chars_stripped: int,
    fully_fenced: bool,
) -> None:
    """Append-only fence-decision log for audit.

    Format (TSV per line):
      <ISO 8601>\t<session_id>\t<slugs-csv-truncated>\t<chars-stripped>\t<fully_fenced>

    Failures are swallowed — logging is best-effort, must not break write path.
    """
    try:
        log_path = _fence_log_path(brv_cwd)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        sid = (session_id or "").replace("\t", " ")[:64]
        # Bound CSV to keep the line under 4 KB PIPE_BUF for atomic appends.
        slugs = list(slugs_for_audit)[:_AUDIT_LOG_SLUG_CSV_MAX]
        slug_csv = ",".join(s.replace(",", "_") for s in slugs)
        if len(slugs_for_audit) > _AUDIT_LOG_SLUG_CSV_MAX:
            slug_csv += f",...trunc({len(slugs_for_audit) - _AUDIT_LOG_SLUG_CSV_MAX} more)"
        line = (
            f"{ts}\t{sid}\t{slug_csv}\t{chars_stripped}\t"
            f"fully_fenced={'true' if fully_fenced else 'false'}\n"
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as e:
        logger.debug("ByteRover fence-log write failed: %s", e)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _load_plugin_config() -> Dict[str, Any]:
    """Read ByteRover's profile-scoped memory config.

    New memory-provider setup stores non-secret provider settings under
    ``memory.<provider>``.  Some users also set ``memory.provider_config`` from
    early docs/issues, so accept it as a compatibility fallback.
    """
    try:
        from hermes_cli.config import load_config

        config = load_config()
        memory_config = config.get("memory", {})
        if not isinstance(memory_config, dict):
            return {}

        provider_config = memory_config.get("byterover", {})
        if isinstance(provider_config, dict) and provider_config:
            return dict(provider_config)

        legacy_config = memory_config.get("provider_config", {})
        if isinstance(legacy_config, dict):
            return dict(legacy_config)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# brv binary resolution (cached, thread-safe)
# ---------------------------------------------------------------------------

_brv_path_lock = threading.Lock()
_cached_brv_path: Optional[str] = None


def _resolve_brv_path() -> Optional[str]:
    """Find the brv binary on PATH or well-known install locations."""
    global _cached_brv_path
    with _brv_path_lock:
        if _cached_brv_path is not None:
            return _cached_brv_path if _cached_brv_path != "" else None

    found = shutil.which("brv")
    if not found:
        home = Path.home()
        candidates = [
            home / ".brv-cli" / "bin" / "brv",
            Path("/usr/local/bin/brv"),
            home / ".npm-global" / "bin" / "brv",
        ]
        for c in candidates:
            if c.exists():
                found = str(c)
                break

    with _brv_path_lock:
        if _cached_brv_path is not None:
            return _cached_brv_path if _cached_brv_path != "" else None
        _cached_brv_path = found or ""
    return found


def _run_brv(args: List[str], timeout: int = _QUERY_TIMEOUT,
             cwd: str = None) -> dict:
    """Run a brv CLI command. Returns {success, output, error}."""
    brv_path = _resolve_brv_path()
    if not brv_path:
        return {"success": False, "error": "brv CLI not found. Install: npm install -g byterover-cli"}

    cmd = [brv_path] + args
    effective_cwd = cwd or str(_get_brv_cwd())
    Path(effective_cwd).mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    brv_bin_dir = str(Path(brv_path).parent)
    env["PATH"] = brv_bin_dir + os.pathsep + env.get("PATH", "")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=effective_cwd, env=env,
            stdin=subprocess.DEVNULL,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0:
            return {"success": True, "output": stdout}
        return {"success": False, "error": stderr or stdout or f"brv exited {result.returncode}"}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"brv timed out after {timeout}s"}
    except FileNotFoundError:
        global _cached_brv_path
        with _brv_path_lock:
            _cached_brv_path = None
        return {"success": False, "error": "brv CLI not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_brv_cwd() -> Path:
    """Profile-scoped working directory for the brv context tree."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "byterover"


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

QUERY_SCHEMA = {
    "name": "brv_query",
    "description": (
        "Search ByteRover's persistent knowledge tree for relevant context. "
        "Returns memories, project knowledge, architectural decisions, and "
        "patterns from previous sessions. Use for any question where past "
        "context would help."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
        },
        "required": ["query"],
    },
}

CURATE_SCHEMA = {
    "name": "brv_curate",
    "description": (
        "Store important information in ByteRover's persistent knowledge tree. "
        "Use for architectural decisions, bug fixes, user preferences, project "
        "patterns — anything worth remembering across sessions. ByteRover's LLM "
        "automatically categorizes and organizes the memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The information to remember."},
        },
        "required": ["content"],
    },
}

STATUS_SCHEMA = {
    "name": "brv_status",
    "description": "Check ByteRover status — CLI version, context tree stats, cloud sync state.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class ByteRoverMemoryProvider(MemoryProvider):
    """ByteRover persistent memory via the brv CLI."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = dict(config) if config is not None else _load_plugin_config()
        self._auto_extract = _coerce_bool(self._config.get("auto_extract"), True)
        self._cwd = ""
        self._session_id = ""
        self._turn_count = 0
        self._sync_thread: Optional[threading.Thread] = None
        # Schema v1.2 — Layer 4 context fencing.
        # Tracks slugs surfaced via prefetch() OR explicit brv_query tool calls
        # in the most-recent turn, used for audit logging when the fence fires.
        # Bounded to _MAX_TRACKED_SLUGS to prevent unbounded growth across many
        # tool calls. The structural marker (not this list) drives the strip.
        self._last_prefetched_slugs: List[str] = []

    @property
    def name(self) -> str:
        return "byterover"

    def is_available(self) -> bool:
        """Check if brv CLI is installed. No network calls."""
        return _resolve_brv_path() is not None

    def get_config_schema(self):
        return [
            {
                "key": "api_key",
                "description": "ByteRover API key (optional, for cloud sync)",
                "secret": True,
                "env_var": "BRV_API_KEY",
                "url": "https://app.byterover.dev",
            },
            {
                "key": "auto_extract",
                "description": "Automatically curate completed turns and compression/memory hooks",
                "default": "true",
                "choices": ["true", "false"],
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._cwd = str(_get_brv_cwd())
        self._session_id = session_id
        self._turn_count = 0
        Path(self._cwd).mkdir(parents=True, exist_ok=True)

    def system_prompt_block(self) -> str:
        if not _resolve_brv_path():
            return ""
        return (
            "# ByteRover Memory\n"
            "Active. Persistent knowledge tree with hierarchical context.\n"
            "Use brv_query to search past knowledge, brv_curate to store "
            "important facts, brv_status to check state."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Run brv query synchronously before the agent's first LLM call.

        Blocks until the query completes (up to _QUERY_TIMEOUT seconds), ensuring
        the result is available as context before the model is called.

        Schema v1.1 (2026-05-26): the raw query result is run through
        `_apply_schema_filter` which (a) re-ranks by recency decay, (b) drops
        entries with `status: resolved | superseded | candidate_resolved`
        unless the query carries an explicit historical-intent signal, and
        (c) logs the filter decision to ~/.hermes/byterover/.brv/filtered.log
        for audit. This stops the dralexmorganbot-style 12-turn stale-recall
        pollution that motivated the filter.

        Schema v1.2 (2026-05-26): output is wrapped with HTML-comment fence
        markers so the Layer 4 write-time fence can structurally strip the
        block from captured turns. Slugs are tracked in
        `_last_prefetched_slugs` for audit logging only — the structural
        marker drives the actual strip.
        """
        if not query or len(query.strip()) < _MIN_QUERY_LEN:
            return ""
        result = _run_brv(
            ["query", "--", query.strip()[:5000]],
            timeout=_QUERY_TIMEOUT, cwd=self._cwd,
        )
        if result["success"] and result.get("output"):
            output = result["output"].strip()
            if len(output) > _MIN_OUTPUT_LEN:
                # Schema v1.1 filter — drop resolved/superseded entries from auto-recall.
                filtered = _apply_schema_filter(
                    raw_output=output,
                    query=query,
                    brv_cwd=self._cwd,
                )
                if len(filtered.strip()) > _MIN_OUTPUT_LEN:
                    # Schema v1.2 — track slugs for fence audit and wrap with markers.
                    slugs = _extract_slugs_from_brv_output(filtered)
                    self._last_prefetched_slugs = slugs[:_MAX_TRACKED_SLUGS]
                    return _wrap_with_fence_markers(filtered)
        # Cold-path: clear slug list when prefetch returns empty.
        self._last_prefetched_slugs = []
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op: prefetch() now runs synchronously at turn start."""
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Curate the conversation turn in background (non-blocking).

        Schema v1.2 — Layer 4 context fencing: BEFORE spawning the curate
        thread, this strips ByteRover-context blocks from both user and
        assistant content using the structural fence markers emitted by
        prefetch(). The strip happens on the calling thread (per the
        architecture-critic's C3 finding) so prefetch(N+1) cannot overwrite
        slug state before sync_turn(N)'s thread fires.
        """
        self._turn_count += 1
        if not self._auto_extract:
            logger.debug("ByteRover sync_turn skipped (auto_extract disabled)")
            return

        # Only curate substantive turns
        if len(user_content.strip()) < _MIN_QUERY_LEN:
            return

        # C3 mitigation: snapshot slug list ON THE CALLING THREAD.
        slugs_snapshot: Tuple[str, ...] = tuple(self._last_prefetched_slugs)

        fenced_user, _ = _apply_context_fence(user_content)
        fenced_assistant, n_strips = _apply_context_fence(assistant_content)
        chars_stripped = (
            len(user_content) - len(fenced_user)
            + len(assistant_content) - len(fenced_assistant)
        )

        # M1 fix: skip curate entirely when post-fence content is too sparse.
        fully_fenced = (
            len(fenced_user.strip()) < _MIN_QUERY_LEN
            and len(fenced_assistant.strip()) < _MIN_OUTPUT_LEN
        )

        if chars_stripped > 0 or n_strips > 0:
            _log_fence_decision(
                brv_cwd=self._cwd,
                session_id=session_id or self._session_id,
                slugs_for_audit=slugs_snapshot,
                chars_stripped=chars_stripped,
                fully_fenced=fully_fenced,
            )

        if fully_fenced:
            # Whole turn was a recall dump; nothing substantive to curate.
            return

        # Capture fenced values into the closure (NOT the instance attribute).
        user_for_thread = fenced_user
        assistant_for_thread = fenced_assistant

        def _sync():
            try:
                combined = (
                    f"User: {user_for_thread[:2000]}\n"
                    f"Assistant: {assistant_for_thread[:2000]}"
                )
                _run_brv(
                    ["curate", "--", combined],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
            except Exception as e:
                logger.debug("ByteRover sync failed: %s", e)

        # Wait for previous sync
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="brv-sync"
        )
        self._sync_thread.start()

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in memory writes to ByteRover.

        Schema v1.2 — content is fenced before write. Even though MEMORY.md
        mirror writes are by definition new content, fencing here is uniform
        defense-in-depth (the architecture-critic's M2 mitigation): if the
        agent ever writes a fact INTO MEMORY.md that was itself recalled
        from ByteRover, we don't want it re-curated as a fresh entry.
        """
        if not self._auto_extract:
            logger.debug("ByteRover memory mirror skipped (auto_extract disabled)")
            return
        if action not in {"add", "replace"} or not content:
            return

        # Fence the content (no-op unless it carries an embedded recall block).
        slugs_snapshot: Tuple[str, ...] = tuple(self._last_prefetched_slugs)
        fenced_content, n_strips = _apply_context_fence(content)
        if n_strips > 0:
            _log_fence_decision(
                brv_cwd=self._cwd,
                session_id=self._session_id,
                slugs_for_audit=slugs_snapshot,
                chars_stripped=len(content) - len(fenced_content),
                fully_fenced=(len(fenced_content.strip()) < _MIN_OUTPUT_LEN),
            )
        if len(fenced_content.strip()) < _MIN_OUTPUT_LEN:
            return

        def _write():
            try:
                label = "User profile" if target == "user" else "Agent memory"
                _run_brv(
                    ["curate", "--", f"[{label}] {fenced_content}"],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
            except Exception as e:
                logger.debug("ByteRover memory mirror failed: %s", e)

        t = threading.Thread(target=_write, daemon=True, name="brv-memwrite")
        t.start()

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract insights before context compression discards turns.

        Schema v1.2 — each message's content is fenced before being joined
        and curated. Per the architecture-critic's C1 finding, this was the
        most dangerous unfenced write path: a long-session compression would
        otherwise pull recalled-block bodies from `messages[-10:]` and
        re-import them under a fresh session-output slug, defeating Layer 4.
        """
        if not self._auto_extract:
            logger.debug("ByteRover pre-compression flush skipped (auto_extract disabled)")
            return ""
        if not messages:
            return ""

        slugs_snapshot: Tuple[str, ...] = tuple(self._last_prefetched_slugs)
        total_stripped = 0
        parts: List[str] = []
        for msg in messages[-10:]:  # last 10 messages
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip() and role in {"user", "assistant"}:
                fenced, _ = _apply_context_fence(content)
                total_stripped += len(content) - len(fenced)
                if len(fenced.strip()) >= _MIN_QUERY_LEN:
                    parts.append(f"{role}: {fenced[:500]}")

        if total_stripped > 0:
            _log_fence_decision(
                brv_cwd=self._cwd,
                session_id=self._session_id,
                slugs_for_audit=slugs_snapshot,
                chars_stripped=total_stripped,
                fully_fenced=(not parts),
            )

        if not parts:
            return ""

        combined = "\n".join(parts)

        def _flush():
            try:
                _run_brv(
                    ["curate", "--", f"[Pre-compression context]\n{combined}"],
                    timeout=_CURATE_TIMEOUT, cwd=self._cwd,
                )
                logger.info("ByteRover pre-compression flush: %d messages", len(parts))
            except Exception as e:
                logger.debug("ByteRover pre-compression flush failed: %s", e)

        t = threading.Thread(target=_flush, daemon=True, name="brv-flush")
        t.start()
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [QUERY_SCHEMA, CURATE_SCHEMA, STATUS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "brv_query":
            return self._tool_query(args)
        elif tool_name == "brv_curate":
            return self._tool_curate(args)
        elif tool_name == "brv_status":
            return self._tool_status()
        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)

    # -- Tool implementations ------------------------------------------------

    def _tool_query(self, args: dict) -> str:
        query = args.get("query", "")
        if not query:
            return tool_error("query is required")

        result = _run_brv(
            ["query", "--", query.strip()[:5000]],
            timeout=_QUERY_TIMEOUT, cwd=self._cwd,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Query failed"))

        output = result.get("output", "").strip()
        if not output or len(output) < _MIN_OUTPUT_LEN:
            return json.dumps({"result": "No relevant memories found."})

        # Truncate very long results
        if len(output) > 8000:
            output = output[:8000] + "\n\n[... truncated]"

        # Schema v1.2 (H1 mitigation): extend the slug-tracking list with
        # any slugs returned by the explicit brv_query tool call. This
        # keeps the fence audit accurate even when the agent uses the tool
        # path instead of relying on auto-prefetch.
        try:
            tool_slugs = _extract_slugs_from_brv_output(output)
            if tool_slugs:
                merged = list(
                    dict.fromkeys(self._last_prefetched_slugs + tool_slugs)
                )
                self._last_prefetched_slugs = merged[:_MAX_TRACKED_SLUGS]
        except Exception as e:
            logger.debug("brv_query slug-tracking failed: %s", e)

        return json.dumps({"result": output})

    def _tool_curate(self, args: dict) -> str:
        content = args.get("content", "")
        if not content:
            return tool_error("content is required")

        # Schema v1.2 (Round 3 defense-in-depth): explicit user-invoked
        # curate is also fenced. If the agent loops a recalled-block back
        # into a brv_curate call (rare, but possible during reasoning),
        # this prevents the fresh entry from re-importing recall verbatim.
        slugs_snapshot: Tuple[str, ...] = tuple(self._last_prefetched_slugs)
        fenced_content, n_strips = _apply_context_fence(content)
        if n_strips > 0:
            _log_fence_decision(
                brv_cwd=self._cwd,
                session_id=self._session_id,
                slugs_for_audit=slugs_snapshot,
                chars_stripped=len(content) - len(fenced_content),
                fully_fenced=(len(fenced_content.strip()) < _MIN_OUTPUT_LEN),
            )
        if len(fenced_content.strip()) < _MIN_OUTPUT_LEN:
            return tool_error("content is empty after fence strip")

        result = _run_brv(
            ["curate", "--", fenced_content],
            timeout=_CURATE_TIMEOUT, cwd=self._cwd,
        )

        if not result["success"]:
            return tool_error(result.get("error", "Curate failed"))

        return json.dumps({"result": "Memory curated successfully."})

    def _tool_status(self) -> str:
        result = _run_brv(["status"], timeout=15, cwd=self._cwd)
        if not result["success"]:
            return tool_error(result.get("error", "Status check failed"))
        return json.dumps({"status": result.get("output", "")})


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register ByteRover as a memory provider plugin."""
    ctx.register_memory_provider(ByteRoverMemoryProvider())
