"""Application configuration.

Configuration is read from the environment so the same image can be promoted
across environments.  ``ProductionConfig`` refuses to start when a secret is
missing rather than silently falling back to a development default -- a
misconfigured production deploy should fail loudly at boot, not leak sessions.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env has to be loaded *here*, before the config classes below are defined,
# because their attributes are evaluated at class-definition time -- that is,
# at import. Loading it in the application factory would be too late: by the
# time create_app() runs, this module has already been imported and every
# setting has already been read from a bare environment.
#
# The `flask` CLI happens to load dotenv itself, which masks this; anything
# else (gunicorn, a maintenance script, a cron job) would silently fall back to
# the default SQLite path and present as a mysteriously empty database.
#
# The path is explicit because a bare load_dotenv() searches upward from the
# *calling* file, so it misses .env whenever the entry point lives elsewhere.
# override=False keeps a real environment variable winning over the file,
# which is what you want in a container.
load_dotenv(BASE_DIR / ".env", override=False)
load_dotenv(override=False)  # conventional cwd-relative fallback


class ConfigError(RuntimeError):
    """Raised when the environment is missing something required to boot."""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - operator error
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _normalise_db_url(url: str) -> str:
    """Accept the ``postgres://`` scheme that many PaaS providers still emit."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class BaseConfig:
    """Settings shared by every environment."""

    # -- Core ---------------------------------------------------------------
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
    ENCRYPTION_KEY: str = os.environ.get("ENCRYPTION_KEY", "")
    ALLOWED_HOSTS: list[str] = _csv("ALLOWED_HOSTS", "localhost,127.0.0.1")
    TRUSTED_PROXY_COUNT: int = _int("TRUSTED_PROXY_COUNT", 0)
    FORCE_HTTPS: bool = _bool("FORCE_HTTPS", False)

    # -- Database -----------------------------------------------------------
    SQLALCHEMY_DATABASE_URI: str = _normalise_db_url(
        os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'scopemaker.sqlite3'}")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    # -- Sessions & CSRF ----------------------------------------------------
    SESSION_COOKIE_NAME = "scopemaker_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = FORCE_HTTPS
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 hours
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = FORCE_HTTPS
    REMEMBER_COOKIE_SAMESITE = "Lax"
    WTF_CSRF_TIME_LIMIT = None  # tie CSRF lifetime to the session, not a timer

    # -- Uploads ------------------------------------------------------------
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB

    # -- Rate limiting ------------------------------------------------------
    RATELIMIT_STORAGE_URI: str = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT: str = os.environ.get("RATELIMIT_DEFAULT", "600 per hour")
    RATELIMIT_ENABLED: bool = _bool("RATELIMIT_ENABLED", True)
    RATELIMIT_HEADERS_ENABLED = True

    # -- Registration policy ------------------------------------------------
    REGISTRATION_MODE: str = os.environ.get("REGISTRATION_MODE", "open").strip().lower()

    # -- Procore ------------------------------------------------------------
    PROCORE_ENABLED: bool = _bool("PROCORE_ENABLED", False)
    PROCORE_CLIENT_ID: str = os.environ.get("PROCORE_CLIENT_ID", "")
    PROCORE_CLIENT_SECRET: str = os.environ.get("PROCORE_CLIENT_SECRET", "")
    PROCORE_API_BASE: str = os.environ.get("PROCORE_API_BASE", "https://api.procore.com").rstrip("/")
    PROCORE_LOGIN_BASE: str = os.environ.get(
        "PROCORE_LOGIN_BASE", "https://login.procore.com"
    ).rstrip("/")
    PROCORE_REDIRECT_URI: str = os.environ.get(
        "PROCORE_REDIRECT_URI", "http://localhost:5000/procore/callback"
    )
    PROCORE_DMSA_COMPANY_ID: str = os.environ.get("PROCORE_DMSA_COMPANY_ID", "")
    PROCORE_TIMEOUT: int = _int("PROCORE_TIMEOUT", 30)

    # -- OIDC SSO -----------------------------------------------------------
    OIDC_ENABLED: bool = _bool("OIDC_ENABLED", False)
    OIDC_NAME: str = os.environ.get("OIDC_NAME", "sso")
    OIDC_DISPLAY_NAME: str = os.environ.get("OIDC_DISPLAY_NAME", "Single Sign-On")
    OIDC_CLIENT_ID: str = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET: str = os.environ.get("OIDC_CLIENT_SECRET", "")
    OIDC_DISCOVERY_URL: str = os.environ.get("OIDC_DISCOVERY_URL", "")
    OIDC_SCOPES: str = os.environ.get("OIDC_SCOPES", "openid email profile")
    OIDC_ALLOWED_DOMAINS: list[str] = _csv("OIDC_ALLOWED_DOMAINS")
    OIDC_DEFAULT_ORG: str = os.environ.get("OIDC_DEFAULT_ORG", "")

    # -- Logging ------------------------------------------------------------
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
    LOG_FORMAT: str = os.environ.get("LOG_FORMAT", "text").lower()

    # -- Product metadata ---------------------------------------------------
    APP_NAME = "ScopeMaker"
    APP_TAGLINE = "Construction scope of work exhibits, generated properly."
    DOCS_URL = "https://ibuilder.github.io/procore-exhibit-generator/"
    SOURCE_URL = "https://github.com/ibuilder/procore-exhibit-generator"

    TESTING = False
    DEBUG = False

    @classmethod
    def validate(cls) -> None:
        """Hook for subclasses to assert on required settings."""
        if cls.REGISTRATION_MODE not in {"open", "invite", "closed"}:
            raise ConfigError(
                "REGISTRATION_MODE must be one of open|invite|closed, got "
                f"{cls.REGISTRATION_MODE!r}"
            )


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # A stable-per-process ephemeral key: convenient locally, useless to an
    # attacker, and guaranteed to be replaced in production by validate().
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
    TEMPLATES_AUTO_RELOAD = True
    SQLALCHEMY_ECHO = _bool("SQLALCHEMY_ECHO", False)


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = False
    SECRET_KEY = "testing-secret-key-not-for-real-use"
    # Fixed Fernet key so encrypted fixtures stay decryptable across runs.
    ENCRYPTION_KEY = "PHMYRQ3sBQGmWfQFOWfLWCkV1s0MnBGKcpS4iFctuNM="
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    REGISTRATION_MODE = "open"
    PROCORE_ENABLED = True
    PROCORE_CLIENT_ID = "test-client-id"
    PROCORE_CLIENT_SECRET = "test-client-secret"


