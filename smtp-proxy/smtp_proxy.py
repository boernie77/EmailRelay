"""
EmailRelay SMTP-Proxy

Lauscht auf Port 1587. Thunderbird verbindet sich hier.
- Ausgehende Mails: From-Adresse wird durch Alias ersetzt
- Weiterleitung über den konfigurierten echten SMTP-Server
"""
import asyncio
import logging
import os
import email
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, formataddr

import httpx
import aiosmtplib
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import AsyncMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smtp-proxy")

API_URL = os.getenv("API_URL", "http://alias-api:8080")
API_SECRET = os.getenv("API_SECRET", "")
PROXY_PORT = int(os.getenv("PROXY_PORT", "1587"))
RETRY_DELAY = 5  # Sekunden zwischen API-Verbindungsversuchen


async def fetch_smtp_config(sender_address: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_URL}/api/smtp-config/{sender_address}",
            headers={"x-api-secret": API_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


async def get_or_create_alias(real_address: str) -> str | None:
    """Gibt den Alias für eine Adresse zurück, oder None wenn nicht konfiguriert."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{API_URL}/api/alias/outgoing/{real_address}",
            headers={"x-api-secret": API_SECRET},
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["alias_address"]


async def get_alias_for_reply(in_reply_to: str) -> str | None:
    """Gibt den Alias zurück, der für eine Message-ID verwendet wurde (für Antworten)."""
    clean_id = in_reply_to.strip().strip("<>")
    if not clean_id:
        return None
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{API_URL}/api/alias/message-log",
                params={"message_id": clean_id},
                headers={"x-api-secret": API_SECRET},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()["alias_address"]
        except Exception as e:
            log.warning(f"Reply-Alias-Lookup fehlgeschlagen: {e}")
    return None


async def log_message_alias(message_id: str, alias_address: str):
    """Speichert Message-ID → Alias für zukünftige Replies."""
    clean_id = message_id.strip().strip("<>")
    if not clean_id:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"{API_URL}/api/alias/message-log",
                json={"message_id": clean_id, "alias_address": alias_address},
                headers={"x-api-secret": API_SECRET},
                timeout=5,
            )
        except Exception as e:
            log.warning(f"Message-ID konnte nicht geloggt werden: {e}")


class AliasHandler(AsyncMessage):
    async def handle_message(self, message: email.message.Message):
        # From-Header parsen
        raw_from = message.get("From", "")
        display_name, real_address = parseaddr(raw_from)
        real_address = real_address.lower().strip()

        log.info(f"Ausgehende Mail von: {real_address}")

        alias_address = None
        try:
            # Bei Antworten: Alias der ursprünglichen Mail wiederverwenden
            in_reply_to = message.get("In-Reply-To", "").strip()
            if in_reply_to:
                alias_address = await get_alias_for_reply(in_reply_to)
                if alias_address:
                    log.info(f"Reply-Alias für {in_reply_to}: {alias_address}")
            # Kein Reply-Alias → normalen Alias ermitteln
            if not alias_address:
                alias_address = await get_or_create_alias(real_address)
        except Exception as e:
            log.warning(f"Alias-API nicht erreichbar: {e} – sende ohne Alias")

        if alias_address:
            log.info(f"Ersetze From: {real_address} → {alias_address}")
            del message["From"]
            message["From"] = formataddr((display_name, alias_address))
            # Reply-To entfernen – würde sonst die echte Adresse verraten
            del message["Reply-To"]
            # Message-ID für zukünftige Replies loggen
            msg_id = message.get("Message-ID", "").strip()
            if msg_id:
                await log_message_alias(msg_id, alias_address)

        # SMTP-Konfiguration laden (per Absenderadresse, Fallback auf globale Settings)
        try:
            cfg = await fetch_smtp_config(real_address)
        except Exception as e:
            log.error(f"SMTP-Konfiguration nicht ladbar: {e}")
            raise

        host = cfg.get("smtp_host", "")
        port = int(cfg.get("smtp_port") or 587)
        user = cfg.get("smtp_user", "")
        password = cfg.get("smtp_password", "")
        use_tls = cfg.get("smtp_use_tls", "true") != "false"

        if not host:
            log.error("Kein SMTP-Server konfiguriert. Bitte in den Einstellungen eintragen.")
            raise RuntimeError("SMTP-Server nicht konfiguriert")

        # Mail weiterleiten
        recipients = (
            [message["To"]] if message["To"] else []
        )
        if message["Cc"]:
            recipients.append(message["Cc"])

        log.info(f"Weiterleitung an {host}:{port} (TLS={use_tls})")
        try:
            await aiosmtplib.send(
                message,
                hostname=host,
                port=port,
                username=user,
                password=password,
                start_tls=use_tls,
            )
            log.info("Mail erfolgreich weitergeleitet")
        except Exception as e:
            log.error(f"SMTP-Fehler: {e}")
            raise


async def wait_for_api():
    """Wartet bis die alias-api erreichbar ist."""
    log.info(f"Warte auf alias-api unter {API_URL} ...")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{API_URL}/api/settings/smtp",
                                        headers={"x-api-secret": API_SECRET},
                                        timeout=5)
                if resp.status_code in (200, 500):  # 500 = API läuft, DB-Fehler ignorieren
                    log.info("alias-api erreichbar")
                    return
        except Exception:
            pass
        log.info(f"alias-api noch nicht bereit, warte {RETRY_DELAY}s ...")
        await asyncio.sleep(RETRY_DELAY)


async def main():
    await wait_for_api()
    handler = AliasHandler()
    controller = Controller(handler, hostname="0.0.0.0", port=PROXY_PORT)
    controller.start()
    log.info(f"SMTP-Proxy gestartet auf Port {PROXY_PORT}")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
