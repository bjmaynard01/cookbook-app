from __future__ import annotations

import os
from pathlib import Path

import pytest

from cookbook import config as config_mod
from cookbook.config import Settings, _from_secret_file


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clean env for each test: strip inherited DB_/SECRET_/... vars, and chdir
    OUT of the repo root so Settings (env_file='.env', relative) cannot pick up
    the local .env. Env vars are the sole config source in these tests."""
    for var in list(os.environ):
        if var.startswith(("DB_", "SECRET_", "APP_", "DEBUG", "MEDIA_", "ALLOWED_", "UPLOAD_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def test_database_url_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "devsecretkey")
    monkeypatch.setenv("DB_HOST", "db")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_NAME", "cookbook")
    monkeypatch.setenv("DB_USER", "cookbook")
    monkeypatch.setenv("DB_PASSWORD", "devpassword")
    s = Settings()
    assert s.database_url == (
        "mysql+asyncmy://cookbook:devpassword@db:3306/cookbook?charset=utf8mb4"
    )


def test_database_url_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setenv("DB_USER", "cookbook")
    s = Settings()
    assert s.database_url == "mysql+asyncmy://cookbook@127.0.0.1:3306/cookbook?charset=utf8mb4"


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "k")
    s = Settings()
    assert s.db_port == 3306
    assert s.db_name == "cookbook"
    assert s.db_user == "cookbook"
    assert s.media_dir == Path("/media")
    assert s.session_ttl_hours == 12


def test_from_secret_file_first_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "secret"
    f.write_text("filesecret\n\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY_FILE", str(f))
    monkeypatch.setenv("SECRET_KEY", "plainsecret")
    assert _from_secret_file("SECRET_KEY") == "filesecret"


def test_from_secret_file_unreadable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="DB_PASSWORD_FILE"):
        _from_secret_file("DB_PASSWORD")


def test_get_settings_prefers_file_over_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "secret"
    f.write_text("fromfile\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY_FILE", str(f))
    monkeypatch.setenv("SECRET_KEY", "plain")
    s = config_mod.get_settings()
    assert s.secret_key == "fromfile"


def test_get_settings_refuses_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SECRET_KEY and no SECRET_KEY_FILE anywhere -> RuntimeError at boot (D20):
    the app fails loudly instead of starting with a default session key."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    config_mod.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config_mod.get_settings()
