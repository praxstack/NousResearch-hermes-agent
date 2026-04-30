"""Tests for the Bedrock patch guard (bedrock_guard.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Make the guard dir importable. The module now lives at
# ~/.local/lib/bedrock-guard/ — independent of Hermes.
GUARD_DIR = Path.home() / ".local" / "lib" / "bedrock-guard"
if str(GUARD_DIR) not in sys.path:
    sys.path.insert(0, str(GUARD_DIR))

bedrock_guard = pytest.importorskip("bedrock_guard")


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


def test_unknown_version_is_skipped(tmp_path, monkeypatch, caplog):
    install = _make_install(tmp_path, version="99.0.0", files={})
    # Only ship a patch set for a DIFFERENT version
    _make_patches(tmp_path, tool="pi", version="1.2.3", markers=[])
    data = tmp_path / "pi_data"
    _bind_install_root(monkeypatch, "pi", install)
    _bind_pi_home(monkeypatch, data)

    with caplog.at_level("INFO"):
        bedrock_guard.check_pi()

    messages = [r.message for r in caplog.records]
    assert any("no marker set" in m for m in messages), messages


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
