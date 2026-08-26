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
        # Read by install_auto_signin, and by nothing else. The server build
        # never sets it.
        "DESKTOP_MODE": True,
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


def migrate_database(app) -> str:
    """Bring the schema to head, whatever state it is in.

    Three cases, and getting the middle one wrong is what loses somebody's
    documents:

    * **Empty database.** ``upgrade`` creates everything and records the
      revision.
    * **Tables but no ``alembic_version``.** A database created by an earlier
      build that used ``create_all``. Running ``upgrade`` here fails trying to
      create tables that already exist; the schema is current, so it is stamped
      instead.
    * **Already stamped.** ``upgrade`` applies whatever is new, or does nothing.

    Returns a word describing which path was taken, for the log and the tests.
    """
    from alembic.migration import MigrationContext
    from flask_migrate import stamp, upgrade

    from .extensions import db

    with app.app_context():
        connection = db.session.connection()
        context = MigrationContext.configure(connection)
        stamped = context.get_current_revision()
        inspector = db.inspect(db.engine)
        has_tables = "scopes" in inspector.get_table_names()

    if stamped:
        with app.app_context():
            upgrade()
        return "upgraded"

    if has_tables:
        # Created by a build that used create_all. The schema matches head, so
        # record that rather than trying to build it again.
        with app.app_context():
            stamp()
        logger.info("Adopted an existing database that predates migrations")
        return "stamped"

    with app.app_context():
        upgrade()
    return "created"


def install_auto_signin(app) -> None:
    """Sign the local user in on every request. Desktop builds only.

    A single-user application on loopback has nobody to authenticate against:
    anyone who can reach the port is already sitting at the machine. A login
    screen there is a password to lose, not a control.

    Gated on ``DESKTOP_MODE``, which only ``desktop_overrides`` sets. If this
    ever ran on the server build it would sign every visitor in as somebody
    else, so the guard is checked at request time rather than trusted from
    however the app was constructed -- and there is a test asserting the server
    build does not get the hook.
    """
    if not app.config.get("DESKTOP_MODE"):
        return

    from flask import g
    from flask_login import current_user, login_user
    from sqlalchemy import select

    from .extensions import db
    from .models import User

    @app.before_request
    def _sign_in_the_local_user():
        if app.config.get("DESKTOP_MODE") is not True:  # pragma: no cover
            return None
        if current_user.is_authenticated:
            return None
        user = db.session.scalar(select(User).where(User.email == DESKTOP_EMAIL))
        if user is None:  # pragma: no cover - first run creates it
            return None
        login_user(user, remember=True)
        g._desktop_auto_signin = True
        return None


def build_app(directory: Path | None = None):
    """A Flask app configured for the desktop, schema migrated, account ready."""
    from . import create_app
    from .services.seeding import seed_library

    directory = directory or data_dir()
    app = create_app("development", **desktop_overrides(directory))

    # Alembic rather than create_all. A desktop user has no operator to run a
    # migration for them, and create_all silently does nothing to an existing
    # database -- so a schema change would ship as a crash, or worse, as a
    # missing column nobody notices until an export fails.
    outcome = migrate_database(app)
    logger.info("Database %s", outcome)

    with app.app_context():
        seed_library()

    ensure_local_account(app)
    install_auto_signin(app)
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
