"""Interne API-Endpunkte für smtp-proxy und VPS-Forwarder."""
import os
import secrets
import string
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func

from database import get_db
from models import Alias, AliasMessageLog, AliasDomainConfig, EmailAddress, Domain, Setting

router = APIRouter(prefix="/api", tags=["api"])

API_SECRET = os.getenv("API_SECRET", "")

_last_ntfy_sent: datetime | None = None
_NTFY_COOLDOWN = timedelta(hours=1)
_last_vps_ok_written: datetime | None = None
_VPS_OK_WRITE_COOLDOWN = timedelta(minutes=10)


def verify_secret(x_api_secret: str = Header(...)):
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=403, detail="Ungültiger API-Key")


async def _get_ntfy_url(db: AsyncSession) -> str | None:
    result = await db.execute(select(Setting).where(Setting.key == "ntfy_url"))
    s = result.scalar_one_or_none()
    return s.value if s and s.value else None


async def _record_vps_event(key: str):
    """Schreibt Zeitstempel eines VPS-Ereignisses in die Settings-Tabelle."""
    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        now_iso = datetime.now(timezone.utc).isoformat()
        if existing:
            existing.value = now_iso
        else:
            session.add(Setting(key=key, value=now_iso))
        await session.commit()


async def _send_ntfy(url: str, message: str, title: str = "EmailRelay"):
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, content=message.encode(),
                              headers={"Title": title, "Priority": "high"}, timeout=5)
        except Exception:
            pass


