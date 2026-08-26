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
