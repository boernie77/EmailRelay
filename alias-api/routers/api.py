"""Interne API-Endpunkte für smtp-proxy und VPS-Forwarder."""
import os
import secrets
import string
import asyncio
import httpx
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func

from database import get_db
from models import Alias, AliasMessageLog, AliasDomainConfig, EmailAddress, Domain, Setting, User, ReplyToken

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


async def _get_user_id_from_credentials(
    db: AsyncSession,
    username: str | None,
    password: str | None,
) -> int | None:
    """Prüft Username/Passwort und gibt user_id zurück, oder None."""
    if not username or not password:
        return None
    import bcrypt as _bcrypt
    user = (await db.execute(
        select(User).where(User.username == username, User.active == True)
    )).scalar_one_or_none()
    if not user:
        return None
    if not _bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return None
    return user.id


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


async def _send_ntfy(url: str, message: str, title: str = "E-Mail Relay"):
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
                    title="E-Mail Relay: VPS-Fehler",
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
    global _last_vps_ok_written
    now = datetime.now(timezone.utc)
    if _last_vps_ok_written is None or (now - _last_vps_ok_written) > _VPS_OK_WRITE_COOLDOWN:
        _last_vps_ok_written = now
        asyncio.create_task(_record_vps_event("last_vps_ok"))

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


@router.post("/forward-email/{alias_address}")
async def forward_email(
    alias_address: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_incoming_secret),
):
    """Empfängt eingehende Mail vom VPS und leitet sie weiter. Enthält die komplette Forward-Logik."""
    global _last_vps_ok_written
    now = datetime.now(timezone.utc)
    if _last_vps_ok_written is None or (now - _last_vps_ok_written) > _VPS_OK_WRITE_COOLDOWN:
        _last_vps_ok_written = now
        asyncio.create_task(_record_vps_event("last_vps_ok"))

    alias_address = alias_address.lower().strip()
    raw = await request.body()

    # Alias auflösen (inkl. Catch-all)
    alias = (await db.execute(
        select(Alias).where(Alias.alias_address == alias_address)
    )).scalar_one_or_none()

    if alias:
        if not alias.active:
            return Response(status_code=410)
        alias.last_used = func.now()
        await db.commit()
        real_address = alias.real_address
    else:
        domain_part = alias_address.split("@")[1].lower() if "@" in alias_address else ""
        if not domain_part:
            return Response(status_code=404)
        cfg_catchall = (await db.execute(
            select(AliasDomainConfig).where(
                AliasDomainConfig.alias_domain == domain_part,
                AliasDomainConfig.active == True,
                AliasDomainConfig.catchall_enabled == True,
            )
        )).scalar_one_or_none()
        if not cfg_catchall or not cfg_catchall.catchall_target_address:
            return Response(status_code=404)
        new_alias = Alias(
            alias_address=alias_address,
            real_address=cfg_catchall.catchall_target_address,
            label="(catch-all)",
            last_used=func.now(),
        )
        db.add(new_alias)
        await db.commit()
        real_address = cfg_catchall.catchall_target_address

    # SMTP-Config laden (Domain → AliasDomainConfig, Fallback auf globale Settings)
    domain_str = real_address.split("@")[1] if "@" in real_address else ""
    smtp_cfg = None
    if domain_str:
        domain_obj = (await db.execute(
            select(Domain)
            .options(selectinload(Domain.alias_domain_config))
            .where(Domain.domain == domain_str, Domain.active == True)
        )).scalars().first()
        if domain_obj and domain_obj.alias_domain_config and domain_obj.alias_domain_config.active:
            c = domain_obj.alias_domain_config
            smtp_cfg = {
                "host": c.smtp_host,
                "port": int(c.smtp_port or 587),
                "user": c.smtp_user,
                "password": c.smtp_password,
                "use_tls": c.smtp_use_tls,
            }
    if not smtp_cfg:
        smtp_cfg = {
            "host": await get_setting(db, "smtp_host") or "",
            "port": int(await get_setting(db, "smtp_port") or 587),
            "user": await get_setting(db, "smtp_user") or "",
            "password": await get_setting(db, "smtp_password") or "",
            "use_tls": (await get_setting(db, "smtp_use_tls") or "true") != "false",
        }
    if not smtp_cfg["host"]:
        return Response(status_code=500, content=b"SMTP nicht konfiguriert")

    # Mail-Header anpassen
    from email.parser import BytesParser
    from email import policy as _ep
    msg = BytesParser(policy=_ep.default).parsebytes(raw)

    # Reply-Gateway: Reply-To auf reply-TOKEN@alias_domain setzen.
    # Wenn der Empfänger antwortet (egal ob Gmail, GMX, Thunderbird, ...), landet die
    # Antwort beim VPS → forward_reply-Endpoint → From wird durch Alias ersetzt.
    # So bleibt die echte Adresse des Users auch beim Antworten verborgen.
    original_from = msg.get("From", "")
    if original_from:
        token = secrets.token_urlsafe(24)
        alias_domain_part = alias_address.split("@")[1] if "@" in alias_address else ""
        reply_token_obj = ReplyToken(
            token=token,
            alias_address=alias_address,
            original_sender=original_from,
        )
        db.add(reply_token_obj)
        await db.commit()
        del msg["Reply-To"]
        msg["Reply-To"] = f"reply-{token}@{alias_domain_part}"

    # ╔══════════════════════════════════════════════════════════════════════════╗
    # ║ ACHTUNG: From = alias_address, NIEMALS smtp_cfg["user"]!               ║
    # ║                                                                          ║
    # ║ Wenn From = echtes Postfach (smtp_cfg["user"], z.B. user@strato.de)     ║
    # ║ und das ist eine Thunderbird-Identität des Empfängers:                  ║
    # ║   → Thunderbird erkennt Mail als "selbst gesendet"                      ║
    # ║   → Reply nutzt To-Feld statt Reply-To                                  ║
    # ║   → Antwort geht an Alias → Alias löst zu echter Adresse → Loop        ║
    # ║   → Mail landet wieder in eigener Inbox (endloser Kreis)                ║
    # ║                                                                          ║
    # ║ Alias-Adresse als From:                                                  ║
    # ║   ✓ Zeigt dem User welcher Alias die Mail empfangen hat                  ║
    # ║   ✓ Ist keine Thunderbird-Eigenidentität → Reply-To wird korrekt genutzt ║
    # ║   ✓ DMARC: subdomain sp=NONE → kein Enforcement für Alias-Subdomains    ║
    # ╚══════════════════════════════════════════════════════════════════════════╝
    del msg["From"]
    msg["From"] = alias_address

    # WICHTIG: To-Header NUR mit Alias, NICHT formataddr((alias, real_address)).
    # formataddr würde die echte Adresse für Empfänger sichtbar machen und bei
    # Reply-All leaken. Zustellung läuft über Envelope (sendmail), nicht To-Header.
    del msg["To"]
    msg["To"] = alias_address

    # Senden via SMTP
    try:
        import aiosmtplib
        smtp = aiosmtplib.SMTP(
            hostname=smtp_cfg["host"],
            port=smtp_cfg["port"],
            start_tls=smtp_cfg["use_tls"],
        )
        await smtp.connect()
        await smtp.login(smtp_cfg["user"], smtp_cfg["password"])
        await smtp.sendmail(smtp_cfg["user"], [real_address], msg.as_bytes(policy=_ep.SMTP))
        await smtp.quit()
    except Exception as e:
        import logging
        logging.getLogger("api").error(f"SMTP-Fehler beim Forwarden an {real_address}: {e}")
        return Response(status_code=500, content=str(e).encode())

    return Response(status_code=200)


