"""UI-Routen für das Web-Interface."""
import asyncio
import io
import os
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from database import get_db
from models import Setting, Domain, EmailAddress, Alias

_VPS_SETUP_SCRIPT = r"""#!/bin/bash
set -e
ALIAS_DOMAIN='__ALIAS_DOMAIN__'
API_URL='__API_URL__'
API_SECRET='__API_SECRET__'

echo "=== EmailRelay VPS Setup ==="
echo "Domain: $ALIAS_DOMAIN"
echo ""
echo "Installiere Pakete..."
export DEBIAN_FRONTEND=noninteractive
echo "postfix postfix/main_mailer_type string Internet Site" | debconf-set-selections
echo "postfix postfix/mailname string $(hostname -f)" | debconf-set-selections
apt-get update -qq
apt-get install -y -qq postfix python3 python3-pip
pip3 install -q httpx 2>/dev/null || pip3 install --break-system-packages -q httpx

echo "Konfiguriere Postfix..."
cp /etc/postfix/main.cf /etc/postfix/main.cf.bak 2>/dev/null || true
cat > /etc/postfix/main.cf << MAINCF
smtpd_banner = \$myhostname ESMTP
biff = no
append_dot_mydomain = no
readme_directory = no
myhostname = $(hostname -f)
myorigin = \$myhostname
inet_interfaces = all
inet_protocols = all
mydestination = localhost
virtual_mailbox_domains = __ALIAS_DOMAIN__
virtual_transport = emailrelay
virtual_mailbox_maps = regexp:/etc/postfix/virtual_mailbox_regex
smtpd_tls_security_level = may
smtp_tls_security_level = may
message_size_limit = 52428800
MAINCF

printf '/@%s$/  OK\n' "$ALIAS_DOMAIN" > /etc/postfix/virtual_mailbox_regex

if ! grep -q "^emailrelay" /etc/postfix/master.cf; then
  printf '\n# EmailRelay forwarder\nemailrelay unix  -       n       n       -       -       pipe\n  flags=Rq user=nobody argv=/usr/local/bin/emailrelay-forward.py ${recipient}\n' >> /etc/postfix/master.cf
fi

echo "Installiere Forward-Script..."
cat > /usr/local/bin/emailrelay-forward.py << 'PYEOF'
#!/usr/bin/env python3
"""Postfix pipe: Leitet eingehende Mails an echte Adressen weiter."""
import sys
import httpx
import smtplib
from email.parser import BytesParser
from email import policy

API_URL = "__API_URL__"
API_SECRET = "__API_SECRET__"
ALIAS_DOMAIN = "__ALIAS_DOMAIN__"

def resolve_alias(alias_address):
    resp = httpx.get(
        f"{API_URL}/api/alias/incoming/{alias_address}",
        headers={"x-api-secret": API_SECRET},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["real_address"]

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    alias_address = sys.argv[1].lower()
    raw = sys.stdin.buffer.read()
    try:
        real_address = resolve_alias(alias_address)
    except Exception as e:
        print(f"API-Fehler: {e}", file=sys.stderr)
        sys.exit(75)
    if not real_address:
        print(f"Alias {alias_address} nicht gefunden", file=sys.stderr)
        sys.exit(67)
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    del msg["To"]
    msg["To"] = real_address
    with smtplib.SMTP("localhost", 25) as smtp:
        smtp.sendmail(f"noreply@{ALIAS_DOMAIN}", [real_address], msg.as_bytes())
    print(f"Weitergeleitet: {alias_address} -> {real_address}")

if __name__ == "__main__":
    main()
PYEOF

chmod +x /usr/local/bin/emailrelay-forward.py
postmap /etc/postfix/virtual_mailbox_regex 2>/dev/null || true
systemctl restart postfix 2>/dev/null || postfix reload || true
echo ""
echo "=== Setup abgeschlossen ==="
"""

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="templates")


async def get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    result = await db.execute(select(Setting).where(Setting.key == key))
    s = result.scalar_one_or_none()
    return s.value if s else default


async def save_setting(db: AsyncSession, key: str, value: str):
    result = await db.execute(select(Setting).where(Setting.key == key))
    s = result.scalar_one_or_none()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    alias_count = (await db.execute(select(Alias))).scalars().all()
    domain_count = (await db.execute(select(Domain))).scalars().all()
    address_count = (await db.execute(select(EmailAddress))).scalars().all()
    recent_aliases = (
        await db.execute(select(Alias).order_by(Alias.created_at.desc()).limit(10))
    ).scalars().all()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "alias_count": len(alias_count),
        "domain_count": len(domain_count),
        "address_count": len(address_count),
        "recent_aliases": recent_aliases,
    })


