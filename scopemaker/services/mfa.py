"""Time-based one-time passwords and recovery codes.

The QR code is rendered as an **inline SVG**, not fetched from an image host or
a third-party chart API. That keeps the `default-src 'self'` policy intact and,
more importantly, means the shared secret is never sent to anyone else — a
surprising number of tutorials hand it to a Google chart URL.

Recovery codes are stored the same way passwords are: Argon2 hashes, verified
by trying each one. A leaked database yields no usable code.
"""

from __future__ import annotations

import io
import logging
import secrets

import pyotp
import segno
from flask import current_app

from ..security import hash_password, verify_password

logger = logging.getLogger(__name__)

#: Accept a code from the adjacent window as well as the current one, so a
#: slightly-wrong device clock does not lock somebody out. One step either side
#: is 30 seconds; more than that meaningfully widens the guessing window.
VALID_WINDOW = 1

RECOVERY_CODE_COUNT = 10
#: 10 chars of Crockford-ish base32 ~= 50 bits. Ambiguous characters are left
#: out because people read these off paper.
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_CODE_LENGTH = 10


def new_secret() -> str:
    """A fresh base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str, issuer: str | None = None) -> str:
    issuer = issuer or current_app.config.get("APP_NAME", "ScopeMaker")
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_svg(secret: str, *, email: str, issuer: str | None = None, scale: int = 4) -> str:
    """The enrolment QR as an inline SVG string."""
    uri = provisioning_uri(secret, email=email, issuer=issuer)
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(
        buffer,
        kind="svg",
        xmldecl=False,
        svgns=True,
        scale=scale,
        dark="#0b1524",
        light=None,          # transparent, so it works in either theme
        omitsize=False,
    )
    return buffer.getvalue().decode("utf-8")


def verify_code(secret: str, code: str) -> bool:
    """Check a six-digit TOTP, tolerating one step of clock drift."""
    if not secret or not code:
        return False
    cleaned = "".join(ch for ch in str(code) if ch.isdigit())
    if len(cleaned) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(cleaned, valid_window=VALID_WINDOW)
    except Exception:
        logger.exception("TOTP verification failed")
        return False


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------

def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Human-transcribable single-use codes, shown once at enrolment."""
    codes = []
    for _ in range(count):
        raw = "".join(
            secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)
        )
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_codes(codes: list[str]) -> list[str]:
    return [hash_password(normalize_recovery_code(code)) for code in codes]


def normalize_recovery_code(code: str) -> str:
    """Strip formatting so ``abcde-fghij`` matches ``ABCDEFGHIJ``."""
    return "".join(ch for ch in (code or "").upper() if ch in RECOVERY_ALPHABET)


def consume_recovery_code(hashes: list[str], code: str) -> tuple[bool, list[str]]:
    """Try a recovery code. Returns (matched, remaining hashes).

    A matching code is removed from the list, which is what makes it
    single-use -- the caller persists the returned list.
    """
    normalized = normalize_recovery_code(code)
    if not normalized or not hashes:
        return False, hashes

    for index, stored in enumerate(hashes):
        if verify_password(stored, normalized):
            return True, hashes[:index] + hashes[index + 1 :]
    return False, hashes
