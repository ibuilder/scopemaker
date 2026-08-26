"""Running as a desktop application.

The native shell is thin on purpose: it starts a process, waits for a port and
opens a webview. Everything that can be got wrong is here, so this is where it
gets tested.

The one that matters most is the bind address. A desktop app listening on
0.0.0.0 has published one user's contract documents to whatever network they
happen to be on, and nothing about the running application would look wrong.
"""

from __future__ import annotations

import socket

import pytest

from scopemaker import desktop


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An isolated data directory, so tests never touch a real profile."""
    monkeypatch.setenv("SCOPEMAKER_DATA_DIR", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def test_the_data_directory_is_created(home):
    path = desktop.data_dir()
    assert path == home
    assert path.is_dir()


def test_the_data_directory_defaults_into_the_user_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("SCOPEMAKER_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("sys.platform", "win32")

    assert desktop.data_dir() == tmp_path / desktop.APP_NAME


# ---------------------------------------------------------------------------
# The signing key
# ---------------------------------------------------------------------------

def test_the_secret_key_survives_a_restart(home):
    """Regenerating it would sign the user out every time they closed the
    window -- which is what DevelopmentConfig does, and why it cannot be used
    as-is."""
    first = desktop.secret_key(home)
    second = desktop.secret_key(home)

    assert first == second
    assert len(first) >= 40
    assert (home / "secret.key").read_text(encoding="utf-8").strip() == first


def test_a_blank_key_file_is_regenerated(home):
    """An interrupted first run can leave an empty file. Reading it back as the
    key would give every such install the same empty secret."""
    (home / "secret.key").write_text("   \n", encoding="utf-8")

    key = desktop.secret_key(home)

    assert key.strip()
    assert len(key) >= 40


def test_two_installs_do_not_share_a_key(home, tmp_path):
    other = tmp_path / "second"
    other.mkdir()
    assert desktop.secret_key(home) != desktop.secret_key(other)


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------

def test_the_port_is_free_and_usable(home):
    port = desktop.free_port()

    assert 1024 < port < 65536
    # Nothing should be holding it -- free_port released it before returning.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_ports_are_not_reused_back_to_back(home):
    ports = {desktop.free_port() for _ in range(5)}
    assert len(ports) > 1, "the OS handed out one port five times"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_the_database_lives_in_the_data_directory(home):
    overrides = desktop.desktop_overrides(home)

    uri = overrides["SQLALCHEMY_DATABASE_URI"]
    assert uri.startswith("sqlite:///")
    assert "scopemaker.sqlite3" in uri
    assert home.as_posix() in uri


def test_server_only_features_are_off(home):
    """Each of these would misbehave on a single-process desktop install."""
    overrides = desktop.desktop_overrides(home)

    # Needs a worker process; a desktop app has one process, so exports would
    # queue and never complete.
    assert overrides["RENDER_ASYNC"] is False
    # A lockout with no administrator to appeal to is a dead end.
    assert overrides["RATELIMIT_ENABLED"] is False
    # No relay is configured; the console backend keeps reset paths alive.
    assert overrides["MAIL_BACKEND"] == "console"
    assert overrides["REGISTRATION_MODE"] == "closed"
    assert overrides["METRICS_TOKEN"] == ""


def test_cookies_are_not_marked_secure(home):
    """Served over http on loopback. A Secure cookie would never be sent, and
    the user would appear to be signed out immediately."""
    overrides = desktop.desktop_overrides(home)

    assert overrides["SESSION_COOKIE_SECURE"] is False
    assert overrides["REMEMBER_COOKIE_SECURE"] is False


# ---------------------------------------------------------------------------
# The local account
# ---------------------------------------------------------------------------

def test_first_run_creates_one_account_and_one_organization(home):
    app = desktop.build_app(home)

    from sqlalchemy import func, select

    from scopemaker.extensions import db
    from scopemaker.models import Organization, User

    with app.app_context():
        assert db.session.scalar(select(func.count(User.id))) == 1
        assert db.session.scalar(select(func.count(Organization.id))) == 1

        user = db.session.scalar(select(User))
        assert user.email == desktop.DESKTOP_EMAIL
        assert [m.role for m in user.memberships] == ["admin"]


def test_a_second_launch_does_not_create_a_second_account(home):
    desktop.build_app(home)
    app = desktop.build_app(home)

    from sqlalchemy import func, select

    from scopemaker.extensions import db
    from scopemaker.models import Organization, User

    with app.app_context():
        assert db.session.scalar(select(func.count(User.id))) == 1
        assert db.session.scalar(select(func.count(Organization.id))) == 1


def test_the_local_account_has_no_password(home):
    """Nothing authenticates against it. Storing a password would imply a
    secret worth stealing that protects nothing."""
    app = desktop.build_app(home)

    from sqlalchemy import select

    from scopemaker.extensions import db
    from scopemaker.models import User

    with app.app_context():
        user = db.session.scalar(select(User))
        assert user.password_hash is None


def test_the_library_is_seeded_on_first_run(home):
    """A desktop install with no clauses cannot generate anything."""
    app = desktop.build_app(home)

    from sqlalchemy import func, select

    from scopemaker.extensions import db
    from scopemaker.models import Clause

    with app.app_context():
        assert db.session.scalar(select(func.count(Clause.id))) > 200


def test_the_database_file_is_created_where_expected(home):
    desktop.build_app(home)
    assert (home / "scopemaker.sqlite3").exists()


# ---------------------------------------------------------------------------
# The bind address
# ---------------------------------------------------------------------------

def test_the_server_binds_loopback_only(home, monkeypatch):
    """The property this whole module exists to get right.

    Binding 0.0.0.0 would publish the user's contract documents to whatever
    network they are on, and nothing about the running application would look
    different.
    """
    captured = {}

    def fake_run_simple(host, port, application, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["reloader"] = kwargs.get("use_reloader")

    monkeypatch.setattr("werkzeug.serving.run_simple", fake_run_simple)
    monkeypatch.setenv("SCOPEMAKER_PORT", "0")

    desktop.main()

    assert captured["host"] == "127.0.0.1", "the desktop server left loopback"
    assert captured["reloader"] is False, "the reloader would fork a second server"


def test_the_port_is_announced_on_stdout(home, monkeypatch, capsys):
    """The shell waits for this line rather than polling a guessed port."""
    monkeypatch.setattr("werkzeug.serving.run_simple", lambda *a, **k: None)
    monkeypatch.setenv("SCOPEMAKER_PORT", "51234")

    desktop.main()

    assert "SCOPEMAKER_PORT=51234" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Schema migration on launch
# ---------------------------------------------------------------------------

def current_revision(app) -> str | None:
    from alembic.migration import MigrationContext

    from scopemaker.extensions import db

    with app.app_context():
        return MigrationContext.configure(
            db.session.connection()
        ).get_current_revision()


def test_a_fresh_install_runs_the_migrations(home):
    """Not create_all: the database has to know which revision it is on, or the
    next release cannot migrate it."""
    app = desktop.build_app(home)

    assert current_revision(app), "the database was created without a revision"


def test_a_second_launch_is_a_no_op(home):
    desktop.build_app(home)
    first = current_revision(desktop.build_app(home))

    app = desktop.build_app(home)

    assert current_revision(app) == first


def test_a_database_predating_migrations_is_adopted_not_rebuilt(home):
    """The case that loses documents if it is got wrong.

    Phase 1 shipped create_all, so early installs have every table and no
    alembic_version. Running `upgrade` against that fails trying to create
    tables that already exist. It has to be stamped instead -- and the user's
    rows have to survive.
    """
    from sqlalchemy import select

    from scopemaker import create_app
    from scopemaker.extensions import db
    from scopemaker.models import Project

    # Build a database the old way, with something in it worth keeping.
    legacy = create_app("development", **desktop.desktop_overrides(home))
    with legacy.app_context():
        db.create_all()
        # A real organization: foreign keys are enforced on SQLite, so an
        # invented id would fail here rather than in the code under test.
        from scopemaker.services.accounts import create_organization

        organization = create_organization("Existing Co")
        db.session.flush()
        db.session.add(
            Project(
                organization_id=organization.id,
                name="Existing project",
                number="KEEP-1",
            )
        )
        db.session.commit()

    assert current_revision(legacy) is None, "fixture should have no revision"

    outcome = desktop.migrate_database(legacy)

    assert outcome == "stamped"
    assert current_revision(legacy), "adopting the database left it unstamped"

    with legacy.app_context():
        kept = db.session.scalar(select(Project).where(Project.number == "KEEP-1"))
        assert kept is not None, "adopting the database destroyed the user's data"
        assert kept.name == "Existing project"


def test_the_reported_outcome_matches_what_happened(home):
    assert desktop.migrate_database(
        __import__("scopemaker").create_app(
            "development", **desktop.desktop_overrides(home)
        )
    ) == "created"


# ---------------------------------------------------------------------------
# No login screen
# ---------------------------------------------------------------------------

def test_the_user_is_signed_in_without_a_login_screen(home):
    """A single-user app on loopback has nobody to authenticate against."""
    app = desktop.build_app(home)
    client = app.test_client()

    # 200 rather than a redirect to /auth/login is the property; the dashboard
    # renders initials, not the address, so the email is checked where it is
    # actually shown.
    assert client.get("/dashboard").status_code == 200, (
        "the desktop build showed a login screen"
    )
    profile = client.get("/auth/profile").get_data(as_text=True)
    assert desktop.DESKTOP_EMAIL in profile


def test_pages_that_need_a_role_are_reachable(home):
    """The local account is an admin, so nothing should be gated off."""
    app = desktop.build_app(home)
    client = app.test_client()

    for path in ("/scopes/", "/projects/", "/library/", "/admin/"):
        assert client.get(path).status_code == 200, f"{path} was not reachable"


def test_the_server_build_never_gets_auto_sign_in(app, db):
    """The guard that matters.

    If this hook were installed on the server build it would sign every
    anonymous visitor in as somebody else. DESKTOP_MODE is set in exactly one
    place and the server build never sets it.

    The account the hook looks for has to exist for this to test anything. An
    earlier version used a fresh in-memory database with no such user, so the
    hook returned early for want of a row and the test passed with the gate
    deleted -- proving nothing at all.
    """
    from scopemaker.models import Membership
    from scopemaker.services.accounts import create_organization, create_user

    organization = create_organization("Server Co")
    local = create_user(email=desktop.DESKTOP_EMAIL, full_name="Local")
    db.session.add(
        Membership(
            organization_id=organization.id, user_id=local.id, role="admin"
        )
    )
    db.session.commit()

    assert app.config.get("DESKTOP_MODE") is not True

    before = len(app.before_request_funcs.get(None, []))
    desktop.install_auto_signin(app)  # must be a no-op
    after = len(app.before_request_funcs.get(None, []))

    assert after == before, "the server build was given the auto sign-in hook"

    response = app.test_client().get("/dashboard")
    assert response.status_code in (302, 401), (
        "the server build signed in an anonymous visitor"
    )


def test_auto_sign_in_leaves_an_existing_session_alone(home):
    """It fills an empty session; it must not overwrite one that exists.

    Tested by invoking the hook directly rather than through a request. Two
    routes around it were tried and both measure something else: /auth/login
    redirects an already-authenticated visitor, so on a desktop build the form
    never processes; and a hand-seeded session is wiped by Flask-Login's
    "strong" session protection before the hook ever runs.
    """
    from flask_login import current_user, login_user

    from scopemaker.extensions import db
    from scopemaker.models import User
    from scopemaker.services.accounts import create_user

    app = desktop.build_app(home)

    with app.app_context():
        other = create_user(email="someone.else@example.com", full_name="Other")
        db.session.commit()
        other_id = other.id

    hook = next(
        f for f in app.before_request_funcs[None]
        if f.__name__ == "_sign_in_the_local_user"
    )

    with app.test_request_context("/dashboard"):
        login_user(db.session.get(User, other_id))
        assert current_user.email == "someone.else@example.com"

        hook()

        assert current_user.email == "someone.else@example.com", (
            "the hook replaced a session that already had a user"
        )


def test_auto_sign_in_fills_an_empty_session(home):
    """The other half: with nobody signed in, the hook signs in the local user."""
    from flask_login import current_user

    app = desktop.build_app(home)
    hook = next(
        f for f in app.before_request_funcs[None]
        if f.__name__ == "_sign_in_the_local_user"
    )

    with app.test_request_context("/dashboard"):
        assert not current_user.is_authenticated

        hook()

        assert current_user.is_authenticated
        assert current_user.email == desktop.DESKTOP_EMAIL
