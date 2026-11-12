"""CLI secret generation and account operations without web SECRET_KEY."""
import asyncio
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from cookbook.auth import sessions
from cookbook.auth.passwords import verify_password
from cookbook.db import session_scope
from cookbook.models import Session, User
from bootstrap_support import PASSWORD, probe


def cli():
    return importlib.import_module("cookbook.cli")


def call(args):
    try:
        return cli().main(args)
    except SystemExit as exc:
        return exc.code


def password_file(tmp_path, value=PASSWORD, name="password.txt"):
    path = tmp_path / name
    path.write_text(value + "\n", encoding="utf-8")
    return str(path)


def child_env():
    # Only OS essentials and the explicit throwaway DB connection are inherited.
    names = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "COMSPEC",
             "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}
    env = {name: value for name, value in os.environ.items() if name in names}
    env["DEBUG"] = "false"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return env


def test_init_secret_is_independent_of_config_and_refuses_overwrite(monkeypatch, tmp_path, capsys):
    module = cli()
    def forbidden(*args, **kwargs):
        pytest.fail("init-secret read account/DB configuration")
    monkeypatch.setattr(module, "cli_config", forbidden)
    output = tmp_path / "secret.key"
    assert call(["init-secret", "--out", str(output)]) == 0
    first = output.read_text(encoding="utf-8").strip()
    assert len(first) == 64 and int(first, 16) >= 0
    assert first not in capsys.readouterr().out
    assert call(["init-secret", "--out", str(output)]) != 0
    assert output.read_text(encoding="utf-8").strip() == first
    assert call(["init-secret", "--out", str(output), "--overwrite"]) == 0
    assert output.read_text(encoding="utf-8").strip() != first


def test_init_secret_subprocess_before_secret_key(tmp_path):
    env = child_env()
    env["SECRET_KEY_FILE"] = str(tmp_path / "missing-web-key")
    output = tmp_path / "generated.key"
    result = subprocess.run([sys.executable, "-m", "cookbook.cli", "init-secret", "--out", str(output)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert output.exists() and len(output.read_text().strip()) == 64
    assert output.read_text().strip() not in result.stdout + result.stderr


def test_cli_setup_subprocess_without_secret_key(tmp_path):
    async def body():
        path = password_file(tmp_path)
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "cookbook.cli", "setup", "--username", "owner", "--password-file", path],
            cwd=tmp_path, env=child_env(), capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert PASSWORD not in result.stdout + result.stderr
        async with session_scope() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert user.role == "admin" and user.is_active and verify_password(PASSWORD, user.password_hash)
    probe(body)


def test_interactive_setup_prompts_without_echoing_password(monkeypatch, capsys):
    async def body():
        import getpass
        monkeypatch.setattr("builtins.input", lambda prompt: " Owner ")
        answers = iter([PASSWORD, PASSWORD])
        monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
        assert await asyncio.to_thread(call, ["setup"]) == 0
        async with session_scope() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert user.username == "owner" and verify_password(PASSWORD, user.password_hash)
        captured = capsys.readouterr()
        assert PASSWORD not in captured.out + captured.err
    probe(body)


def test_explicit_password_file_overrides_bad_bootstrap_fallback(monkeypatch, tmp_path):
    async def body():
        monkeypatch.setenv("FIRST_ADMIN_PASSWORD_FILE", str(tmp_path / "missing-fallback"))
        monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "wrong-plain")
        path = password_file(tmp_path, " spaced password ")
        assert await asyncio.to_thread(call, ["setup", "--username", "owner", "--password-file", path]) == 0
        async with session_scope() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert verify_password(" spaced password ", user.password_hash)
            assert not verify_password("spaced password", user.password_hash)
    probe(body)


def test_bootstrap_file_beats_plain_environment(monkeypatch, tmp_path):
    async def body():
        monkeypatch.setenv("FIRST_ADMIN_PASSWORD_FILE", password_file(tmp_path))
        monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "wrong-plain")
        assert await asyncio.to_thread(call, ["setup", "--username", "owner"]) == 0
        async with session_scope() as db:
            assert verify_password(PASSWORD, (await db.execute(select(User))).scalar_one().password_hash)
    probe(body)


