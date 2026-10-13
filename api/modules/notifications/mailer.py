"""SMTP email delivery. smtplib blocks, so it runs off the event loop."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from api.core.config import settings


def build_message(to: str, subject: str, body: str) -> EmailMessage:
    """Header values with line breaks are refused, so no header can be injected."""
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="visionpulse")
    message.set_content(body)
    return message


def _send_blocking(message: EmailMessage) -> None:
    host, port = settings.smtp_host, settings.smtp_port
    timeout = settings.smtp_timeout_s
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
    with server:
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password or "")
        server.send_message(message)


async def send(to: str, subject: str, body: str) -> None:
    """Send one email over SMTP. Raises on any failure."""
    await asyncio.to_thread(_send_blocking, build_message(to, subject, body))
