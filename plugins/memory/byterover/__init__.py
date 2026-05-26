"""ByteRover memory plugin — MemoryProvider interface.

Persistent memory via the ByteRover CLI (``brv``). Organizes knowledge into
a hierarchical context tree with tiered retrieval (fuzzy text → LLM-driven
search). Local-first with optional cloud sync.

Original PR #3499 by hieuntg81, adapted to MemoryProvider ABC.

Requires: ``brv`` CLI installed (npm install -g byterover-cli or
curl -fsSL https://byterover.dev/install.sh | sh).

Config via environment variables (profile-scoped via each profile's .env):
  BRV_API_KEY   — ByteRover API key (for cloud features, optional for local)

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
    "old rca",
    "already fixed",
    "already shipped",
    "resolved entr",       # "resolved entry" / "resolved entries"
    "archived",
    "archive ",
    "when did we",
    "what did we ship",
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

# Strip the WHOLE fenced block in one operation. The block contains the recall
# preamble + entries; everything outside the markers is preserved verbatim.
_FENCE_BLOCK_RE = re.compile(
    re.escape(_FENCE_BEGIN_MARKER)
    + r"[\s\S]*?"
    + re.escape(_FENCE_END_MARKER)
    + r"\n?",
    re.MULTILINE,
)

# Bound on _last_prefetched_slugs to keep memory + audit-log line size finite.
_MAX_TRACKED_SLUGS = 50

# Truncate slug-csv field in audit log so each line stays under 4 KB PIPE_BUF.
_AUDIT_LOG_SLUG_CSV_MAX = 50

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

    Header lines `### <slug>` separate entries. The first chunk before the
    first header (typically `**Summary**: Found N relevant topics...`) is
    returned with slug `__preamble__` so callers can preserve it.

    Returns an empty list if no headers found — caller should treat the
    whole output as a single un-splittable blob.
    """
    headers = list(_ENTRY_HEADER_RE.finditer(query_output))
    if not headers:
        return []

    entries: List[Tuple[str, str]] = []
    # Preamble (everything before first ### header)
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

    _log_filter_decision(
        brv_cwd=brv_cwd,
        query=query,
        hidden_entries=hidden,
        bypassed=historical,
    )

    return _reassemble_output(kept, hidden_count=len(hidden))


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


def _wrap_with_fence_markers(filtered_output: str) -> str:
    """Wrap a brv-shaped output block with HTML-comment fence markers.

    These markers are invisible in markdown rendering but provide a
    deterministic anchor for the structural strip in
    `_apply_context_fence`.
    """
    return (
        f"{_FENCE_BEGIN_MARKER}\n"
        f"## ByteRover Context\n{filtered_output}\n"
        f"{_FENCE_END_MARKER}"
    )


def _apply_context_fence(text: str) -> Tuple[str, int]:
    """Strip ByteRover-context blocks from text. Returns (fenced_text, n_strips).

    The strip is STRUCTURAL: anchored on `_FENCE_BEGIN_MARKER` /
    `_FENCE_END_MARKER`. Per the architecture-critic's H3 finding, this is
    the only safe way to strip — per-slug regex breaks on nested `###`
    sub-headers inside entry bodies.

    No-op if no begin marker is found.
    """
    if not text or _FENCE_BEGIN_MARKER not in text:
        return text, 0
    fenced, n_strips = _FENCE_BLOCK_RE.subn("", text)
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

    def __init__(self):
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

        result = _run_brv(
            ["curate", "--", content],
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