def test_plain_bootstrap_env_warns_without_disclosing_password(monkeypatch, caplog):
    async def body():
        monkeypatch.setenv("FIRST_ADMIN_PASSWORD", PASSWORD)
        monkeypatch.delenv("FIRST_ADMIN_PASSWORD_FILE", raising=False)
        monkeypatch.setenv("DEBUG", "false")
        assert await asyncio.to_thread(call, ["setup", "--username", "owner"]) == 0
        assert "FIRST_ADMIN_PASSWORD" in caplog.text
        assert PASSWORD not in caplog.text
    probe(body)


def test_short_password_and_confirmation_mismatch_create_nothing(monkeypatch, tmp_path):
    async def body():
        assert await asyncio.to_thread(call, ["setup", "--username", "owner", "--password-file",
                                              password_file(tmp_path, "short")]) != 0
        import getpass
        answers = iter([PASSWORD, "different-password"])
        monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
        assert await asyncio.to_thread(call, ["setup", "--username", "owner"]) != 0
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0
    probe(body)


def test_cli_password_reset_wipes_sessions(tmp_path):
    async def body():
        from cookbook.auth import bootstrap
        user = await bootstrap.create_first_admin("owner", PASSWORD)
        await sessions.create_session(user.id)
        path = password_file(tmp_path, "replacement-password")
        assert await asyncio.to_thread(call, ["user", "reset-password", "OWNER", "--password-file", path]) == 0
        async with session_scope() as db:
            assert verify_password("replacement-password", (await db.get(User, user.id)).password_hash)
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
    probe(body)


def test_recovery_requires_confirmation_and_named_existing_account(monkeypatch, tmp_path):
    async def body():
        from cookbook.auth import bootstrap
        user = await bootstrap.create_first_admin("owner", PASSWORD)
        async with session_scope() as db:
            stored = await db.get(User, user.id)
            stored.role, stored.is_active = "editor", False
        path = password_file(tmp_path, "replacement-password")
        args = ["setup", "--username", "owner", "--password-file", path, "--force-owner-recreation"]
        monkeypatch.setattr("builtins.input", lambda prompt: "no")
        assert await asyncio.to_thread(call, args) != 0
        async with session_scope() as db:
            stored = await db.get(User, user.id)
            assert stored.role == "editor" and not stored.is_active
        monkeypatch.setattr("builtins.input", lambda prompt: "yes/owner")
        assert await asyncio.to_thread(call, args) == 0
        async with session_scope() as db:
            stored = await db.get(User, user.id)
            assert stored.role == "admin" and stored.is_active
            assert verify_password("replacement-password", stored.password_hash)
        monkeypatch.setattr("builtins.input", lambda prompt: "yes/nonexistent")
        assert await asyncio.to_thread(call, ["setup", "--username", "nonexistent", "--password-file", path,
                                              "--force-owner-recreation"]) != 0
        assert await asyncio.to_thread(call, ["setup", "--password-file", path, "--force-owner-recreation"]) != 0
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
    probe(body)


RESTORE_FLAGS = {
    "--sql": "backup/cookbook-2026-11-10.sql.gz",
    "--media-archive": "backup/media-2026-11-10.tar.gz",
    "--manifest": "backup/manifest-2026-11-10.json",
    "--db-url": "mariadb+aiomysql://cookbook:secret@127.0.0.1/cookbook",
    "--media-dir": "media/restore-target",
}


def _restore_argv(missing=None):
    argv = ["restore"]
    for flag, value in RESTORE_FLAGS.items():
        if flag != missing:
            argv += [flag, value]
    return argv


