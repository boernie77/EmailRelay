"""Zurückgehaltene Mails: dauerhaft abgelehnte Weiterleitungen erkennen, speichern und erneut zustellen."""

import logging
import os
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser

import aiosmtplib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import Domain, HeldMessage, Setting

log = logging.getLogger("api")

HELD_RETENTION_DAYS = 30
_PERMANENT_MIN = 500
_PERMANENT_MAX = 599
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t]+")
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
_BREAK_TAGS = frozenset({"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"})


async def _setting(db: AsyncSession, key: str) -> str:
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row and row.value else ""


async def load_smtp_cfg(db: AsyncSession, real_address: str) -> dict | None:
    """SMTP-Config für die Zieladresse (Domain → AliasDomainConfig, Fallback globale Settings)."""
    domain_str = real_address.split("@")[1] if "@" in real_address else ""
    if domain_str:
        domain_obj = (
            (
                await db.execute(
                    select(Domain)
                    .options(selectinload(Domain.alias_domain_config))
                    .where(Domain.domain == domain_str, Domain.active == True)
                )
            )
            .scalars()
            .first()
        )
        if domain_obj and domain_obj.alias_domain_config and domain_obj.alias_domain_config.active:
            c = domain_obj.alias_domain_config
            return {
                "host": c.smtp_host,
                "port": int(c.smtp_port or 587),
                "user": c.smtp_user,
                "password": c.smtp_password,
                "use_tls": c.smtp_use_tls,
            }
    cfg = {
        "host": await _setting(db, "smtp_host"),
        "port": int(await _setting(db, "smtp_port") or 587),
        "user": await _setting(db, "smtp_user"),
        "password": await _setting(db, "smtp_password"),
        "use_tls": (await _setting(db, "smtp_use_tls") or "true") != "false",
    }
    return cfg if cfg["host"] else None


async def smtp_deliver(smtp_cfg: dict, real_address: str, raw: bytes) -> None:
    """Sendet die fertige Mail (Envelope MAIL FROM = SMTP-User, RCPT TO = echte Adresse)."""
    smtp = aiosmtplib.SMTP(
        hostname=smtp_cfg["host"],
        port=smtp_cfg["port"],
        start_tls=smtp_cfg["use_tls"],
    )
    await smtp.connect()
    try:
        await smtp.login(smtp_cfg["user"], smtp_cfg["password"])
        await smtp.sendmail(smtp_cfg["user"], [real_address], raw)
    finally:
        try:
            await smtp.quit()
        except Exception:
            pass


def is_permanent_rejection(exc: Exception) -> bool:
    """True, wenn der Zielserver die Mail selbst mit 5xx abgelehnt hat.

    Login-Fehler (535) und Verbindungsfehler zählen nicht: das ist ein Konfigurations- bzw.
    Netzproblem, die Mail selbst ist in Ordnung und soll von Postfix weiter wiederholt werden.
    """
    if isinstance(exc, aiosmtplib.SMTPAuthenticationError | aiosmtplib.SMTPConnectError):
        return False
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        return bool(codes) and all(_PERMANENT_MIN <= c <= _PERMANENT_MAX for c in codes)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return _PERMANENT_MIN <= exc.code <= _PERMANENT_MAX
    return False


def describe_error(exc: Exception) -> str:
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        return "; ".join(f"{r.code} {r.message}" for r in exc.recipients)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return f"{exc.code} {exc.message}"
    return str(exc)


async def hold_message(
    db: AsyncSession,
    *,
    user_id: int | None,
    alias_address: str,
    real_address: str,
    raw: bytes,
    error: str,
    sender: str = "",
) -> HeldMessage:
    parsed = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    held = HeldMessage(
        user_id=user_id,
        alias_address=alias_address,
        real_address=real_address,
        sender=(sender or str(parsed.get("From") or ""))[:500],
        subject=str(parsed.get("Subject") or "")[:500],
        size=len(raw),
        raw=raw,
        error=error[:2000],
    )
    db.add(held)
    await db.commit()
    return held


def public_url() -> str:
    return os.getenv("PUBLIC_URL", "").rstrip("/")


# ── Variante "Ohne Links zustellen" ────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """HTML → Klartext. Links bleiben als "Text (URL)" sichtbar."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href, self._href = self._href, None
            if href and href.startswith(("http://", "https://")):
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


_KEEP_HEADERS = ("From", "To", "Reply-To", "Sender", "Subject", "Date", "Message-ID")


def strip_to_plaintext(raw: bytes) -> bytes:
    """Baut aus der Mail eine reine Text-Mail: HTML/Anhänge entfallen, Header bleiben erhalten."""
    original = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = original.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        text = "(Kein Textinhalt in der Originalmail)"
    elif body_part.get_content_type() == "text/html":
        text = _html_to_text(body_part.get_content())
    else:
        text = body_part.get_content().strip()

    attachments = [p.get_filename() or p.get_content_type() for p in original.iter_attachments()]
    note = (
        "[E-Mail Relay: Mail wurde ohne HTML und Anhänge zugestellt, da der Mailserver die Originalmail abgelehnt hat.]"
    )
    if attachments:
        note += f"\n[Entfernte Anhänge: {', '.join(attachments)}]"

    msg = EmailMessage(policy=policy.SMTP)
    for name in _KEEP_HEADERS:
        value = original.get(name)
        if value:
            msg[name] = str(value)
    msg.set_content(f"{note}\n\n{text}\n")
    return msg.as_bytes(policy=policy.SMTP)


def sender_display(sender: str) -> str:
    name, addr = parseaddr(sender)
    return name or addr or sender


async def purge_old_held(db: AsyncSession) -> int:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=HELD_RETENTION_DAYS)
    result = await db.execute(delete(HeldMessage).where(HeldMessage.created_at < cutoff))
    await db.commit()
    return result.rowcount or 0


async def purge_scheduler() -> None:
    """Löscht zurückgehaltene Mails nach HELD_RETENTION_DAYS Tagen (läuft täglich)."""
    import asyncio

    from database import AsyncSessionLocal

    while True:
        try:
            async with AsyncSessionLocal() as session:
                await purge_old_held(session)
        except Exception as e:
            log.error(f"Held-Purge fehlgeschlagen: {e}")
        await asyncio.sleep(24 * 3600)
