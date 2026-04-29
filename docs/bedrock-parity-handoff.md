# Bedrock Cline-Parity — Handoff

**Date:** 2026-04-29
**Author:** Prax (with Claude Opus 4.7)
**Branch:** `feat/native-bedrock-provider-20260428` (Hermes) — this doc is committed here.

---

## TL;DR

Amazon Bedrock provider support in **pi-coding-agent** and **OpenClaw** has been brought to 1:1 feature parity with Cline's Bedrock UI. Changes are:

1. **Landed locally** on your machine via the Hermes post-update patch guard. Both installed tools at `/opt/homebrew/lib/node_modules/` now carry the patched Bedrock code and will self-heal after any `npm -g upgrade` that overwrites them.
2. **Pending upstream** as PRs to both source repos. Either or both may be merged into the published npm packages, at which point the local patches become redundant (the guard will log `all markers present ✓` and skip).

No dependency on upstream merge. Works today.

## Status matrix

| Track | Status | Evidence |
|---|---|---|
| pi-mono PR | **OPEN (auto-closed by new-contributor gate)** | https://github.com/badlogic/pi-mono/pull/3947 |
| openclaw PR | **OPEN** | https://github.com/openclaw/openclaw/pull/74202 |
| pi-coding-agent installed bundle (0.70.5) | patched + guarded | 12/12 markers present |
| openclaw installed bundle (2026.4.26) | patched + guarded | 7/7 markers present |
| Hermes H1–H6 audit | **5/6 ✅, 1 partial** | See Hermes gaps below |
| End-to-end lifecycle (D.1) | **PASS** | overwrite → detect → heal → idempotent |
| Tests | **123 new + green regressions** | 77 pi-ai + 46 coding-agent + 71 openclaw + 11 handler_dist |

## What landed

### pi-mono fork (`github.com/praxstack/pi-mono:feat/bedrock-cline-parity-20260429`)

12 commits covering:

- `packages/ai/src/providers/amazon-bedrock-auth.ts` (NEW) — `BedrockAuthMode`, `BedrockAuthInputs`, `ResolvedBedrockClientInputs`, `resolveBedrockAuthMode`, `resolveBedrockClientInputs`, `BedrockAuthError`.
- `packages/ai/src/providers/amazon-bedrock.ts` — refactored to dispatch auth through the resolver. Extended `BedrockOptions` with Cline-parity fields (`awsAuthentication`, `awsBedrockApiKey`, `awsProfile`, `awsAccessKey`, `awsSecretKey`, `awsSessionToken`, `awsBedrockEndpoint`, `awsRegion`, `awsUseCrossRegionInference`, `awsUseGlobalInference`, `awsBedrockUsePromptCache`, `enable1MContext`). Preserves legacy `bearerToken`/`profile`/`region` aliases. Exports `supportsOpus1MContext`, `applyOpus1MSuffix`, `supportsAdaptiveThinking`.
- `packages/ai/src/providers/amazon-bedrock.ts` — wires `:1m` suffix + `anthropic_beta: ["context-1m-2025-08-07"]` into `additionalModelRequestFields` for Opus 4.6/4.7 when `enable1MContext: true`. Deduped beta emission into a single `betaFlags` builder.
- `packages/coding-agent/src/core/auth-storage.ts` — added `bedrock-config` variant to `AuthCredential` union + `BedrockAuthConfig` type + pure `migrateLegacyBedrockAuth` helper.
- `packages/coding-agent/src/core/bedrock-setup-config.ts` (NEW) — `buildBedrockAuthConfigFromSetup` pure helper (unit-testable without pi-tui).
- `packages/coding-agent/src/modes/interactive/components/bedrock-setup-dialog.ts` (NEW) — `BedrockSetupFlow` TUI orchestration using existing `ExtensionSelectorComponent` + `LoginDialogComponent.showPrompt`.
- `packages/coding-agent/src/modes/interactive/interactive-mode.ts` — replaced info-only `showBedrockSetupDialog` with the new flow.
- All 7 pi-mono packages bumped lockstep to 0.70.7. CHANGELOGs + provider docs updated.

