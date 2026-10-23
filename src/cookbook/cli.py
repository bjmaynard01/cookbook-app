"""Command-line interface for cookbook maintenance commands.

``init-secret`` is deliberately dependency-free (stdlib only at runtime) so
it can run before the rest of the app is set up.  The module still imports
``cli_config`` from :mod:`cookbook.config` for the account subcommands, but
``init-secret`` never calls it nor any DB/SECRET_KEY code.  Account commands
(``setup``, ``user reset-password``) lazily import
:mod:`cookbook.cli_accounts` so secret generation needs no database setup.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

# Imported for the account subcommands.
# `init-secret` below must never call this.
from .config import cli_config


def _run_init_secret(args: argparse.Namespace) -> int:
    """Write a fresh 32-byte hex key to args.out (default: secret.key)."""
    destination: Path = args.out
    # 32 hex bytes = 256 bits of key material, one line.
    token = secrets.token_hex(32)
    if args.overwrite:
        # --overwrite is a deliberate key rotation: O_TRUNC replaces the file.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    else:
        # Default is atomic creation; O_EXCL makes an existing file an error
        # instead of a silent clobber of a key someone is already using.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError:
        # Fixed message: no path detail beyond the destination we were given.
        print(f"Could not write {destination}.", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    try:
        # Best-effort: some filesystems ignore the os.open mode bits, so
        # enforce owner-only access after the fact if possible.
        os.chmod(destination, 0o600)
    except OSError:
        pass
    # Destination path only — never the key itself.
    print(destination)
    return 0


def _run_setup(args: argparse.Namespace) -> int:
    # Keep database imports out of secret generation.
    from .cli_accounts import run_account

    return run_account(args)


def _run_reset_password(args: argparse.Namespace) -> int:
    # Keep database imports out of secret generation.
    from .cli_accounts import run_account

    return run_account(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cookbook",
        description="Cookbook maintenance commands.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_secret = subparsers.add_parser(
        "init-secret",
        help="Generate a 32-byte hex SECRET_KEY file (default: secret.key).",
    )
    init_secret.add_argument(
        "--out",
        type=Path,
        default=Path("secret.key"),
        help="Destination file (default: secret.key).",
    )
    init_secret.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing file (deliberate key rotation).",
    )
    init_secret.set_defaults(handler=_run_init_secret)

    setup = subparsers.add_parser(
        "setup",
        help="Create the owner account (first run only).",
    )
    setup.add_argument("--username", help="Owner account name.")
    setup.add_argument(
        "--password-file",
        type=Path,
        help="File whose first line is the owner password (never pass it on the command line).",
    )
    setup.add_argument(
        "--force-owner-recreation",
        action="store_true",
        help="Recover a named existing account as active admin; requires confirmation.",
    )
    setup.set_defaults(handler=_run_setup)

    user = subparsers.add_parser("user", help="User management.")
    user_sub = user.add_subparsers(dest="user_command", required=True)
    reset = user_sub.add_parser(
        "reset-password",
        help="Reset one user's password.",
    )
    reset.add_argument("username", help="Account whose password should be reset.")
    # Password comes from exactly one source: a file, or interactively.
    # There is intentionally no plain --password flag.
    password_group = reset.add_mutually_exclusive_group(required=True)
    password_group.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for the new password (hidden input).",
    )
    password_group.add_argument(
        "--password-file",
        type=Path,
        help="File whose first line is the new password.",
    )
    reset.set_defaults(handler=_run_reset_password)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
