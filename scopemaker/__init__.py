"""ScopeMaker -- construction scope of work exhibit generator.

Application factory.  Everything the app needs is wired here so that tests can
build an isolated instance with ``create_app("testing")``.
"""

from __future__ import annotations

import time
import uuid

from dotenv import load_dotenv
from flask import Flask, Response, abort, g, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import BaseConfig, get_config
from .errors import register_error_handlers
from .extensions import csrf, db, limiter, login_manager, migrate, oauth
from .logging_config import configure_logging

__version__ = "1.0.0"
__all__ = ["__version__", "create_app"]

load_dotenv()


def create_app(config_name: str | None = None, **overrides: object) -> Flask:
    """Build and configure a Flask application instance."""
    app = Flask(__name__, instance_relative_config=True)

    config_class: type[BaseConfig] = get_config(config_name)
    config_class.validate()
    app.config.from_object(config_class)
    app.config.update(overrides)

    configure_logging(app)
    _configure_proxy(app)
    _init_extensions(app)
    _register_blueprints(app)
    _register_request_hooks(app)
    _register_template_helpers(app)
    register_error_handlers(app)

    from . import cli

    cli.register_commands(app)

    app.logger.info(
        "ScopeMaker %s started (config=%s, procore=%s, oidc=%s)",
        __version__,
        config_class.__name__,
        app.config["PROCORE_ENABLED"],
        app.config["OIDC_ENABLED"],
    )
    return app


def _configure_proxy(app: Flask) -> None:
    """Trust X-Forwarded-* only as many hops as the operator declares.

    Trusting an unbounded number of proxies would let a client spoof its own
    source IP by injecting X-Forwarded-For, which would defeat rate limiting.
    """
    hops = int(app.config.get("TRUSTED_PROXY_COUNT", 0))
    if hops > 0:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=hops, x_proto=hops, x_host=hops, x_port=hops
        )


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db, directory="migrations")
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    oauth.init_app(app)

    # Models must be imported before create_all/migrations can see them.
    from . import models  # noqa: F401
    from .models.user import User

    @login_manager.user_loader
    def _load_user(user_id: str):
        return db.session.get(User, user_id)

    @login_manager.unauthorized_handler
    def _unauthorized():
        if request.path.startswith("/api/"):
            return {"error": {"code": "unauthorized", "message": "Authentication required."}}, 401
        from flask import flash, redirect, url_for

        flash(login_manager.login_message, login_manager.login_message_category)
        return redirect(url_for("auth.login", next=request.full_path))

    if app.config.get("OIDC_ENABLED"):
        _register_oidc(app)


def _register_oidc(app: Flask) -> None:
    """Register the OIDC provider with Authlib using OpenID discovery."""
    oauth.register(
        name=app.config["OIDC_NAME"],
        client_id=app.config["OIDC_CLIENT_ID"],
        client_secret=app.config["OIDC_CLIENT_SECRET"],
        server_metadata_url=app.config["OIDC_DISCOVERY_URL"],
        client_kwargs={"scope": app.config["OIDC_SCOPES"]},
    )


def _register_blueprints(app: Flask) -> None:
    from .blueprints.admin import bp as admin_bp
    from .blueprints.api import bp as api_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.exports import bp as exports_bp
    from .blueprints.library import bp as library_bp
    from .blueprints.main import bp as main_bp
    from .blueprints.procore import bp as procore_bp
    from .blueprints.projects import bp as projects_bp
    from .blueprints.scopes import bp as scopes_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(projects_bp, url_prefix="/projects")
    app.register_blueprint(scopes_bp, url_prefix="/scopes")
    app.register_blueprint(library_bp, url_prefix="/library")
    app.register_blueprint(exports_bp, url_prefix="/exports")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(procore_bp, url_prefix="/procore")
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    # The JSON API authenticates with bearer tokens, not cookies, so CSRF
    # (which protects cookie-authenticated state-changing requests) does not
    # apply and would otherwise reject every non-browser client.
    csrf.exempt(api_bp)


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def _assign_request_id() -> None:
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        g.request_started = time.perf_counter()

    @app.before_request
    def _validate_host() -> None:
        """Reject requests with an unexpected Host header.

        Flask builds absolute URLs (password reset links, OAuth redirects) from
        the Host header, so an attacker-controlled Host can poison them.
        """
        allowed = app.config.get("ALLOWED_HOSTS") or []
        if not allowed or app.debug or app.testing:
            return
        host = (request.host or "").split(":")[0].lower()
        if host not in {h.lower() for h in allowed}:
            app.logger.warning("Rejected request with unexpected Host header: %r", request.host)
            abort(400)

    @app.after_request
    def _security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
        )
        # Every asset is served from our own origin -- no CDNs -- so the policy
        # can stay tight. 'unsafe-inline' for style covers the small number of
        # dynamic width/visibility styles set by the editor.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        if app.config.get("FORCE_HTTPS"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if getattr(g, "request_id", None):
            response.headers["X-Request-ID"] = g.request_id
        return response

    @app.after_request
    def _access_log(response: Response) -> Response:
        started = getattr(g, "request_started", None)
        if started is not None and not request.path.startswith("/static/"):
            app.logger.info(
                "%s %s -> %s",
                request.method,
                request.path,
                response.status_code,
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": response.status_code,
                },
            )
        return response

    @app.teardown_appcontext
    def _remove_session(exception: BaseException | None) -> None:
        if exception is not None:
            db.session.rollback()
        db.session.remove()


def _register_template_helpers(app: Flask) -> None:
    import functools
    from pathlib import Path

    from .data.masterformat import DIVISIONS
    from .services.sanitize import sanitize_html

    css_path = Path(app.root_path) / "static" / "css" / "document.css"

    @functools.lru_cache(maxsize=1)
    def _read_document_css() -> str:
        try:
            return css_path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - packaging problem
            app.logger.error("document.css is missing at %s", css_path)
            return ""

    def document_css() -> str:
        """The paged-media stylesheet, inlined into standalone documents."""
        if app.debug:  # pick up edits without a restart
            _read_document_css.cache_clear()
        return _read_document_css()

    app.jinja_env.globals["document_css"] = document_css

    @app.context_processor
    def _inject_globals() -> dict:
        return {
            "app_name": app.config["APP_NAME"],
            "app_tagline": app.config["APP_TAGLINE"],
            "app_version": __version__,
            "docs_url": app.config["DOCS_URL"],
            "source_url": app.config["SOURCE_URL"],
            "procore_enabled": app.config["PROCORE_ENABLED"],
            "oidc_enabled": app.config["OIDC_ENABLED"],
            "oidc_display_name": app.config["OIDC_DISPLAY_NAME"],
            "divisions": DIVISIONS,
        }

    app.jinja_env.filters["sanitize"] = sanitize_html
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