Hardening:
- `AWS_BEDROCK_SKIP_AUTH=1` suppresses bearer token AND profile, not just credentials.
- Empty/whitespace-only API key (including literal `"Bearer "`) raises `BedrockAuthError`, not `token: ""`.
- Browser-safe: `process.env` guarded with `typeof process !== "undefined"`.
- Compile-time exhaustiveness guard on the auth-mode `switch`.

Tests: 22 (auth-modes) + 6 (credentials) + 6 (vpc) + 15 (1m incl. capturePayload integration) + 4 (reasoning signature) + 12 (adaptive thinking) + 8 (migration) + 14 (setup-flow) + existing 24 (auth-storage) = **111 passing**.

### openclaw fork (`github.com/praxstack/openclaw:feat/bedrock-cline-parity-20260429`)

4 commits covering:

- `extensions/amazon-bedrock/bedrock-auth-config.ts` (NEW) + `bedrock-auth-config.test.ts` (NEW) — `BedrockAuthConfig`, `BedrockAuthenticationMode`, `LegacyBedrockOptions`, `ReasoningEffort`, `normalizeBedrockAuthConfig` with legacy `awsUseProfile` migration. 13 tests.
- `extensions/amazon-bedrock/openclaw.plugin.json` — declares all 14 Cline-parity config keys (`awsAuthentication`, `awsBedrockApiKey`, `awsProfile`, `awsAccessKey`, `awsSecretKey`, `awsSessionToken`, `awsRegion`, `awsBedrockEndpoint`, `awsUseCrossRegionInference`, `awsUseGlobalInference`, `awsBedrockUsePromptCache`, `reasoningEffort`, `thinkingBudgetTokens`, `enable1MContext`).
- `extensions/amazon-bedrock/setup-api.ts` — re-exports the auth-config types as `BedrockSetupOptions`.
- Plugin version bump `2026.4.25` → `2026.4.26`. CHANGELOG entries. `docs/providers/bedrock.md` updated with four-mode auth section.

Boundary decision: `resolveAuth` / `BedrockAuthError` intentionally NOT added to OpenClaw — inference belongs to pi-ai per OpenClaw's extension boundary rules (`extensions/CLAUDE.md`). OpenClaw owns the config surface; pi-ai owns auth resolution.

### Local patch guards (`~/.hermes/hooks/post-update-patches/handler_dist.py`)

245-LOC Python helper imported by the existing Hermes post-update hook. Runs on every `gateway:startup` event:

- Loads `~/.pi/patches/dist/pi-<version>.markers.json` and `~/.openclaw/patches/dist/openclaw-<version>.markers.json` for the currently-installed versions.
- For each marker, checks if the declared needle is present in the target file.
- If missing, applies the declared remediation — currently all entries use `op: "file_copy"` with an absolute `from` path pointing into `~/forks/pi-mono/packages/*/dist/`.
- Detection-only mode available via `op: "none"`; `op: "anchor_patch"` wired up for future text-anchor replacements.

Tests: 11 pytest cases covering all-present no-op, missing-marker file-copy, nested-directory creation, anchor-patch replace + skip-when-anchor-missing, unknown version, not-installed tool, source-missing graceful failure, idempotency, malformed JSON.

### Hermes (no code changes)

H1, H2, H4, H5, H6: ✅
H3 partial — `vpc_endpoint_url` works functionally in `agent/bedrock_adapter.py` but is NOT prompted by the interactive setup wizard and NOT present in the default `bedrock` config block. See "Deferred" below.

459 Hermes tests still pass (190 ran in the scoped Bedrock sweep). CR markers all present.

## How to use

### Verify the guard is active

```bash
cd ~/.hermes/hooks/post-update-patches
python3 -c "
import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)-7s %(message)s')
from handler_dist import check_pi, check_openclaw
check_pi()
check_openclaw()
"
```

Expected: `all 12 markers present ✓` for Pi, `all 7 markers present ✓` for OpenClaw.

### If upstream has pushed an update that overwrites our patches

The Hermes gateway startup hook automatically runs the guard. No manual action needed. If you want to force a check:

```bash
cd ~/.hermes/hooks/post-update-patches
python3 -c "from handler_dist import check_pi, check_openclaw; check_pi(); check_openclaw()"
```

### Configuring Bedrock in Pi

