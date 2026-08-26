"""Running ScopeMaker as a local desktop application.

Phase 1 of ``docs/desktop-app-scope.md``. The native shell (``src-tauri``) is a
thin thing: it starts this, waits for a port, and points a webview at it. All
the decisions that can be got wrong live here, in Python, where they can be
tested.

The four that matter:

**Loopback only.** The server binds ``127.0.0.1``. A desktop app that listens on
``0.0.0.0`` has quietly published one user's contract documents to their hotel
wifi.

**An ephemeral port.** Asking the OS for port 0 and reading back what it gave
avoids both a collision with whatever else the user runs and a predictable
target for anything else on the machine. The port is handed to the shell on
stdout rather than fixed by agreement.

**A persistent secret key.** DevelopmentConfig generates one per process, which
would sign every user out each time they closed the window. It is generated once
into the user profile directory and read back after that, with 0600 permissions
where the platform honours them.

**One local account, no login screen.** A single-user desktop app has nobody to
authenticate against. The account exists because the data model has an owner for
every row, not because anyone is being kept out.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "ScopeMaker"
DESKTOP_EMAIL = "you@localhost"
DESKTOP_ORG = "My Company"


def data_dir() -> Path:
    """Where a desktop install keeps its database and key.

    ``%APPDATA%`` on Windows, the XDG data directory elsewhere. Overridable with
    SCOPEMAKER_DATA_DIR, which is what the tests use.
    """
    override = os.environ.get("SCOPEMAKER_DATA_DIR")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        path = Path(base) / "scopemaker"

    path.mkdir(parents=True, exist_ok=True)
    return path


def secret_key(directory: Path | None = None) -> str:
    """Read the signing key, generating it on first run.

    Regenerating this per launch would invalidate the session cookie every time
    the window closed. It is not a password and never leaves the machine, but it
    is still a secret: the file is created 0600 where the platform enforces it.
    """
    directory = directory or data_dir()
    key_file = directory / "secret.key"

    if key_file.exists():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover - filesystems that do not support it
        logger.debug("Could not restrict permissions on %s", key_file)
    return generated


def free_port() -> int:
    """Ask the OS for a port nobody is using.

    Bound and released rather than guessed. There is a race between this and the
    server binding it, which is unavoidable without passing the socket itself;
    on a single-user desktop the window is microseconds and the failure mode is
    a clear bind error at startup rather than anything silent.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def desktop_overrides(directory: Path | None = None) -> dict[str, object]:
    """Configuration a local single-user install needs, and why.

    Returned rather than applied so a test can assert on it without starting a
    server.
    """
    directory = directory or data_dir()
    database = directory / "scopemaker.sqlite3"

    return {
        "SECRET_KEY": secret_key(directory),
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
        # Nothing is reachable from another machine, and a lockout with no
        # administrator to appeal to would be a support call with no remedy.
        "RATELIMIT_ENABLED": False,
        # Async rendering needs a worker process. A desktop app has one process.
        "RENDER_ASYNC": False,
        # No relay is configured and no invitations are sent; the console
        # backend keeps password-reset code paths from failing.
        "MAIL_BACKEND": "console",
        # Registration is meaningless with one local account.
        "REGISTRATION_MODE": "closed",
        # There is nobody to scrape /metrics.
        "METRICS_TOKEN": "",
        # Served over http on loopback, so a Secure cookie would never be sent.
        "SESSION_COOKIE_SECURE": False,
        "REMEMBER_COOKIE_SECURE": False,
        "PREFERRED_URL_SCHEME": "http",
    }


def ensure_local_account(app) -> tuple[str, str]:
    """Create the single local account on first run. Returns (email, org slug).

    Idempotent: a second launch finds the existing account rather than making
    another.
    """
    from sqlalchemy import select

    from .extensions import db
    from .models import Membership, User
    from .services.accounts import create_organization, create_user

    with app.app_context():
        user = db.session.scalar(
            select(User).where(User.email == DESKTOP_EMAIL)
        )
        if user is not None:
            membership = user.memberships[0] if user.memberships else None
            slug = membership.organization.slug if membership else ""
            return user.email, slug

        organization = create_organization(DESKTOP_ORG)
        # No password: nothing authenticates against it. The account exists
        # because every row needs an owner, not to keep anyone out.
        user = create_user(email=DESKTOP_EMAIL, full_name="Local user")
        db.session.add(
            Membership(
                organization_id=organization.id, user_id=user.id, role="admin"
            )
        )
        db.session.commit()
        logger.info("Created the local account %s", DESKTOP_EMAIL)
        return user.email, organization.slug


def build_app(directory: Path | None = None):
    """A Flask app configured for the desktop, schema migrated, account ready."""
    from . import create_app
    from .extensions import db
    from .services.seeding import seed_library

    directory = directory or data_dir()
    app = create_app("development", **desktop_overrides(directory))

    with app.app_context():
        # create_all rather than Alembic: a desktop install has no operator to
        # run a migration, and the schema is whatever this version ships.
        # Phase 2 should replace this with `flask db upgrade` on launch so an
        # upgrade over an existing database keeps the user's documents.
        db.create_all()
        seed_library()

    ensure_local_account(app)
    return app


def main() -> int:
    """Entry point the native shell spawns.

    Prints the chosen port on stdout as ``SCOPEMAKER_PORT=<n>`` before serving,
    so the shell can wait for a line rather than poll a guessed port.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    port = int(os.environ.get("SCOPEMAKER_PORT") or free_port())
    app = build_app()

    print(f"SCOPEMAKER_PORT={port}", flush=True)
    logger.info("ScopeMaker desktop on http://127.0.0.1:%s", port)

    from werkzeug.serving import run_simple

    run_simple(
        "127.0.0.1",  # never 0.0.0.0
        port,
        app,
        use_reloader=False,
        threaded=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
