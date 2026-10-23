"""Credential source precedence and confirmation without a database."""
import importlib
import pytest
from cookbook.cli import build_parser
from cookbook.config import CLISettings


def credentials():
    return importlib.import_module("cookbook.cli_inputs").account_credentials


def test_explicit_file_does_not_read_bad_fallback(tmp_path):
    path = tmp_path / "explicit"
    path.write_text(" spaces preserved \n", encoding="utf-8")
    cfg = CLISettings(_env_file=None, DEBUG=False, FIRST_ADMIN_PASSWORD="wrong",
                      FIRST_ADMIN_PASSWORD_FILE=tmp_path / "missing")
    args = build_parser().parse_args(["setup", "--username", " OWNER ", "--password-file", str(path)])
    assert credentials()(args, cfg) == ("owner", " spaces preserved ", False)


def test_force_without_explicit_name_refused_before_prompts(monkeypatch):
    def forbidden(prompt):
        pytest.fail("recovery prompted before requiring explicit username")
    monkeypatch.setattr("builtins.input", forbidden)
    args = build_parser().parse_args(["setup", "--force-owner-recreation"])
    with pytest.raises(ValueError):
        credentials()(args, CLISettings(_env_file=None, DEBUG=False))


def test_reset_interactive_ignores_bootstrap_environment(monkeypatch):
    import getpass
    answers = iter(["new-password", "new-password"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    cfg = CLISettings(_env_file=None, DEBUG=False, FIRST_ADMIN_PASSWORD="wrong-bootstrap")
    args = build_parser().parse_args(["user", "reset-password", " OWNER ", "--interactive"])
    assert credentials()(args, cfg) == ("owner", "new-password", False)


def test_interactive_password_honors_explicit_username(monkeypatch):
    import getpass
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected username prompt"))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "new-password")
    args = build_parser().parse_args(["setup", "--username", " ALICE "])
    assert credentials()(args, CLISettings(_env_file=None)) == ("alice", "new-password", False)
