"""Hilfsfunktionen für System-E-Mails (Registrierung, Passwort-Reset etc.)."""

import aiosmtplib
from email.message import EmailMessage
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import Setting


async def get_system_smtp(db: AsyncSession) -> dict | None:
    """Liest System-SMTP-Konfiguration aus Settings. Gibt None zurück wenn nicht konfiguriert."""
    cfg = {}
    for key in [
        "system_smtp_host",
        "system_smtp_port",
        "system_smtp_user",
        "system_smtp_password",
        "system_smtp_from",
        "system_smtp_use_tls",
    ]:
        row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        cfg[key] = row.value if row else ""
    if not cfg["system_smtp_host"] or not cfg["system_smtp_user"]:
        return None
    return cfg


async def send_system_email(to: str, subject: str, html_body: str, db: AsyncSession) -> bool:
    """Sendet eine System-E-Mail. Gibt True zurück wenn erfolgreich."""
    cfg = await get_system_smtp(db)
    if not cfg:
        return False
    msg = EmailMessage()
    msg["From"] = cfg.get("system_smtp_from") or cfg["system_smtp_user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(html_body, subtype="html")
    try:
        use_tls = cfg.get("system_smtp_use_tls", "true") != "false"
        smtp = aiosmtplib.SMTP(
            hostname=cfg["system_smtp_host"],
            port=int(cfg.get("system_smtp_port") or 587),
            start_tls=use_tls,
        )
        await smtp.connect()
        await smtp.login(cfg["system_smtp_user"], cfg["system_smtp_password"])
        await smtp.send_message(msg)
        await smtp.quit()
        return True
    except Exception:
        return False
