"""Outbound email.

Deliberately built on ``smtplib`` rather than an extension: the app sends a
handful of transactional messages, and a dependency that has to be configured
before the app will boot is worse than a small amount of code here.

Three backends:

``console``
    Writes the message to the log. The default in development, so a password
    reset can be completed without any mail infrastructure -- the link is right
    there in the terminal.

``smtp``
    Real delivery. The only backend that should be used in production.

``null``
    Silently records messages in memory. Used by the tests.

Delivery failures never abort the surrounding request. A password reset that
cannot be emailed is still a valid reset; the operator needs to see the failure
in the log, but the user should not get a 500 for it.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from flask import current_app, render_template

logger = logging.getLogger(__name__)

#: Messages captured by the ``null`` backend, for assertions in tests.
outbox: list[Message] = []


@dataclass
class Message:
    to: str
    subject: str
    text: str
    html: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def as_mime(self, sender: str, sender_name: str | None = None) -> EmailMessage:
        message = EmailMessage()
        message["From"] = formataddr((sender_name or "", sender))
        message["To"] = self.to
        message["Subject"] = self.subject
        message["Message-ID"] = make_msgid()
        # Transactional mail should never end up in a bulk-mail loop.
        message["Auto-Submitted"] = "auto-generated"
        for key, value in self.headers.items():
            message[key] = value
        message.set_content(self.text)
        if self.html:
            message.add_alternative(self.html, subtype="html")
        return message


class MailError(RuntimeError):
    """Delivery failed. Raised only by ``send(..., raise_on_error=True)``."""


def _backend() -> str:
    configured = (current_app.config.get("MAIL_BACKEND") or "").strip().lower()
    if configured:
        return configured
    if current_app.testing:
        return "null"
    return "smtp" if current_app.config.get("MAIL_SERVER") else "console"


def send(message: Message, *, raise_on_error: bool = False) -> bool:
    """Deliver a message. Returns True when it was handed off successfully."""
    backend = _backend()
    config = current_app.config
    sender = config.get("MAIL_SENDER") or "scopemaker@localhost"
    sender_name = config.get("MAIL_SENDER_NAME") or config.get("APP_NAME")

    if backend == "null":
        outbox.append(message)
        return True

    if backend == "console":
        logger.info(
            "Email not sent (MAIL_BACKEND=console).\n"
            "  To:      %s\n  Subject: %s\n%s",
            message.to,
            message.subject,
            _indent(message.text),
        )
        return True

    if backend != "smtp":  # pragma: no cover - operator error
        logger.error("Unknown MAIL_BACKEND %r; message to %s dropped", backend, message.to)
        return False

    try:
        _send_smtp(message.as_mime(sender, sender_name), config)
    except Exception as exc:
        logger.exception("Could not send %r to %s", message.subject, message.to)
        if raise_on_error:
            raise MailError(str(exc)) from exc
        return False

    logger.info("Sent %r to %s", message.subject, message.to)
    return True


def _send_smtp(mime: EmailMessage, config) -> None:
    host = config["MAIL_SERVER"]
    port = int(config.get("MAIL_PORT") or 587)
    timeout = int(config.get("MAIL_TIMEOUT") or 15)
    username = config.get("MAIL_USERNAME")
    password = config.get("MAIL_PASSWORD")

    if config.get("MAIL_USE_SSL"):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as client:
            if username:
                client.login(username, password or "")
            client.send_message(mime)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as client:
        client.ehlo()
        if config.get("MAIL_USE_TLS", True):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if username:
            client.login(username, password or "")
        client.send_message(mime)


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.strip().splitlines())


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def send_template(
    *,
    to: str,
    subject: str,
    template: str,
    **context,
) -> bool:
    """Render ``emails/<template>.txt`` (and ``.html`` when present) and send."""
    text = render_template(f"emails/{template}.txt", **context)
    html: str | None
    try:
        html = render_template(f"emails/{template}.html", **context)
    except Exception:
        html = None
    return send(Message(to=to, subject=subject, text=text, html=html))


def send_password_reset(*, to: str, name: str, url: str, expires_hours: int) -> bool:
    return send_template(
        to=to,
        subject=f"Reset your {current_app.config['APP_NAME']} password",
        template="password_reset",
        name=name,
        url=url,
        expires_hours=expires_hours,
    )


def send_invitation(*, to: str, organization: str, inviter: str, url: str) -> bool:
    return send_template(
        to=to,
        subject=f"You have been invited to {organization} on "
        f"{current_app.config['APP_NAME']}",
        template="invitation",
        organization=organization,
        inviter=inviter,
        url=url,
    )


def send_password_changed(*, to: str, name: str) -> bool:
    return send_template(
        to=to,
        subject=f"Your {current_app.config['APP_NAME']} password was changed",
        template="password_changed",
        name=name,
    )
