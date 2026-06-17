"""Interactive prompt callbacks for terminal_tool integration.

These bridge terminal_tool's interactive prompts (clarify, sudo, approval)
into prompt_toolkit's event loop. Each function takes the HermesCLI instance
as its first argument and uses its state (queues, app reference) to coordinate
with the TUI.
"""

import queue
import time as _time

from hermes_cli.banner import cprint, _DIM, _RST
from hermes_cli.config import save_env_value_secure
from hermes_cli.secret_prompt import masked_secret_prompt
from hermes_constants import display_hermes_home


def _notify_attention(title: str, message: str) -> None:
    """Fire a best-effort attention signal when an interactive prompt is
    waiting on the user. Critical for terminals (cmux, headless tmux panes)
    that do NOT surface prompt_toolkit prompts visually — without this the
    user can miss an approval and it times out to deny.

    Two channels, both non-blocking and failure-tolerant:
      1. macOS notification via ``osascript`` (backgrounded, 5s cap).
      2. Terminal BEL (\\a) — cmux/Ghostty/iTerm raise an attention badge.

    Gated by ``approvals.notify`` (default True). Never raises.
    """
    try:
        from cli import CLI_CONFIG
        if not CLI_CONFIG.get("approvals", {}).get("notify", True):
            return
    except Exception:
        pass
    # 1. Terminal bell — cheapest, surfaces in cmux/Ghostty even when unfocused.
    try:
        import sys
        sys.stderr.write("\a")
        sys.stderr.flush()
    except Exception:
        pass
    # 2. macOS notification (backgrounded so it never blocks the prompt loop).
    try:
        import sys as _sys
        if _sys.platform != "darwin":
            return
        import subprocess
        safe_title = title.replace('"', "'")[:120]
        safe_msg = message.replace('"', "'")[:240]
        script = (
            f'display notification "{safe_msg}" with title "{safe_title}" '
            f'sound name "Glass"'
        )
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass



def clarify_callback(cli, question, choices):
    """Prompt for clarifying question through the TUI.

    Sets up the interactive selection UI, then blocks until the user
    responds. Returns the user's choice or a timeout message.
    """
    from cli import CLI_CONFIG

    timeout = CLI_CONFIG.get("clarify", {}).get("timeout", 120)
    response_queue = queue.Queue()
    is_open_ended = not choices

    cli._clarify_state = {
        "question": question,
        "choices": choices if not is_open_ended else [],
        "selected": 0,
        "response_queue": response_queue,
    }
    cli._clarify_deadline = _time.monotonic() + timeout
    cli._clarify_freetext = is_open_ended

    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()

    while True:
        try:
            result = response_queue.get(timeout=1)
            cli._clarify_deadline = 0
            return result
        except queue.Empty:
            remaining = cli._clarify_deadline - _time.monotonic()
            if remaining <= 0:
                break
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()

    cli._clarify_state = None
    cli._clarify_freetext = False
    cli._clarify_deadline = 0
    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()
    cprint(f"\n{_DIM}(clarify timed out after {timeout}s — agent will decide){_RST}")
    return (
        "The user did not provide a response within the time limit. "
        "Use your best judgement to make the choice and proceed."
    )


