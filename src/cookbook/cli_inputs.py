"""CLI credential input helpers (task 7.7a).

Pure input parsing only: builds (username, password, restore_admin) from
already-parsed ``args`` and the passed-in CLI config. No DB, no web settings,
no raw passwords in logs or exception messages.
"""
from __future__ import annotations

import getpass
import logging

from cookbook.auth.bootstrap import normalize_username
from cookbook.config import CLISettings, read_password_file

logger = logging.getLogger("cookbook.cli")


def _prompt_password_twice() -> str:
    """Interactive password + confirmation; both reads via getpass."""
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise ValueError("Passwords did not match; please try again.")
    return first


def _password_from_source(
    password_file: str | None, cfg: CLISettings
) -> tuple[str, bool]:
    """Resolve password: explicit file > cfg file > cfg plain > interactive.

    Returns (password, used_plain_cfg_env). The plain env value is only
    chosen when no higher-precedence source is set; the caller warns if
    cfg.debug is False.
    """
    if password_file is not None:
        return read_password_file(password_file), False
    if cfg.first_admin_password_file is not None:
        return read_password_file(cfg.first_admin_password_file), False
    if cfg.first_admin_password is not None:
        return cfg.first_admin_password, True
    return _prompt_password_twice(), False


def account_credentials(args, cfg: CLISettings) -> tuple[str, str, bool]:
    """Resolve credentials for setup / user reset-password commands.

    Returns (normalized_username, password, restore_admin_bool).
    ``restore_admin`` is True ONLY for ``setup --force-owner-recreation``.
    """
    command = args.command

    if command == "setup":
        username = getattr(args, "username", None)
        force = bool(getattr(args, "force_owner_recreation", False))
        password_file = getattr(args, "password_file", None)

        # Noninteractive setup (a password file was given, or the config set a
        # password file / env password) CANNOT fall back to prompting for a
        # username either, so an explicit --username is required there.
        noninteractive = (
            password_file is not None
            or cfg.first_admin_password_file is not None
            or cfg.first_admin_password is not None
        )
        if force or noninteractive:
            # --force-owner-recreation names the account to recover; a
            # noninteractive password source can't prompt for one.
            if not username:
                raise ValueError(
                    "Explicit --username is required when using "
                    "--force-owner-recreation or a noninteractive password "
                    "source."
                )
            username = normalize_username(username)
        elif username:
            # Interactive setup with an explicitly supplied --username: honor
            # the given name instead of prompting.
            username = normalize_username(username)
        else:
            # Interactive setup with no name supplied: default to owner.
            username = normalize_username(input("Username (default owner): ") or "owner")

        password, used_plain = _password_from_source(password_file, cfg)
        if used_plain and not cfg.debug:
            logger.warning(
                "Bootstrap password came from FIRST_ADMIN_PASSWORD environment "
                "variable (plain value, not file); enable DEBUG to silence."
            )

        restore_admin = False
        if force:
            # Recovery: restore the NAMED existing account as active admin and
            # reset its password and active sessions. Never print the
            # password or hash.
            logger.warning(
                "WARNING: --force-owner-recreation will restore the existing "
                f"account '{username}' as the active admin and reset its "
                "password and active sessions."
            )
            expected = f"yes/{username}"
            confirmation = input(f"Type exactly '{expected}' to proceed: ")
            if confirmation != expected:
                raise ValueError("Confirmation did not match the required token.")
            restore_admin = True

        return username, password, restore_admin

    if command == "user" and getattr(args, "user_command", None) == "reset-password":
        username = normalize_username(args.username)
        password_file = getattr(args, "password_file", None)
        if password_file is not None:
            password = read_password_file(password_file)
        else:
            # reset-password parser requires either --password-file or
            # --interactive, so reaching here means interactive prompting.
            password = _prompt_password_twice()
        return username, password, False

    raise ValueError(f"Unsupported command for credential inputs: {command!r}")
