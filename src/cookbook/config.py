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
    allowed_hosts: list[str] = Field(default_factory=list, alias="ALLOWED_HOSTS")
    session_ttl_hours: int = Field(default=12, alias="SESSION_TTL_HOURS")

    # --- database ---
    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=3306, alias="DB_PORT")
    db_name: str = Field(default="cookbook", alias="DB_NAME")
    db_user: str = Field(default="cookbook", alias="DB_USER")
    db_password: str | None = Field(default=None, alias="DB_PASSWORD")

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
        password = self.db_password or ""
        auth = f"{self.db_user}:{password}@" if password else f"{self.db_user}@"
        return f"mysql+asyncmy://{auth}{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # File-first resolution (spec D20): *_FILE wins over the plain value.
    file_secret = _from_secret_file("SECRET_KEY")
    if file_secret is not None:
        s.secret_key = file_secret
    file_pw = _from_secret_file("DB_PASSWORD")
    if file_pw is not None:
        s.db_password = file_pw
    if not s.secret_key:
        raise RuntimeError(
            "SECRET_KEY (or SECRET_KEY_FILE) is not set — refusing to start "
            "with a default session key (spec D20)."
        )
    return s
