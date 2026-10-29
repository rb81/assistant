"""Terminal-owned authorization; passwords never pass through Python."""
import os
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar


_handler = ContextVar("setup_authorization_handler", default=None)


@contextmanager
def authorization_handler(handler):
    token = _handler.set(handler)
    try:
        yield
    finally:
        _handler.reset(token)


def authentication_required(stderr):
    return any(message in (stderr or "").lower() for message in (
        "sudo: a password is required", "sudo: a terminal is required",
        "sudo: authentication is required", "sudo: interactive authentication is required",
    ))


def request_authorization():
    handler = _handler.get()
    return bool(handler and handler())


def prepare_sudo(command, *, runner, environment=None):
    if _handler.get() is None:
        return
    from .setup import DockerCommandError

    def check():
        # Repeat only this non-executing policy check, never the privileged operation.
        env = dict(os.environ if environment is None else environment, LC_ALL="C")
        return runner(["sudo", "-n", "-l", "--", *command], capture_output=True,
                      text=True, stdin=subprocess.DEVNULL, timeout=15, env=env)

    result = check()
    if result.returncode and authentication_required(result.stderr) and request_authorization():
        result = check()
    if result.returncode:
        raise DockerCommandError(
            "sudo authorization failed: the required command is not authorized. "
            "Check your account's sudo policy. The command was not run.")


def terminal_authorization(*, confirmed=False):
    import questionary

    from .setup import SetupError

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SetupError("Administrator authorization requires an interactive terminal.")
    if not confirmed and not questionary.confirm(
        "This operation needs administrator authorization. Allow the system password prompt?",
        default=False,
    ).unsafe_ask():
        raise SetupError("Administrator authorization cancelled. The pending command was not run.")
    print("Only privileged commands use sudo. Assistant does not read or save your password.")
    try:
        result = subprocess.run(["sudo", "-v"], check=False)
    except (OSError, KeyboardInterrupt):
        raise SetupError("Administrator authorization interrupted or unavailable. The pending command was not run.") from None
    if result.returncode:
        raise SetupError("Administrator authorization failed. The pending command was not run.")
    return True