# ── Einstellungen ──────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_use_tls",
            "alias_domain", "vps_host", "vps_port", "vps_user", "vps_ssh_key"]
    cfg = {k: await get_setting(db, k) for k in keys}
    return templates.TemplateResponse("settings.html", {"request": request, "cfg": cfg})


@router.post("/settings")
async def settings_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form("true"),
    alias_domain: str = Form(""),
    vps_host: str = Form(""),
    vps_port: str = Form("22"),
    vps_user: str = Form("root"),
    vps_ssh_key: str = Form(""),
):
    pairs = {
        "smtp_host": smtp_host, "smtp_port": smtp_port,
        "smtp_user": smtp_user, "smtp_password": smtp_password,
        "smtp_use_tls": smtp_use_tls, "alias_domain": alias_domain,
        "vps_host": vps_host, "vps_port": vps_port,
        "vps_user": vps_user, "vps_ssh_key": vps_ssh_key,
    }
    for k, v in pairs.items():
        await save_setting(db, k, v)
    await db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


# ── Domains ────────────────────────────────────────────────────────────────────

@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request, db: AsyncSession = Depends(get_db)):
    domains = (await db.execute(select(Domain).order_by(Domain.created_at.desc()))).scalars().all()
    return templates.TemplateResponse("domains.html", {"request": request, "domains": domains})


@router.post("/domains")
async def domain_add(
    db: AsyncSession = Depends(get_db),
    domain: str = Form(...),
    alias_domain: str = Form(""),
):
    domain = domain.strip().lower()
    existing = (await db.execute(select(Domain).where(Domain.domain == domain))).scalar_one_or_none()
    if not existing:
        db.add(Domain(domain=domain, alias_domain=alias_domain.strip().lower() or None))
        await db.commit()
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/{domain_id}/delete")
async def domain_delete(domain_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Domain).where(Domain.id == domain_id))
    await db.commit()
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/{domain_id}/toggle")
async def domain_toggle(domain_id: int, db: AsyncSession = Depends(get_db)):
    d = (await db.execute(select(Domain).where(Domain.id == domain_id))).scalar_one_or_none()
    if d:
        d.active = not d.active
        await db.commit()
    return RedirectResponse("/domains", status_code=303)


# ── E-Mail-Adressen ────────────────────────────────────────────────────────────

@router.get("/addresses", response_class=HTMLResponse)
async def addresses_page(request: Request, db: AsyncSession = Depends(get_db)):
    addresses = (
        await db.execute(select(EmailAddress).order_by(EmailAddress.created_at.desc()))
    ).scalars().all()
    domains = (await db.execute(select(Domain).where(Domain.active == True))).scalars().all()
    return templates.TemplateResponse("addresses.html", {
        "request": request, "addresses": addresses, "domains": domains
    })


@router.post("/addresses")
async def address_add(
    db: AsyncSession = Depends(get_db),
    address: str = Form(...),
    domain_id: int = Form(...),
):
    address = address.strip().lower()
    existing = (await db.execute(select(EmailAddress).where(EmailAddress.address == address))).scalar_one_or_none()
    if not existing:
        db.add(EmailAddress(address=address, domain_id=domain_id))
        await db.commit()
    return RedirectResponse("/addresses", status_code=303)


@router.post("/addresses/{addr_id}/delete")
async def address_delete(addr_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(EmailAddress).where(EmailAddress.id == addr_id))
    await db.commit()
    return RedirectResponse("/addresses", status_code=303)


@router.post("/addresses/{addr_id}/toggle")
async def address_toggle(addr_id: int, db: AsyncSession = Depends(get_db)):
    a = (await db.execute(select(EmailAddress).where(EmailAddress.id == addr_id))).scalar_one_or_none()
    if a:
        a.active = not a.active
        await db.commit()
    return RedirectResponse("/addresses", status_code=303)


# ── Aliases ────────────────────────────────────────────────────────────────────

@router.get("/aliases", response_class=HTMLResponse)
async def aliases_page(request: Request, db: AsyncSession = Depends(get_db)):
    aliases = (
        await db.execute(select(Alias).order_by(Alias.created_at.desc()))
    ).scalars().all()
    return templates.TemplateResponse("aliases.html", {"request": request, "aliases": aliases})


@router.post("/aliases/{alias_id}/toggle")
async def alias_toggle(alias_id: int, db: AsyncSession = Depends(get_db)):
    a = (await db.execute(select(Alias).where(Alias.id == alias_id))).scalar_one_or_none()
    if a:
        a.active = not a.active
        await db.commit()
    return RedirectResponse("/aliases", status_code=303)


@router.post("/aliases/{alias_id}/delete")
async def alias_delete(alias_id: int, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Alias).where(Alias.id == alias_id))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)
