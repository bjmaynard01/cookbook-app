"""Application configuration.

File-first secrets (spec D20): each secret reads the ``*_FILE`` env var first
(first line, trailing newline stripped), falling back to the plain env var.
The DB connection URL is assembled from DB_* parts — dev and prod (existing
MariaDB) share this exact code path; only .env values differ.

Trusted-proxy and cookie-secure settings (auth plan C4/C3):
``TRUSTED_PROXY_CIDRS`` is the comma-separated list of networks whose
*direct TCP peers* may supply X-Forwarded-For/X-Forwarded-Proto; it is
validated and parsed once at settings load so a malformed non-empty value
fails at startup instead of being silently treated as "trust all".
``COOKIE_SECURE`` is an explicit true/false override; unset (or blank)
means "follow the effective request scheme".
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


def read_password_file(path: Path | str) -> str:
    """Read a password from ``path``: UTF-8, first line only.

    Strips only line endings so passwords containing spaces survive; an
    empty file yields "".  Unreadable files raise a RuntimeError with a
    fixed message (no file contents) so failures are loud but leak nothing.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers non-UTF-8 (binary) files as "unreadable".
        raise RuntimeError(f"Password file could not be read: {path}") from None
    return text.splitlines()[0] if text else ""


def _from_secret_file(name: str) -> str | None:
    """Read a secret from a file if ``<name>_FILE`` points at one."""
    path_env = f"{name}_FILE"
    raw = os.environ.get(path_env)
    if raw:
        try:
            return Path(raw).read_text(encoding="utf-8").strip().splitlines()[0]
        except OSError:
            # A configured but unreadable secret must fail loudly, never fall
            # back silently (spec D20/D24).
            raise RuntimeError(f"{path_env} points at an unreadable file: {raw}")
    return os.environ.get(name)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- app ---
    app_name: str = Field(default="cookbook", alias="APP_NAME")
    debug: bool = Field(default=False, alias="DEBUG")
    secret_key: str | None = Field(default=None, alias="SECRET_KEY")
    secret_key_file: Path | None = Field(default=None, alias="SECRET_KEY_FILE")
    allowed_hosts: list[str] = Field(default_factory=list, alias="ALLOWED_HOSTS")
    session_ttl_hours: int = Field(default=12, alias="SESSION_TTL_HOURS")

    # --- database ---
    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=3306, alias="DB_PORT")
    db_name: str = Field(default="cookbook", alias="DB_NAME")
    db_user: str = Field(default="cookbook", alias="DB_USER")
    db_password: str | None = Field(default=None, alias="DB_PASSWORD", repr=False)
    db_password_file: Path | None = Field(default=None, alias="DB_PASSWORD_FILE")

    # --- bootstrap owner account (CLI) ---
    first_admin_password: str | None = Field(default=None, alias="FIRST_ADMIN_PASSWORD", repr=False)
    first_admin_password_file: Path | None = Field(default=None, alias="FIRST_ADMIN_PASSWORD_FILE")

    # --- media ---
    media_dir: Path = Field(default=Path("/media"), alias="MEDIA_DIR")
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, alias="UPLOAD_MAX_BYTES")

    # --- trusted proxy / cookie security (auth S2) ---
    trusted_proxy_cidrs: str = Field(default="", alias="TRUSTED_PROXY_CIDRS")
    cookie_secure: bool | None = Field(default=None, alias="COOKIE_SECURE")

    @model_validator(mode="before")
    @classmethod
    def _normalize_cookie_secure(cls, data: object) -> object:
        # A blank COOKIE_SECURE (an empty .env line) means "unset": follow the
        # effective request scheme. pydantic's own bool parsing rejects blank
        # strings, so normalize to None instead of failing the whole load.
        if isinstance(data, dict):
            for key in ("COOKIE_SECURE", "cookie_secure"):
                value = data.get(key)
                if isinstance(value, str) and not value.strip():
                    data[key] = None
        return data

    @model_validator(mode="after")
    def _parse_trusted_cidrs(self) -> Settings:
        # Parse once, at boot: a malformed non-empty value raises here so the
        # app fails startup loudly (never silently "trust all"). Unset/blank/
        # all-blank values are a valid "no trusted peers" configuration.
        raw = self.trusted_proxy_cidrs.strip()
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        if raw:
            for piece in raw.split(","):
                piece = piece.strip()
                if piece:
                    try:
                        # strict=False: "10.0.0.1" (no /prefix) is accepted as
                        # a /32; host bits are normalized away.
                        networks.append(ipaddress.ip_network(piece, strict=False))
                    except ValueError:
                        raise ValueError(
                            f"TRUSTED_PROXY_CIDRS: {piece!r} is not a valid IPv4/IPv6 CIDR"
                        ) from None
        self._trusted_nets = networks
        return self

    @property
    def database_url(self) -> str:
        # URL.create percent-encodes password characters (@ / : ? #, ...) so
        # passwords with special characters round-trip safely through make_url.
        password = self.db_password or ""
        built = URL.create(
            drivername="mysql+asyncmy",
            username=self.db_user,
            password=password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )
        return built.render_as_string(hide_password=False)


class CLISettings(BaseSettings):
    """Lean CLI configuration: never references SECRET_KEY (spec D24).

    DB_PASSWORD_FILE is resolved file-first over DB_PASSWORD; the bootstrap
    password file path is NOT read here (stays lazy) so an explicit CLI
    ``--password-file`` can override even an unreadable fallback file.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    debug: bool = Field(default=False, alias="DEBUG")
    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=3306, alias="DB_PORT")
    db_name: str = Field(default="cookbook", alias="DB_NAME")
    db_user: str = Field(default="cookbook", alias="DB_USER")
    db_password: str | None = Field(default=None, alias="DB_PASSWORD", repr=False)
    db_password_file: Path | None = Field(default=None, alias="DB_PASSWORD_FILE")
    media_dir: Path = Field(default=Path("/media"), alias="MEDIA_DIR")
    first_admin_password: str | None = Field(default=None, alias="FIRST_ADMIN_PASSWORD", repr=False)
    first_admin_password_file: Path | None = Field(default=None, alias="FIRST_ADMIN_PASSWORD_FILE")

    @property
    def database_url(self) -> str:
        built = URL.create(
            drivername="mysql+asyncmy",
            username=self.db_user,
            password=self.db_password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )
        return built.render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # File-first resolution (spec D20): *_FILE wins over the plain value.
    if s.secret_key_file is not None:
        s.secret_key = read_password_file(s.secret_key_file)
    if s.db_password_file is not None:
        s.db_password = read_password_file(s.db_password_file)
    # Bootstrap owner password file-first (CLI subtask 7.7).
    if s.first_admin_password_file is not None:
        s.first_admin_password = read_password_file(s.first_admin_password_file)
    if not s.secret_key:
        raise RuntimeError(
            "SECRET_KEY (or SECRET_KEY_FILE) is not set — refusing to start "
            "with a default session key (spec D20)."
        )
    return s


def cli_config() -> CLISettings:
    """Build a lean CLI config; no SECRET_KEY required or read (spec D24).

    DB_PASSWORD_FILE is resolved eagerly (file-first over DB_PASSWORD); the
    bootstrap password file path is kept LAZY (not read here) so an explicit
    CLI ``--password-file`` can override even an unreadable fallback file.
    """
    s = CLISettings()
    if s.db_password_file is not None:
        s.db_password = read_password_file(s.db_password_file)
    return s
