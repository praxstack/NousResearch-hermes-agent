# Upstream Issue — Test suite leaks real macOS Keychain credentials

**Repo:** NousResearch/hermes-agent
**File:** tests/agent/test_anthropic_adapter.py
**Classes affected:** TestReadClaudeCodeCredentials, TestResolveAnthropicToken, TestRunOauthSetupToken (10 tests)
**Severity:** Test-only (production code is correct)
**Environment required to reproduce:** macOS with Claude Code ≥ 2.1.114 installed (which uses Keychain for OAuth creds)

## Summary

`agent.anthropic_adapter.read_claude_code_credentials()` checks the macOS Keychain FIRST via:

```python
subprocess.run(
    ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
    capture_output=True, text=True, timeout=5,
)
```

Only when the Keychain entry is missing does it fall back to `~/.claude/.credentials.json`. Tests in the three affected classes use `monkeypatch.setattr(Path.home, lambda: tmp_path)` to isolate the filesystem, but they never stub the Keychain subprocess call. On dev machines with Claude Code installed (very common among Anthropic/Hermes contributors), the Keychain returns real credentials and bypasses the filesystem isolation entirely.

Symptoms:
- `AssertionError: assert 'sk-ant-oat01..._6Dg-YIgnYAAA' == 'sk-ant...oken'` — real token bleeds into assertion
- `TypeError: the JSON object must be str, bytes or bytearray, not MagicMock` — when `subprocess.run` is partially mocked through `shutil.which` shims, the keychain's `result.stdout` is a MagicMock that then gets fed to `json.loads`

CI passes because CI runners are Linux / clean macOS images with no Keychain entry.

## Minimal repro (macOS + Claude Code installed)

```bash
# Verify you have a Keychain entry (if empty, you can't reproduce)
security find-generic-password -s "Claude Code-credentials" -w | head -c 30
# Should print the start of a JSON blob containing claudeAiOauth

# Run the tests
cd hermes-agent
venv/bin/python -m pytest tests/agent/test_anthropic_adapter.py -o addopts= -q 2>&1 | tail
# 11 failed, 132 passed
```

## Fix (already applied in praxstack fork, commit 9d1263c2c)

Add a module-level fixture that patches the Keychain helper, then opt the three credential-resolution test classes into it via `@pytest.mark.usefixtures`:

```python
@pytest.fixture
def _isolate_claude_keychain():
    with patch(
        "agent.anthropic_adapter._read_claude_code_credentials_from_keychain",
        return_value=None,
    ):
        yield


@pytest.mark.usefixtures("_isolate_claude_keychain")
class TestReadClaudeCodeCredentials: ...

@pytest.mark.usefixtures("_isolate_claude_keychain")
class TestResolveAnthropicToken: ...

@pytest.mark.usefixtures("_isolate_claude_keychain")
class TestRunOauthSetupToken: ...
```

After fix: 143/143 pass on the same dev machine.

## Why patch the helper, not `subprocess.run`

1. Targeted — only the Keychain path is mocked; other subprocess calls (e.g. `claude` CLI invocation in `run_oauth_setup_token`) keep their own per-test mocking.
2. Survives implementation churn — as long as `read_claude_code_credentials()` calls `_read_claude_code_credentials_from_keychain()` by that name, the fixture works regardless of how the helper's internals change.
3. Opt-in rather than autouse so future tests that DO want to exercise the Keychain path can skip the fixture.

## Secondary issue — not part of this fix

`tests/agent/test_anthropic_adapter.py::TestBuildAnthropicClient::test_custom_base_url` was also failing, but that was unrelated: it pre-dates the `context-1m-2025-08-07` addition for third-party Anthropic endpoints (commit 984951ae8, `feat(bedrock): native provider`). The test's expected `default_headers` dict was never updated to reflect the new three-beta set. Fix in same commit 9d1263c2c.