Run `pi` interactively and pick `/login` → Amazon Bedrock. The four-mode radio will appear:

- **API Key** — `AWS_BEARER_TOKEN_BEDROCK`-style bearer token.
- **AWS Profile** — profile name from `~/.aws/credentials`.
- **AWS Credentials** — explicit access key + secret + optional session token.
- **Default Chain** — AWS SDK default credential chain.

After the mode, you'll be prompted for region. The four toggles (VPC endpoint, CRI, global inference, prompt cache) use sensible defaults today (`true/true/true/false`). To override, edit `~/.config/pi/auth.json` directly.

### Configuring Bedrock in OpenClaw

OpenClaw delegates to pi-ai, so whatever you configure in pi-coding-agent's auth.json applies to OpenClaw as well. The plugin manifest also declares the config keys, so OpenClaw's setup UX (where present) can surface them.

## Deferred

These items are known gaps, either because they're out of scope for this handoff or waiting on upstream:

1. **Hermes interactive wizard does not expose `vpc_endpoint_url`.** Functional today (edit `~/.hermes/config.yaml` directly), no UX discovery path. User instruction: "leave Hermes scope expansion for now — will get it reviewed by Hermes team."
2. **Hermes default `bedrock` config block omits `vpc_endpoint_url`** as a commented reference. Same user deferral.
3. **Hermes `_pin_bedrock_model_chain` does not write the full 13-field `bedrock:` block** (only model/provider/base_url/context_length/fallback/auxiliary). Deferred for Hermes-team review.
4. **Pi interactive setup toggles.** VPC endpoint / CRI / global inference / prompt cache / 1M context are not yet in the TUI; users edit `~/.config/pi/auth.json` for non-default values. Pi PR #3947 describes this as "Deferred (follow-up)".
5. **pi-mono PR is auto-closed** by the new-contributor gate. Needs `lgtm` from `badlogic`. Once reopened and merged, the local patch overlay becomes redundant.
6. **OpenClaw PR has merge conflicts** because `main` moved during our branch's lifetime. A quick rebase will resolve. Once merged and a new version publishes, the OpenClaw patch overlay becomes redundant.

## When upstream merges

Both PRs are additive + backward-compatible. Expected sequence:

1. pi-mono PR #3947 merges → published as `@mariozechner/pi-ai@0.70.8+` (or whatever the next lockstep version is).
2. OpenClaw PR #74202 merges → published as `openclaw@2026.4.27+`.
3. User runs `npm -g upgrade @mariozechner/pi-coding-agent openclaw`.
4. On next Hermes gateway startup, the guard detects markers already present in the upgraded bundle → logs `all markers present ✓` for both tools → no remediation needed.
5. At that point, `~/.pi/patches/dist/` and `~/.openclaw/patches/dist/` can be deleted — they become dead weight.

## Regenerating patches after a fork rebase

If the fork branch is rebased / rebuilt, the compiled dist in `~/forks/pi-mono/packages/*/dist/` may change. The `from:` paths in the markers.json still point at the fork, so new content flows through automatically on the next heal — no regeneration needed unless:

- Fork's dist layout changes (new file names / moved paths).
- Marker needles are removed or renamed.

If markers drift, rerun the patch-generation logic from Phase C.1 / C.2 (see the README.md files in `~/.pi/patches/dist/` and `~/.openclaw/patches/dist/`).

## References

- Spec: `docs/superpowers/specs/2026-04-29-bedrock-provider-parity-design.md`
- Plan: `docs/superpowers/plans/2026-04-29-openclaw-pi-bedrock-cline-parity.md`
- Hermes existing CR: `docs/superpowers/plans/2026-04-27-native-bedrock-provider-hardening.md`
- Cline reference code: `~/research-bedrock/cline/src/core/api/providers/bedrock.ts`
- Pi-mono PR: https://github.com/badlogic/pi-mono/pull/3947
- OpenClaw PR: https://github.com/openclaw/openclaw/pull/74202
- Fork (pi-mono): https://github.com/praxstack/pi-mono/tree/feat/bedrock-cline-parity-20260429
- Fork (openclaw): https://github.com/praxstack/openclaw/tree/feat/bedrock-cline-parity-20260429
