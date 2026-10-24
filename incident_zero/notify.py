"""Email for approvals and escalations (REQUIREMENTS C9), through the SMTP settings in .env (SMTP_HOST, SMTP_PORT,
SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_STARTTLS, ADMIN_EMAIL; see config.mail_settings). Silent when SMTP is not
configured; the Control Room badge and browser notification still fire."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import mail_settings


def send(settings, subject: str, body: str) -> str:
    """Returns what happened, for the event log. Never raises."""
    try:
        m = mail_settings()
    except Exception as exc:
        return f"email skipped: settings unavailable ({exc})"
    to = settings.notify_email or m.admin_email
    if not m.host or not to:
        return "email skipped: SMTP or recipient not configured"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, m.sender, to
    msg.set_content(body)
    try:
        with smtplib.SMTP(m.host, m.port, timeout=15) as s:
            if m.starttls:
                s.starttls(context=ssl.create_default_context())
            if m.user:
                s.login(m.user, m.password)
            s.send_message(msg)
        return f"email sent to {to}"
    except Exception as exc:
        return f"email failed: {exc.__class__.__name__}: {exc}"