def prompt_for_secret(cli, var_name: str, prompt: str, metadata=None) -> dict:
    """Prompt for a secret value through the TUI (e.g. API keys for skills).

    Returns a dict with keys: success, stored_as, validated, skipped, message.
    The secret is stored in ~/.hermes/.env and never exposed to the model.
    """
    if not getattr(cli, "_app", None):
        if not hasattr(cli, "_secret_state"):
            cli._secret_state = None
        if not hasattr(cli, "_secret_deadline"):
            cli._secret_deadline = 0
        try:
            value = masked_secret_prompt(f"{prompt} (hidden, ESC or empty Enter to skip): ")
        except (EOFError, KeyboardInterrupt):
            value = ""

        if not value:
            cprint(f"\n{_DIM}  ⏭ Secret entry skipped{_RST}")
            return {
                "success": True,
                "reason": "cancelled",
                "stored_as": var_name,
                "validated": False,
                "skipped": True,
                "message": "Secret setup was skipped.",
            }

        stored = save_env_value_secure(var_name, value)
        _dhh = display_hermes_home()
        cprint(f"\n{_DIM}  ✓ Stored secret in {_dhh}/.env as {var_name}{_RST}")
        return {
            **stored,
            "skipped": False,
            "message": "Secret stored securely. The secret value was not exposed to the model.",
        }

    timeout = 120
    response_queue = queue.Queue()

    cli._secret_state = {
        "var_name": var_name,
        "prompt": prompt,
        "metadata": metadata or {},
        "response_queue": response_queue,
    }
    cli._secret_deadline = _time.monotonic() + timeout
    # Avoid storing stale draft input as the secret when Enter is pressed.
    if hasattr(cli, "_clear_secret_input_buffer"):
        try:
            cli._clear_secret_input_buffer()
        except Exception:
            pass
    elif hasattr(cli, "_app") and cli._app:
        try:
            cli._app.current_buffer.reset()
        except Exception:
            pass

    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()

    while True:
        try:
            value = response_queue.get(timeout=1)
            cli._secret_state = None
            cli._secret_deadline = 0
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()

            if not value:
                cprint(f"\n{_DIM}  ⏭ Secret entry skipped{_RST}")
                return {
                    "success": True,
                    "reason": "cancelled",
                    "stored_as": var_name,
                    "validated": False,
                    "skipped": True,
                    "message": "Secret setup was skipped.",
                }

            stored = save_env_value_secure(var_name, value)
            _dhh = display_hermes_home()
            cprint(f"\n{_DIM}  ✓ Stored secret in {_dhh}/.env as {var_name}{_RST}")
            return {
                **stored,
                "skipped": False,
                "message": "Secret stored securely. The secret value was not exposed to the model.",
            }
        except queue.Empty:
            remaining = cli._secret_deadline - _time.monotonic()
            if remaining <= 0:
                break
            if hasattr(cli, "_app") and cli._app:
                cli._app.invalidate()

    cli._secret_state = None
    cli._secret_deadline = 0
    if hasattr(cli, "_clear_secret_input_buffer"):
        try:
            cli._clear_secret_input_buffer()
        except Exception:
            pass
    elif hasattr(cli, "_app") and cli._app:
        try:
            cli._app.current_buffer.reset()
        except Exception:
            pass
    if hasattr(cli, "_app") and cli._app:
        cli._app.invalidate()
    cprint(f"\n{_DIM}  ⏱ Timeout — secret capture cancelled{_RST}")
    return {
        "success": True,
        "reason": "timeout",
        "stored_as": var_name,
        "validated": False,
        "skipped": True,
        "message": "Secret setup timed out and was skipped.",
    }


def approval_callback(cli, command: str, description: str) -> str:
    """Prompt for dangerous command approval through the TUI.

    Shows a selection UI with choices: once / session / always / deny.
    When the command is longer than 70 characters, a "view" option is
    included so the user can reveal the full text before deciding.

    Uses cli._approval_lock to serialize concurrent requests (e.g. from
    parallel delegation subtasks) so each prompt gets its own turn.
    """
    lock = getattr(cli, "_approval_lock", None)
    if lock is None:
        import threading
        cli._approval_lock = threading.Lock()
        lock = cli._approval_lock

    with lock:
        from cli import CLI_CONFIG
        timeout = CLI_CONFIG.get("approvals", {}).get("timeout", 60)
        response_queue = queue.Queue()
        choices = ["once", "session", "always", "deny"]
        if len(command) > 70:
            choices.append("view")

        cli._approval_state = {
            "command": command,
            "description": description,
            "choices": choices,
            "selected": 0,
            "response_queue": response_queue,
        }
        cli._approval_deadline = _time.monotonic() + timeout

        # Attention signal (bell + macOS notification) so the prompt isn't
        # missed in terminals that don't surface it visually (cmux/tmux).
        # Without this, an unseen prompt times out to "deny" after `timeout`s.
        _notify_attention(
            "Hermes — approval needed",
            (description or command or "A command needs your approval")[:200],
        )

        if hasattr(cli, "_app") and cli._app:
            cli._app.invalidate()

        # Re-notify with backoff: if the first attention signal was missed,
        # fire follow-ups as the deadline approaches. Fractions of the total
        # timeout window remaining at which to re-alert (escalating urgency).
        _renotify_at = sorted(
            {timeout * 0.5, timeout * 0.2, 12.0}, reverse=True
        )
        _renotified = set()

        while True:
            try:
                result = response_queue.get(timeout=1)
                cli._approval_state = None
                cli._approval_deadline = 0
                if hasattr(cli, "_app") and cli._app:
                    cli._app.invalidate()
                return result
            except queue.Empty:
                remaining = cli._approval_deadline - _time.monotonic()
                if remaining <= 0:
                    break
                # Escalating re-notification for missed prompts.
                for mark in _renotify_at:
                    if remaining <= mark and mark not in _renotified:
                        _renotified.add(mark)
                        _notify_attention(
                            "Hermes — approval still waiting",
                            f"{int(remaining)}s left before auto-deny: "
                            + (description or command or "command")[:160],
                        )
                        break
                if hasattr(cli, "_app") and cli._app:
                    cli._app.invalidate()

        cli._approval_state = None
        cli._approval_deadline = 0
        if hasattr(cli, "_app") and cli._app:
            cli._app.invalidate()
        cprint(f"\n{_DIM}  ⏱ Timeout — denying command{_RST}")
        _notify_attention(
            "Hermes — command auto-denied",
            "Approval timed out with no response; command was denied.",
        )
        return "deny"
