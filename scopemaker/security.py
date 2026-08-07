"""Password hashing, at-rest encryption, and authorization helpers."""

from __future__ import annotations

import functools
import hmac
import secrets
from collections.abc import Callable
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from flask import abort, current_app, g
from flask_login import current_user

# Argon2id with parameters comfortably above the OWASP 2024 floor
# (19 MiB memory, t=2, p=1) while staying fast enough for a web login.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time-ish verification that never raises on bad input."""
    if not stored_hash or not password:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than we now use."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


def password_problems(password: str) -> list[str]:
    """Return human-readable reasons a password is unacceptable.

    Length is the dominant factor in real-world resistance, so we require a
    genuinely long password instead of imposing character-class theatre.
    """
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("This password is too common.")
    if password and len(set(password)) < 5:
        problems.append("Must not repeat the same few characters.")
    return problems


_COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd123",
    "123456789012", "qwertyuiop123", "letmein12345", "administrator",
    "iloveyou1234", "welcome12345", "changeme1234", "scopemaker123",
}


# ---------------------------------------------------------------------------
# At-rest encryption for third-party OAuth tokens
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    key = current_app.config.get("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not configured; refusing to store third-party "
            "credentials in plaintext. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    cached = getattr(g, "_fernet", None)
    if cached is None:
        cached = Fernet(key.encode() if isinstance(key, str) else key)
        g._fernet = cached
    return cached


def encrypt_secret(plaintext: str | None) -> str | None:
    """Encrypt a token for database storage. ``None`` passes through."""
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str | None) -> str | None:
    """Decrypt a stored token. Returns ``None`` if it cannot be read.

    A rotated ``ENCRYPTION_KEY`` makes old ciphertext undecryptable; callers
    treat ``None`` as "this connection must be re-authorized" rather than
    crashing the request.
    """
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def generate_token(length: int = 32) -> str:
    """A URL-safe random token for invites, API keys and OAuth state."""
    return secrets.token_urlsafe(length)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# ---------------------------------------------------------------------------
# Authorization decorators
# ---------------------------------------------------------------------------

def require_role(*roles: str) -> Callable:
    """Require the current user to hold one of ``roles`` in their active org.

    Roles are hierarchical -- ``admin`` satisfies ``editor`` which satisfies
    ``viewer`` -- so callers name the *minimum* role a view needs.
    """

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not current_user.is_authenticated:
                abort(401)
            if not any(current_user.has_role(role) for role in roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view: Callable) -> Callable:
    """Shorthand for ``require_role("admin")``."""
    return require_role("admin")(view)


def editor_required(view: Callable) -> Callable:
    """Shorthand for ``require_role("editor")`` (admins included)."""
    return require_role("editor")(view)