class ProductionConfig(BaseConfig):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"

    @classmethod
    def validate(cls) -> None:
        super().validate()
        missing = [
            name
            for name in ("SECRET_KEY", "ENCRYPTION_KEY")
            if not getattr(cls, name)
        ]
        if missing:
            raise ConfigError(
                "Refusing to start in production without: "
                + ", ".join(missing)
                + ". See .env.example for how to generate them."
            )
        if cls.SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
            raise ConfigError(
                "SQLite is not supported in production. Set DATABASE_URL to a "
                "PostgreSQL connection string."
            )
        if cls.PROCORE_ENABLED and not (cls.PROCORE_CLIENT_ID and cls.PROCORE_CLIENT_SECRET):
            raise ConfigError(
                "PROCORE_ENABLED=1 requires PROCORE_CLIENT_ID and PROCORE_CLIENT_SECRET."
            )
        if cls.OIDC_ENABLED and not (cls.OIDC_CLIENT_ID and cls.OIDC_DISCOVERY_URL):
            raise ConfigError(
                "OIDC_ENABLED=1 requires OIDC_CLIENT_ID and OIDC_DISCOVERY_URL."
            )


CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(name: str | None = None) -> type[BaseConfig]:
    """Resolve a config class by name, defaulting to ``FLASK_ENV``."""
    key = (name or os.environ.get("FLASK_ENV") or "development").strip().lower()
    try:
        return CONFIGS[key]
    except KeyError as exc:
        raise ConfigError(
            f"Unknown config {key!r}. Expected one of: {', '.join(CONFIGS)}"
        ) from exc