async def verify_incoming_secret(
    x_api_secret: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Auth für VPS-Aufrufe — sendet ntfy-Benachrichtigung bei falschem Secret."""
    global _last_ntfy_sent
    if x_api_secret != API_SECRET:
        now = datetime.now(timezone.utc)
        if _last_ntfy_sent is None or (now - _last_ntfy_sent) > _NTFY_COOLDOWN:
            ntfy_url = await _get_ntfy_url(db)
            if ntfy_url:
                _last_ntfy_sent = now
                asyncio.create_task(_send_ntfy(
                    ntfy_url,
                    "VPS konnte sich nicht authentifizieren (403 Forbidden).\n"
                    "API-Secret veraltet? → VPS-Setup unter /vps ausführen.",
                    title="EmailRelay: VPS-Fehler",
                ))
        asyncio.create_task(_record_vps_event("last_vps_403"))
        raise HTTPException(status_code=403, detail="Ungültiger API-Key")


def generate_alias_local() -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(10))


async def get_setting(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(Setting).where(Setting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


@router.get("/alias/outgoing/{real_address}")
async def get_or_create_alias(
    real_address: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """Gibt den Alias für eine echte Adresse zurück. Erstellt ihn falls nötig."""
    # Prüfen ob Adresse in der Liste ist, mit Relationship-Preload
    result = await db.execute(
        select(EmailAddress)
        .options(selectinload(EmailAddress.domain).selectinload(Domain.alias_domain_config))
        .where(EmailAddress.address == real_address, EmailAddress.active == True)
    )
    email_addr = result.scalar_one_or_none()
    if not email_addr:
        raise HTTPException(status_code=404, detail="Adresse nicht für Aliase konfiguriert")

    # Bestehenden Alias suchen – zuletzt genutzten nehmen (mehrere möglich)
    result = await db.execute(
        select(Alias).where(
            Alias.real_address == real_address,
            Alias.active == True,
        ).order_by(Alias.last_used.desc().nullslast(), Alias.created_at.desc())
    )
    alias = result.scalars().first()

    if alias:
        # last_used aktualisieren
        alias.last_used = func.now()
        await db.commit()
        return {"alias_address": alias.alias_address, "real_address": real_address}

    # Alias-Domain ermitteln: erst aus Domain-Config, dann globaler Fallback
    domain_obj = email_addr.domain
    alias_domain = None
    if domain_obj and domain_obj.alias_domain_config and domain_obj.alias_domain_config.active:
        alias_domain = domain_obj.alias_domain_config.alias_domain
    if not alias_domain:
        alias_domain = await get_setting(db, "alias_domain")  # Legacy-Fallback
    if not alias_domain:
        raise HTTPException(status_code=500, detail="Alias-Domain nicht konfiguriert")

    for _ in range(10):
        local = generate_alias_local()
        candidate = f"{local}@{alias_domain}"
        existing = await db.execute(select(Alias).where(Alias.alias_address == candidate))
        if not existing.scalar_one_or_none():
            break
    else:
        raise HTTPException(status_code=500, detail="Konnte keinen eindeutigen Alias generieren")

    new_alias = Alias(alias_address=candidate, real_address=real_address)
    db.add(new_alias)
    await db.commit()
    return {"alias_address": candidate, "real_address": real_address}


@router.get("/alias/incoming/{alias_address}")
async def resolve_alias(
    alias_address: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_incoming_secret),
):
    """Löst einen Alias zur echten Adresse auf (für VPS-Forwarder). Erstellt ihn bei Catch-all automatisch."""
    result = await db.execute(
        select(Alias).where(Alias.alias_address == alias_address)
    )
    alias = result.scalar_one_or_none()
    if alias:
        if not alias.active:
            raise HTTPException(status_code=410, detail="Alias blockiert")
        alias.last_used = func.now()
        await db.commit()
        return {"alias_address": alias_address, "real_address": alias.real_address}

    # Alias unbekannt – Catch-all prüfen
    domain_part = alias_address.split("@")[1].lower() if "@" in alias_address else ""
    if not domain_part:
        raise HTTPException(status_code=404, detail="Alias nicht gefunden")

    cfg = (await db.execute(
        select(AliasDomainConfig).where(
            AliasDomainConfig.alias_domain == domain_part,
            AliasDomainConfig.active == True,
            AliasDomainConfig.catchall_enabled == True,
        )
    )).scalar_one_or_none()

    if not cfg or not cfg.catchall_target_address:
        raise HTTPException(status_code=404, detail="Alias nicht gefunden")

    new_alias = Alias(
        alias_address=alias_address,
        real_address=cfg.catchall_target_address,
        label="(catch-all)",
        last_used=func.now(),
    )
    db.add(new_alias)
    await db.commit()
    return {"alias_address": alias_address, "real_address": cfg.catchall_target_address}


@router.get("/settings/smtp")
async def get_smtp_settings(
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """SMTP-Einstellungen (global, Fallback). Veraltet – smtp-proxy nutzt /smtp-config/{address}."""
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_use_tls"]
    result = {}
    for key in keys:
        result[key] = await get_setting(db, key)
    return result


@router.get("/smtp-config/{sender_address}")
async def get_smtp_config(
    sender_address: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """SMTP-Konfiguration für eine Absenderadresse. Prüft Domain-AliasDomainConfig, dann globale Settings."""
    sender_address = sender_address.lower().strip()
    domain_str = sender_address.split("@")[1] if "@" in sender_address else ""

    # Domain mit zugehöriger AliasDomainConfig laden
    if domain_str:
        domain_obj = (await db.execute(
            select(Domain)
            .options(selectinload(Domain.alias_domain_config))
            .where(Domain.domain == domain_str, Domain.active == True)
        )).scalar_one_or_none()

        if domain_obj and domain_obj.alias_domain_config and domain_obj.alias_domain_config.active:
            cfg = domain_obj.alias_domain_config
            return {
                "smtp_host": cfg.smtp_host,
                "smtp_port": str(cfg.smtp_port),
                "smtp_user": cfg.smtp_user,
                "smtp_password": cfg.smtp_password,
                "smtp_use_tls": "true" if cfg.smtp_use_tls else "false",
            }

    # Fallback: globale Settings
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_use_tls"]
    return {key: await get_setting(db, key) for key in keys}


@router.post("/alias/message-log")
async def log_message_alias(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """Speichert Message-ID → Alias für spätere Reply-Zuordnung (smtp-proxy → API)."""
    message_id = payload.get("message_id", "").strip().strip("<>")
    alias_address = payload.get("alias_address", "").strip()
    if not message_id or not alias_address:
        raise HTTPException(status_code=400, detail="message_id und alias_address erforderlich")
    entry = AliasMessageLog(message_id=message_id, alias_address=alias_address)
    db.add(entry)
    try:
        await db.commit()
    except Exception:
        await db.rollback()  # Duplikat ignorieren
    return {"ok": True}


@router.get("/alias/message-log")
async def get_alias_for_message(
    message_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """Gibt den Alias zurück, der für eine bestimmte Message-ID verwendet wurde."""
    clean_id = message_id.strip().strip("<>")
    result = await db.execute(
        select(AliasMessageLog).where(AliasMessageLog.message_id == clean_id)
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Kein Alias für diese Message-ID")
    return {"alias_address": entry.alias_address}


@router.post("/settings/test-ntfy")
async def test_ntfy(db: AsyncSession = Depends(get_db), _=Depends(verify_secret)):
    """Sendet eine Test-Benachrichtigung an die konfigurierte ntfy-URL."""
    ntfy_url = await _get_ntfy_url(db)
    if not ntfy_url:
        raise HTTPException(status_code=400, detail="Keine ntfy-URL konfiguriert")
    await _send_ntfy(ntfy_url, "Test-Benachrichtigung von EmailRelay ✓", title="EmailRelay: Test")
    return {"ok": True}