@router.post("/auth/validate")
async def auth_validate(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
):
    """Prüft Benutzername/Passwort für SMTP-Auth (smtp-proxy → alias-api)."""
    import bcrypt as _bcrypt
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(status_code=401, detail="Credentials fehlen")
    user = (await db.execute(
        select(User).where(User.username == username, User.active == True)
    )).scalar_one_or_none()
    if not user or not _bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        raise HTTPException(status_code=401, detail="Ungültige Zugangsdaten")
    return {"ok": True, "user_id": user.id}


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
        )).scalars().first()

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


@router.get("/addresses")
async def list_addresses(
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
    x_username: str | None = Header(None),
    x_password: str | None = Header(None),
):
    """Gibt aktiven E-Mail-Adressen zurück. Mit User-Credentials nur die des Benutzers."""
    user_id = await _get_user_id_from_credentials(db, x_username, x_password)
    query = select(EmailAddress).where(EmailAddress.active == True)
    if user_id:
        query = query.join(Domain, EmailAddress.domain_id == Domain.id).where(Domain.user_id == user_id)
    result = await db.execute(query.order_by(EmailAddress.address))
    return [{"address": a.address} for a in result.scalars().all()]


@router.post("/alias/create")
async def create_alias_with_label(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_secret),
    x_username: str | None = Header(None),
    x_password: str | None = Header(None),
):
    """Erstellt einen neuen Alias mit optionaler Bezeichnung (für Chrome Extension)."""
    real_address = payload.get("real_address", "").strip().lower()
    label = payload.get("label", "").strip()

    email_addr = (await db.execute(
        select(EmailAddress)
        .options(selectinload(EmailAddress.domain).selectinload(Domain.alias_domain_config))
        .where(EmailAddress.address == real_address, EmailAddress.active == True)
    )).scalar_one_or_none()
    if not email_addr:
        raise HTTPException(status_code=404, detail="Adresse nicht konfiguriert")

    domain_obj = email_addr.domain
    alias_domain = None
    if domain_obj and domain_obj.alias_domain_config and domain_obj.alias_domain_config.active:
        alias_domain = domain_obj.alias_domain_config.alias_domain
    if not alias_domain:
        alias_domain = await get_setting(db, "alias_domain")
    if not alias_domain:
        raise HTTPException(status_code=500, detail="Alias-Domain nicht konfiguriert")

    for _ in range(10):
        local = generate_alias_local()
        candidate = f"{local}@{alias_domain}"
        if not (await db.execute(select(Alias).where(Alias.alias_address == candidate))).scalar_one_or_none():
            break
    else:
        raise HTTPException(status_code=500, detail="Konnte keinen eindeutigen Alias generieren")

    user_id = await _get_user_id_from_credentials(db, x_username, x_password)
    if not user_id:
        user_id = domain_obj.user_id if domain_obj else None
    new_alias = Alias(alias_address=candidate, real_address=real_address, label=label, user_id=user_id)
    db.add(new_alias)
    await db.commit()
    return {"alias_address": candidate, "real_address": real_address, "label": label}


@router.post("/settings/test-ntfy")
async def test_ntfy(db: AsyncSession = Depends(get_db), _=Depends(verify_secret)):
    """Sendet eine Test-Benachrichtigung an die konfigurierte ntfy-URL."""
    ntfy_url = await _get_ntfy_url(db)
    if not ntfy_url:
        raise HTTPException(status_code=400, detail="Keine ntfy-URL konfiguriert")
    await _send_ntfy(ntfy_url, "Test-Benachrichtigung von E-Mail Relay ✓", title="E-Mail Relay: Test")
    return {"ok": True}
