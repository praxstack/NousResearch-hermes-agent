# Tech debt: B2 — per-client bearer token binding (deferred 2026-06-01)

## Status: TRACKED, not yet implemented. Council-approved deferral.

## Context
The NoAuthTokenError fix (commits acfd44498, 9f12c8641, + this B1/B3 commit) makes
bearer auth work by PERSISTING `AWS_BEARER_TOKEN_BEDROCK` into the global
`os.environ` for the process lifetime, because the anthropic SDK's bearer-auth
monkeypatch (`_install_apex_bearer_auth_patch`) reads the token from os.environ
lazily at request time, and the SDK exposes no explicit bearer-token kwarg.

## Why B2 is the proper long-term fix
A 3-stage llm-council deliberation (2026-06-01, conversation a8a4a96f) unanimously
identified that binding the token to global mutable state is the wrong layer:
- Process-lifetime token in os.environ is inherited by every subprocess
  (checkpoint_manager git ops, pty_bridge, web_server, kanban all do
  `os.environ.copy()` then spawn).
- It couples auth to a global the way the original bug did.

B2 = make the monkeypatch read the token from a passed-in value / `contextvars`
(thread-local, async-safe) bound at client construction, instead of os.environ.
This eliminates the env mutation, the subprocess-inheritance surface, and the
mask race simultaneously.

## Why it was deferred (not done now)
- B2 rewrites the EXACT monkeypatch (`_bearer_aware_get_auth_headers`) that just
  caused a P0. Rewriting it during a hotfix is how you get a second outage.
- It requires validating that the anthropic SDK's retry/stream machinery
  preserves contextvar scope across internal threads/async boundaries — real
  validation work, not a quick edit.
- The immediate race is already CLOSED by B1 (token removed from all masks;
  reproduced 0/210k empty reads after, vs 186k/246k before).

## Trigger to implement B2 (escalate to council when ANY fires)
1. Bedrock auth ever needs to support BOTH api_key (bearer) AND IAM creds in the
   same process (the B3 warning fires in the gateway, not just an interactive shell).
2. A subprocess/crash-reporter is added that dumps env (Sentry, etc.).
3. The anthropic SDK adds a native bearer-token kwarg (then bind it explicitly,
   delete the monkeypatch + the env persist).

## Acceptance criteria for B2 (when done)
- Token is NEVER written to os.environ in api_key mode.
- A concurrent default_chain/profile/credentials build cannot affect an api_key
  request's auth (already true post-B1, must STAY true).
- `/proc`-equivalent + subprocess inheritance no longer exposes the token.
- All existing adapter tests pass; the request-time-read test
  (`test_bearer_patch_reads_token_from_environ_at_request_time`) is rewritten to
  assert per-client binding instead of env read.
- SDK retry + streaming paths verified to preserve the bound token.

## References
- Council conversation id: a8a4a96f-000d-49d5-a287-3c89e3c5e224
- Commits: acfd44498 (fix), 9f12c8641 (RCA correction + tests), this commit (B1+B3)
- bedrock_adapter.py mask definitions + resolve_bedrock_auth_config B3 guard
- anthropic_adapter.py _install_apex_bearer_auth_patch (the request-time reader)
