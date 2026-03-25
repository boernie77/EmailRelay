"""Interne API-Endpunkte für smtp-proxy und VPS-Forwarder."""
import os
import secrets
import string
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func

from database import get_db
from models import Alias, AliasMessageLog, AliasDomainConfig, EmailAddress, Domain, Setting

router = APIRouter(prefix="/api", tags=["api"])

API_SECRET = os.getenv("API_SECRET", "")


def verify_secret(x_api_secret: str = Header(...)):
    if x_api_secret != API_SECRET:
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

    # Bestehenden Alias suchen
    result = await db.execute(
        select(Alias).where(
            Alias.real_address == real_address,
            Alias.active == True,
        )
    )
    alias = result.scalar_one_or_none()

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
    _=Depends(verify_secret),
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
