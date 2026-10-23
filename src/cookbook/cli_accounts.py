from __future__ import annotations

import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import SQLAlchemyError

from .auth.bootstrap import (
    AlreadyBootstrapped,
    LockRowMissing,
    UserNotFound,
    create_first_admin,
    reset_password,
)
from .cli_inputs import account_credentials
from .config import cli_config

INPUT_ERROR = (
    "Input was invalid (passwords must be at least 8 characters; no "
    "complexity rules). Check the command arguments and try again."
)
DB_ERROR = (
    "Database failure: could not complete the operation. Check the DB settings "
    "and that the server is reachable, then retry."
)


async def _perform(args, url: str, username: str, password: str, restore: bool) -> None:
    """Run the account operation; engine disposed on EVERY outcome."""
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        if restore:
            # setup --force-owner-recreation: recover the named account.
            await reset_password(username, password, restore_admin=True, factory=factory)
            success = "Existing account restored as active admin; password and sessions reset."
        elif args.command == "setup":
            await create_first_admin(username, password, factory=factory)
            success = "Owner account created."
        else:
            await reset_password(username, password, factory=factory)
            success = "Password reset completed."
    finally:
        # dispose on success AND failure so no pooled sockets outlive the run.
        await engine.dispose()
    print(success)


def run_account(args) -> int:
    try:
        cfg = cli_config()
        username, password, restore = account_credentials(args, cfg)
        asyncio.run(_perform(args, cfg.database_url, username, password, restore))
    except (EOFError, KeyboardInterrupt):
        # Credential input interrupted: cancel cleanly, no traceback.
        print("Cancelled; no changes were made.", file=sys.stderr)
        return 130
    except AlreadyBootstrapped:
        print(
            "An admin account already exists. To recover an existing "
            "account as admin, re-run 'setup' with --force-owner-recreation.",
            file=sys.stderr,
        )
        return 1
    except UserNotFound:
        print("No such account exists.", file=sys.stderr)
        return 1
    except LockRowMissing:
        print(
            "Required auth_locks row is missing; refusing to continue. "
            "Verify the database migration/setup state.", file=sys.stderr,
        )
        return 1
    except SQLAlchemyError:
        # Fixed message: stringifying the error can leak DB parameters.
        print(DB_ERROR, file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError):
        print(INPUT_ERROR, file=sys.stderr)
        return 1
    # Any other exception propagates: never mask a real bug as success.
    return 0
