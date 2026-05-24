"""Tests for the Bedrock patch guard (bedrock_guard.py).

NOTE: These tests exercise the bedrock-guard dotfiles project at
~/.local/lib/bedrock-guard/ (symlinked from ~/dotfiles/bedrock-guard/).
That tree is NOT part of Hermes — it's an out-of-tree per-user tool.

Code review P1-E (2026-05-24): the previous `pytest.importorskip` here
SILENTLY SKIPPED on every machine without bedrock-guard installed,
hiding the fact that 421 lines of tests weren't running. This is the
classic "tests pass because they don't actually run" failure mode.

The fix: convert the silent skip to an EXPLICIT module-level
`pytest.skip(allow_module_level=True)` with a loud reason so anyone
viewing pytest output sees the deliberate skip, not an empty
collection. The tests DO exercise real production paths on Prax's
machine where bedrock-guard is installed.

Long-term TODO: move these tests into the bedrock-guard repo itself
(co-located with the code they test) so Hermes CI doesn't carry tests
for an external project.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the guard dir importable. The module lives at
# ~/.local/lib/bedrock-guard/ — independent of Hermes.
GUARD_DIR = Path.home() / ".local" / "lib" / "bedrock-guard"
if not GUARD_DIR.exists() or not (GUARD_DIR / "bedrock_guard.py").exists():
    pytest.skip(
        f"bedrock-guard out-of-tree dependency not installed at {GUARD_DIR}. "
        "These tests exercise an external dotfiles tool; they only run on "
        "machines where bedrock-guard is installed. To install, see "
        "~/dotfiles/bedrock-guard/. Long-term these tests belong in the "
        "bedrock-guard repo itself (P1-E, 2026-05-24).",
        allow_module_level=True,
    )

if str(GUARD_DIR) not in sys.path:
    sys.path.insert(0, str(GUARD_DIR))

import bedrock_guard  # noqa: E402  (import has to follow path injection above)


def _make_install(tmp_path: Path, version: str, files: dict[str, str]) -> Path:
    """Build a fake installed-bundle tree. Returns the install root path."""
    root = tmp_path / "install"
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "fake", "version": version}))
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _make_patches(
    tmp_path: Path,
    tool: str,
    version: str,
    markers: list[dict[str, Any]],
) -> Path:
    """Build the data-root patches/dist/<tool>-<version>.markers.json tree.
    Returns the data-root path (what PI_HOME / OPENCLAW_HOME should point at)."""
    data_root = tmp_path / f"{tool}_data"
    patches = data_root / "patches" / "dist"
    patches.mkdir(parents=True)
    (patches / f"{tool}-{version}.markers.json").write_text(json.dumps(markers))
    return data_root


def _bind_install_root(monkeypatch: pytest.MonkeyPatch, tool: str, root: Path) -> None:
    """Route bedrock_guard._install_root(<tool>) to our fake path."""
    def fake(t: str) -> Path | None:
        if t == tool:
            return root
        return None
    monkeypatch.setattr(bedrock_guard, "_install_root", fake)


def _bind_pi_home(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    monkeypatch.setenv("PI_HOME", str(data_root))


def _bind_openclaw_home(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    monkeypatch.setenv("OPENCLAW_HOME", str(data_root))


# ---------------------------------------------------------------------------


def test_all_markers_present_is_noop(tmp_path, monkeypatch, caplog):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/core/auth.js": 'const x = "bedrock-config"; const y = "context-1m-2025-08-07";'},
    )
    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/core/auth.js",
                "needle": "bedrock-config",
                "label": "variant",
                "remediation": {"op": "file_copy", "from": "/nonexistent/fork/path.js"},
            },
            {
                "file": "dist/core/auth.js",
                "needle": "context-1m-2025-08-07",
                "label": "1M beta",
                "remediation": {"op": "file_copy", "from": "/nonexistent/fork/path.js"},
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any("all 2 markers present" in m for m in messages), messages
    # File unchanged (no copy attempted)
    assert "bedrock-config" in (install / "dist/core/auth.js").read_text()


def test_missing_marker_triggers_file_copy(tmp_path, monkeypatch, caplog):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/core/auth.js": "const stale = true;"},
    )
    fork_source = tmp_path / "fork/dist/core/auth.js"
    fork_source.parent.mkdir(parents=True)
    fork_source.write_text('const x = "bedrock-config"; const y = "context-1m-2025-08-07";')

    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/core/auth.js",
                "needle": "bedrock-config",
                "label": "variant",
                "remediation": {"op": "file_copy", "from": str(fork_source)},
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()

    # File was overwritten with fork content
    new_content = (install / "dist/core/auth.js").read_text()
    assert "bedrock-config" in new_content
    assert "stale" not in new_content
    messages = [r.message for r in caplog.records]
    assert any("applied file_copy" in m for m in messages), messages
    assert any("healed=1" in m for m in messages), messages


def test_file_copy_creates_missing_directories(tmp_path, monkeypatch, caplog):
    install = _make_install(tmp_path, version="1.2.3", files={})  # no files at all
    fork_source = tmp_path / "fork/brand-new.js"
    fork_source.parent.mkdir(parents=True)
    fork_source.write_text('"marker-needle-abc"')

    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/newly/nested/file.js",
                "needle": "marker-needle-abc",
                "label": "new file",
                "remediation": {"op": "file_copy", "from": str(fork_source)},
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    bedrock_guard.check_pi()

    created = install / "dist/newly/nested/file.js"
    assert created.exists()
    assert "marker-needle-abc" in created.read_text()


def test_anchor_patch_replaces_needle(tmp_path, monkeypatch):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/main.js": "const greet = 'hello world';"},
    )
    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/main.js",
                "needle": "goodbye",
                "label": "swap greeting",
                "remediation": {
                    "op": "anchor_patch",
                    "old": "hello world",
                    "new": "goodbye world",
                },
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    bedrock_guard.check_pi()

    assert (install / "dist/main.js").read_text() == "const greet = 'goodbye world';"


def test_anchor_patch_skip_when_old_text_missing(tmp_path, monkeypatch, caplog):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/main.js": "completely different content"},
    )
    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/main.js",
                "needle": "marker-xyz",
                "label": "won't-apply",
                "remediation": {
                    "op": "anchor_patch",
                    "old": "hello world",
                    "new": "goodbye world",
                },
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("WARNING"):
        bedrock_guard.check_pi()

    # File untouched
    assert (install / "dist/main.js").read_text() == "completely different content"
    messages = [r.message for r in caplog.records]
    assert any("old-text not found" in m for m in messages), messages


def test_unknown_version_falls_back_to_latest_markers(tmp_path, monkeypatch, caplog):
    """Unknown installed version → ``_find_latest_markers`` falls back.

    When ``pi`` ships a new version before the patch maintainer creates a
    matching ``pi-<version>.markers.json``, ``bedrock_guard`` gracefully
    falls back to the newest available markers file under ``patches/dist/``.
    That behaviour was added specifically to prevent silent
    ``no marker set — skip`` regressions on every new upstream release.
    See ``_find_latest_markers`` at bedrock_guard.py:82.

    This test replaces the original ``test_unknown_version_is_skipped``,
    which asserted the OPPOSITE (skip with "no marker set"). That was the
    old pre-fallback behaviour; keeping the test would have locked a
    regression in. We now have explicit coverage for BOTH:
      * this test — fallback fires when another version's markers exist
      * ``test_no_markers_at_all_is_skipped`` — skip fires when patches/dist
        is empty or absent

    (2026-05-11 — RCA of ``test_unknown_version_is_skipped`` flagging on the
    rebase of ``feat/native-bedrock-provider-20260428``.)
    """
    install = _make_install(tmp_path, version="99.0.0", files={})
    # Ship a patch set for a DIFFERENT version — the fallback must find it.
    _make_patches(tmp_path, tool="pi", version="1.2.3", markers=[])
    data = tmp_path / "pi_data"
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    # Fallback-found log MUST fire (the feature's whole point).
    assert any(
        "no exact markers" in m and "falling back" in m for m in messages
    ), messages
    # Empty marker list → "all 0 markers present ✓" success log.
    assert any("all 0 markers present" in m for m in messages), messages
    # And we must NOT fall through to the "no marker set" skip path —
    # that would defeat the fallback.
    assert not any("no marker set" in m for m in messages), messages


def test_no_markers_at_all_is_skipped(tmp_path, monkeypatch, caplog):
    """Truly empty patches dir → skip via ``no marker set``.

    When ``patches/dist/`` exists but contains no ``pi-*.markers.json``
    files (or doesn't exist at all), there's nothing for
    ``_find_latest_markers`` to fall back to. ``check_pi`` must then log
    ``no marker set — skip`` and return cleanly — this is the benign path
    for tools that have been upstream-merged out of existence.
    """
    install = _make_install(tmp_path, version="99.0.0", files={})
    # Build the data root with an EMPTY patches/dist dir — no markers files.
    data_root = tmp_path / "pi_data"
    (data_root / "patches" / "dist").mkdir(parents=True)
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data_root)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any(
        "no marker set" in m and "skip" in m for m in messages
    ), messages
    # And we must NOT have pretended to fall back to something.
    assert not any("falling back" in m for m in messages), messages


def test_not_installed_tool_is_skipped(monkeypatch, caplog):
    monkeypatch.setattr(bedrock_guard, "_install_root", lambda t: None)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()
        bedrock_guard.check_openclaw()

    messages = [r.message for r in caplog.records]
    assert sum("not installed" in m for m in messages) == 2, messages


def test_source_missing_fails_gracefully(tmp_path, monkeypatch, caplog):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/main.js": "stale"},
    )
    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/main.js",
                "needle": "marker-xyz",
                "label": "missing-source",
                "remediation": {
                    "op": "file_copy",
                    "from": "/does/not/exist.js",
                },
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("WARNING"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any("source missing" in m for m in messages), messages
    # File untouched
    assert (install / "dist/main.js").read_text() == "stale"


def test_idempotent_double_run(tmp_path, monkeypatch, caplog):
    install = _make_install(
        tmp_path,
        version="1.2.3",
        files={"dist/main.js": "stale"},
    )
    fork = tmp_path / "fork/main.js"
    fork.parent.mkdir(parents=True)
    fork.write_text('"marker-xyz"')
    data = _make_patches(
        tmp_path,
        tool="pi",
        version="1.2.3",
        markers=[
            {
                "file": "dist/main.js",
                "needle": "marker-xyz",
                "label": "x",
                "remediation": {"op": "file_copy", "from": str(fork)},
            },
        ],
    )
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    bedrock_guard.check_pi()
    first_content = (install / "dist/main.js").read_text()

    caplog.clear()
    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()
    second_content = (install / "dist/main.js").read_text()

    assert first_content == second_content
    messages = [r.message for r in caplog.records]
    assert any("all 1 markers present" in m for m in messages), messages


def test_malformed_markers_json_is_handled(tmp_path, monkeypatch, caplog):
    install = _make_install(tmp_path, version="1.2.3", files={})
    data = tmp_path / "pi_data"
    patches = data / "patches" / "dist"
    patches.mkdir(parents=True)
    (patches / "pi-1.2.3.markers.json").write_text("not valid json {{")

    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("ERROR"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any("invalid JSON" in m for m in messages), messages


def test_markers_must_be_a_list(tmp_path, monkeypatch, caplog):
    install = _make_install(tmp_path, version="1.2.3", files={})
    data = tmp_path / "pi_data"
    patches = data / "patches" / "dist"
    patches.mkdir(parents=True)
    (patches / "pi-1.2.3.markers.json").write_text('{"not": "a list"}')

    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("ERROR"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any("must be a JSON array" in m for m in messages), messages