def install_fake_cli_backup(monkeypatch):
    """Spy module occupying cookbook.cli_backup; proves dispatch without the real file."""
    import types

    fake = types.ModuleType("cookbook.cli_backup")
    fake.recorded = []

    def run_backup_command(args):
        fake.recorded.append(("backup", args))
        return 7

    def run_restore_command(args):
        fake.recorded.append(("restore", args))
        return 8

    fake.run_backup_command = run_backup_command
    fake.run_restore_command = run_restore_command
    monkeypatch.setitem(sys.modules, "cookbook.cli_backup", fake)
    return fake


def test_backup_parser_requires_out(tmp_path):
    module = cli()
    args = module.build_parser().parse_args(["backup", "--out", str(tmp_path / "backups")])
    assert args.command == "backup"
    assert args.out == tmp_path / "backups"
    assert call(["backup"]) != 0  # --out is required, no silent default


def test_restore_parser_accepts_explicit_sources_and_targets():
    args = cli().build_parser().parse_args(_restore_argv())
    assert args.command == "restore"
    for flag, expected in RESTORE_FLAGS.items():
        attribute = flag.lstrip("-").replace("-", "_")
        value = getattr(args, attribute)
        if flag == "--db-url":
            assert value == expected  # URL stays a plain string
        else:
            assert value == Path(expected)
    assert args.replace is False


@pytest.mark.parametrize("flag", list(RESTORE_FLAGS))
def test_restore_missing_required_flag_refused(flag):
    assert call(_restore_argv(missing=flag)) != 0


def test_restore_replace_flag_defaults_false_and_can_be_set():
    assert cli().build_parser().parse_args(_restore_argv()).replace is False
    assert cli().build_parser().parse_args(_restore_argv() + ["--replace"]).replace is True


def test_parsing_does_not_import_cli_backup(monkeypatch):
    # sys.modules = None makes any import of the module raise ImportError,
    # so this proves parsing alone never triggers the lazy import.
    monkeypatch.setitem(sys.modules, "cookbook.cli_backup", None)
    assert cli().build_parser().parse_args(["backup", "--out", "backups"]).command == "backup"
    assert cli().build_parser().parse_args(_restore_argv()).command == "restore"


def test_unrelated_command_does_not_load_backup_module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "cookbook.cli_backup", None)
    assert call(["init-secret", "--out", str(tmp_path / "secret.key")]) == 0


def test_backup_dispatch_forwards_args_to_cli_backup(monkeypatch, tmp_path):
    fake = install_fake_cli_backup(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("backup dispatch read DB configuration inside cli.py")
    monkeypatch.setattr(cli(), "cli_config", forbidden)

    destination = tmp_path / "backups"
    assert call(["backup", "--out", str(destination)]) == 7
    (name, args), = fake.recorded
    assert name == "backup" and args.out == destination


def test_restore_dispatch_forwards_args_and_replace_flag(monkeypatch):
    fake = install_fake_cli_backup(monkeypatch)
    assert call(_restore_argv()) == 8
    (name, args), = fake.recorded
    assert name == "restore"
    assert args.sql == Path(RESTORE_FLAGS["--sql"])
    assert args.media_archive == Path(RESTORE_FLAGS["--media-archive"])
    assert args.manifest == Path(RESTORE_FLAGS["--manifest"])
    assert args.db_url == RESTORE_FLAGS["--db-url"]
    assert args.media_dir == Path(RESTORE_FLAGS["--media-dir"])
    assert args.replace is False
    assert call(_restore_argv() + ["--replace"]) == 8
    name, args = fake.recorded[-1]
    assert name == "restore" and args.replace is True


def test_cli_and_http_share_one_bootstrap_guard(monkeypatch, tmp_path):
    from test_setup_routes import submit, web_probe
    from test_login_routes import token
    async def body(app, client):
        page = await client.get("/setup")
        path = password_file(tmp_path)
        code, response = await asyncio.gather(asyncio.to_thread(call,
            ["setup", "--username", "owner-cli", "--password-file", path]), submit(client, token(page)))
        assert (code == 0 and response.status_code == 403) or (code != 0 and response.status_code == 302)
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
    web_probe(monkeypatch, body)
