"""Outgoing email over SMTP: Mailpit locally (inbox UI at http://localhost:8025), SendGrid's
SMTP relay later. Plain text only: no HTML, so nothing a user typed can become markup.

Sign-in emails are sent straight from the request (not via the job queue): the visitor gets an
immediate error if sending fails, and the sign-in link never sits in a queue in Redis.
"""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from config import settings

log = logging.getLogger("easychat.mailer")

SMTP_TIMEOUT_SECONDS = 10


class MailError(RuntimeError):
    """The email could not be handed to the SMTP server."""


def _send_now(to: str, subject: str, text: str) -> None:
    """Blocking SMTP send (run in a thread by send_email)."""
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        if settings.smtp_username:          # real relays: encrypted and authenticated
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def send_email(to: str, subject: str, text: str) -> None:
    """Send one plain-text email. Raises MailError (details are logged, never shown)."""
    try:
        await asyncio.to_thread(_send_now, to, subject, text)
    except (OSError, smtplib.SMTPException) as exc:
        log.error("email not sent: %s", type(exc).__name__)
        raise MailError("could not send email") from exc
