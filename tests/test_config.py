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
        prefixes = (
            "DB_", "SECRET_", "APP_", "DEBUG", "MEDIA_", "ALLOWED_", "UPLOAD_",
            "TRUSTED_PROXY", "COOKIE_", "LLM_",
        )
        if var.startswith(prefixes):
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


def test_upload_limit_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "0")
    with pytest.raises(ValueError):
        Settings()


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
    assert s.llm_base_url is None
    assert s.llm_model is None
    assert s.llm_api_key is None
    assert s.llm_api_key_file is None
    assert s.llm_timeout_seconds == 10
    assert s.ingest_max_concurrent == 4


def test_llm_timeout_cannot_exceed_product_cap() -> None:
    with pytest.raises(ValueError):
        Settings(LLM_TIMEOUT_SECONDS=11)


def test_ingest_concurrency_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(INGEST_MAX_CONCURRENT=0)


def test_llm_api_key_file_setting_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("file-key\nignored second line\n", encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("LLM_API_KEY", "plain-key")

    settings = Settings()

    assert settings.llm_api_key_file == key_file
    assert settings.llm_api_key == "plain-key"


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


# --- TRUSTED_PROXY_CIDRS / COOKIE_SECURE (S2) --------------------------------


def test_trusted_cidrs_parsed_when_set() -> None:
    s = Settings(trusted_proxy_cidrs=" 192.0.2.0/24 , 10.0.0.1 , 2001:db8::/32 ")
    assert [str(n) for n in s._trusted_nets] == ["192.0.2.0/24", "10.0.0.1/32", "2001:db8::/32"]


def test_trusted_cidrs_unset_and_blank_mean_no_trusted_peers() -> None:
    # Unset = raw empty string, blank = blank text: both parse to zero nets.
    assert Settings()._trusted_nets == []
    for raw in ("", "   ,  "):
        s = Settings(trusted_proxy_cidrs=raw)
        assert s.trusted_proxy_cidrs == raw
        assert s._trusted_nets == []


def test_trusted_cidrs_malformed_rejected_at_load() -> None:
    for bad in ("not-an-ip", "10.0.0.1/33", "1.2.3.4/banana"):
        with pytest.raises(Exception) as excinfo:
            Settings(trusted_proxy_cidrs=bad)
        assert "TRUSTED_PROXY_CIDRS" in str(excinfo.value)


def test_cookie_secure_unset_blank_and_explicit() -> None:
    assert Settings().cookie_secure is None
    # Blank (e.g. an empty .env line) must stay "unset", never quietly False.
    for raw in ("", "   "):
        assert Settings(cookie_secure=raw).cookie_secure is None
    assert Settings(cookie_secure=True).cookie_secure is True
    assert Settings(cookie_secure=False).cookie_secure is False


def test_cookie_secure_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKIE_SECURE", "not-a-bool")
    with pytest.raises(Exception) as excinfo:
        Settings()
    assert "COOKIE_SECURE" in str(excinfo.value)
