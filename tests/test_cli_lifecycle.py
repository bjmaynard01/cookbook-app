"""CLI database failures are safe and engines stay within one event loop."""
import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from cookbook import cli_accounts as accounts
from cookbook.cli import build_parser
from cookbook.config import CLISettings


@pytest.mark.parametrize("failure_at", ["operation", "factory"])
def test_database_failure_is_safe_and_engine_disposed(monkeypatch, capsys, failure_at):
    loops = []

    class Engine:
        async def dispose(self):
            loops.append(asyncio.get_running_loop())

    def engine(*args, **kwargs):
        loops.append(asyncio.get_running_loop())
        return Engine()

    def fail():
        raise IntegrityError("query", {"password_hash": "sensitive-hash"}, Exception("sensitive-password"))

    def factory(*args, **kwargs):
        if failure_at == "factory":
            fail()
        return object()

    async def create(*args, **kwargs):
        fail()

    monkeypatch.setattr(accounts, "cli_config", lambda: CLISettings(_env_file=None))
    monkeypatch.setattr(accounts, "account_credentials", lambda *args: ("owner", "private-password", False))
    monkeypatch.setattr(accounts, "create_async_engine", engine)
    monkeypatch.setattr(accounts, "async_sessionmaker", factory)
    monkeypatch.setattr(accounts, "create_first_admin", create)
    args = build_parser().parse_args(["setup"])
    assert accounts.run_account(args) != 0
    output = capsys.readouterr()
    assert "Database failure" in output.err and not output.out
    assert "sensitive" not in output.err and "private-password" not in output.err
    assert len(loops) == 2 and loops[0] is loops[1]
