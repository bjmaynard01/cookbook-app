"""Lean command configuration works without web session secrets."""
import os
from pathlib import Path

import pytest
from cookbook import config


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("DB_", "SECRET_", "FIRST_ADMIN_", "DEBUG", "MEDIA_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_cli_config_never_requires_or_reads_web_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / "missing-web-secret"))
    monkeypatch.setenv("DB_PASSWORD", "db-password")
    cfg = config.cli_config()
    assert cfg.db_host == "127.0.0.1" and cfg.db_port == 3306
    assert cfg.db_password == "db-password"
    assert cfg.database_url.startswith("mysql+asyncmy://")


def test_cli_db_file_wins_and_bootstrap_file_is_lazy(monkeypatch, tmp_path):
    path = tmp_path / "db-pw"
    path.write_text("file-db-password\n", encoding="utf-8")
    monkeypatch.setenv("DB_PASSWORD_FILE", str(path))
    monkeypatch.setenv("DB_PASSWORD", "wrong")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD_FILE", str(tmp_path / "not-read-yet"))
    cfg = config.cli_config()
    assert cfg.db_password == "file-db-password"
    assert cfg.first_admin_password_file == tmp_path / "not-read-yet"


def test_bootstrap_password_file_preserves_spaces_and_wins(monkeypatch, tmp_path):
    path = tmp_path / "owner-pw"
    path.write_text(" 123456 \nignored-second-line\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY", "web-test-key")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD", "wrong-plain")
    monkeypatch.setenv("FIRST_ADMIN_PASSWORD_FILE", str(path))
    assert config.get_settings().first_admin_password == " 123456 "


def test_cli_database_url_encodes_password_characters(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "a@b:/?#x")
    cfg = config.cli_config()
    from sqlalchemy.engine import make_url
    assert make_url(cfg.database_url).password == "a@b:/?#x"


def test_dotenv_cli_database_and_bootstrap_sources(tmp_path):
    (tmp_path / ".env").write_text("DB_HOST=test-db\nDB_PORT=33061\nFIRST_ADMIN_PASSWORD=dotenv-password\n", encoding="utf-8")
    cfg = config.cli_config()
    assert cfg.db_host == "test-db" and cfg.db_port == 33061
    assert cfg.first_admin_password == "dotenv-password"


def test_web_dotenv_secret_file_paths_are_resolved_file_first(tmp_path):
    key = tmp_path / "web-key"
    db = tmp_path / "db-key"
    key.write_text("file-web-key\n", encoding="utf-8")
    db.write_text("file-db-password\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"SECRET_KEY_FILE={key.as_posix()}\nDB_PASSWORD_FILE={db.as_posix()}\n"
        "SECRET_KEY=wrong-plain\nDB_PASSWORD=wrong-db\n", encoding="utf-8")
    cfg = config.get_settings()
    assert cfg.secret_key == "file-web-key"
    assert cfg.db_password == "file-db-password"
