"""UI-Routen für das Web-Interface."""
import asyncio
import io
import os
import secrets
import string
import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload

from database import get_db
from models import Setting, Domain, EmailAddress, Alias, AliasDomainConfig

_VPS_SETUP_SCRIPT = r'''#!/bin/bash
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
smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination
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

if len(sys.argv) < 2:
    sys.exit(1)

alias_address = sys.argv[1].lower()
raw = sys.stdin.buffer.read()
headers = {"x-api-secret": API_SECRET}

try:
    resp = httpx.get(f"{API_URL}/api/alias/incoming/{alias_address}",
                     headers=headers, timeout=10)
    if resp.status_code == 404:
        sys.exit(67)
    real_address = resp.json()["real_address"]
except Exception as e:
    print(f"API-Fehler: {e}", file=sys.stderr)
    sys.exit(75)

try:
    cfg = httpx.get(f"{API_URL}/api/smtp-config/{real_address}",
                    headers=headers, timeout=10).json()
except Exception as e:
    print(f"SMTP-Config-Fehler: {e}", file=sys.stderr)
    sys.exit(75)

msg = BytesParser(policy=policy.default).parsebytes(raw)

original_from = msg.get("From", "")
if original_from and not msg.get("Reply-To"):
    del msg["Reply-To"]
    msg["Reply-To"] = original_from
del msg["From"]
msg["From"] = cfg.get("smtp_user", real_address)
del msg["To"]
msg["To"] = real_address

try:
    use_tls = cfg.get("smtp_use_tls", "true") != "false"
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587))) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(cfg["smtp_user"], cfg["smtp_password"])
        smtp.sendmail(cfg["smtp_user"], [real_address], msg.as_bytes(policy=policy.SMTP))
except Exception as e:
    print(f"SMTP-Fehler: {e}", file=sys.stderr)
    sys.exit(75)
PYEOF

chmod +x /usr/local/bin/emailrelay-forward.py
postmap /etc/postfix/virtual_mailbox_regex 2>/dev/null || true
systemctl restart postfix 2>/dev/null || postfix reload || true
echo ""
echo "=== Setup abgeschlossen ==="
'''

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


# ── Auth ───────────────────────────────────────────────────────────────────────

def _redirect_if_not_logged_in(request: Request):
    """Gibt RedirectResponse zurück wenn nicht eingeloggt, sonst None."""
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=302)
    return None


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=302)
    has_password = bool(await get_setting(db, "ui_password_hash"))
    return templates.TemplateResponse("login.html", {
        "request": request, "has_password": has_password,
    })


@router.post("/login")
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    password: str = Form(...),
):
    stored_hash = await get_setting(db, "ui_password_hash")
    if not stored_hash:
        # Erstes Login: Passwort direkt setzen
        hashed = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
        await save_setting(db, "ui_password_hash", hashed)
        await db.commit()
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    if _bcrypt.checkpw(password.encode(), stored_hash.encode()):
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=302)
    has_password = bool(stored_hash)
    return templates.TemplateResponse("login.html", {
        "request": request, "error": "Falsches Passwort", "has_password": has_password,
    })


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
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
    if r := _redirect_if_not_logged_in(request): return r
    return templates.TemplateResponse("settings.html", {"request": request})


@router.post("/settings")
async def settings_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    if r := _redirect_if_not_logged_in(request): return r
    error = None
    success = None

    if new_password:
        stored_hash = await get_setting(db, "ui_password_hash")
        if stored_hash and not _bcrypt.checkpw(old_password.encode(), stored_hash.encode()):
            error = "Aktuelles Passwort ist falsch."
        elif new_password != new_password2:
            error = "Neues Passwort und Bestätigung stimmen nicht überein."
        else:
            hashed = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
            await save_setting(db, "ui_password_hash", hashed)
            await db.commit()
            success = "Passwort wurde geändert."
    else:
        error = "Bitte neues Passwort eingeben."

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "error": error,
        "success": success,
    })


# ── Alias-Domains ──────────────────────────────────────────────────────────────

@router.get("/alias-domains", response_class=HTMLResponse)
async def alias_domains_page(request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    configs = (await db.execute(
        select(AliasDomainConfig).order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("alias_domains.html", {
        "request": request, "configs": configs,
    })


@router.post("/alias-domains")
async def alias_domain_add(
    request: Request,
    db: AsyncSession = Depends(get_db),
    label: str = Form(""),
    alias_domain: str = Form(...),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form("true"),
    vps_host: str = Form(""),
    vps_port: str = Form("22"),
    vps_user: str = Form("root"),
    vps_ssh_key: str = Form(""),
    api_url_for_vps: str = Form(""),
    catchall_enabled: str = Form("false"),
    catchall_target_address: str = Form(""),
):
    if r := _redirect_if_not_logged_in(request): return r
    alias_domain = alias_domain.strip().lower()
    existing = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.alias_domain == alias_domain)
    )).scalar_one_or_none()
    if not existing:
        db.add(AliasDomainConfig(
            label=label.strip(),
            alias_domain=alias_domain,
            smtp_host=smtp_host.strip(),
            smtp_port=int(smtp_port or 587),
            smtp_user=smtp_user.strip(),
            smtp_password=smtp_password,
            smtp_use_tls=smtp_use_tls != "false",
            vps_host=vps_host.strip(),
            vps_port=int(vps_port or 22),
            vps_user=vps_user.strip() or "root",
            vps_ssh_key=vps_ssh_key,
            api_url_for_vps=api_url_for_vps.strip(),
            catchall_enabled=catchall_enabled == "true",
            catchall_target_address=catchall_target_address.strip().lower(),
        ))
        await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.get("/alias-domains/{config_id}/edit", response_class=HTMLResponse)
async def alias_domain_edit_page(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    if not cfg:
        return RedirectResponse("/alias-domains", status_code=302)
    return templates.TemplateResponse("alias_domain_edit.html", {
        "request": request, "cfg": cfg,
    })


@router.post("/alias-domains/{config_id}/edit")
async def alias_domain_edit_save(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    label: str = Form(""),
    alias_domain: str = Form(...),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_use_tls: str = Form("true"),
    vps_host: str = Form(""),
    vps_port: str = Form("22"),
    vps_user: str = Form("root"),
    vps_ssh_key: str = Form(""),
    api_url_for_vps: str = Form(""),
):
    if r := _redirect_if_not_logged_in(request): return r
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    if cfg:
        cfg.label = label.strip()
        cfg.alias_domain = alias_domain.strip().lower()
        cfg.smtp_host = smtp_host.strip()
        cfg.smtp_port = int(smtp_port or 587)
        cfg.smtp_user = smtp_user.strip()
        cfg.smtp_password = smtp_password
        cfg.smtp_use_tls = smtp_use_tls != "false"
        cfg.vps_host = vps_host.strip()
        cfg.vps_port = int(vps_port or 22)
        cfg.vps_user = vps_user.strip() or "root"
        cfg.vps_ssh_key = vps_ssh_key
        cfg.api_url_for_vps = api_url_for_vps.strip()
        await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/delete")
async def alias_domain_delete(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    await db.execute(delete(AliasDomainConfig).where(AliasDomainConfig.id == config_id))
    await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/toggle")
async def alias_domain_toggle(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    if cfg:
        cfg.active = not cfg.active
        await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/test", response_class=HTMLResponse)
async def alias_domain_test(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    all_configs = (await db.execute(
        select(AliasDomainConfig).order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()
    test_error = None
    test_success = None
    if not cfg:
        test_error = "Konfiguration nicht gefunden"
    else:
        try:
            import aiosmtplib
            smtp = aiosmtplib.SMTP(hostname=cfg.smtp_host, port=cfg.smtp_port, start_tls=cfg.smtp_use_tls)
            await smtp.connect()
            await smtp.login(cfg.smtp_user, cfg.smtp_password)
            await smtp.quit()
            test_success = f"Verbindung zu {cfg.smtp_host}:{cfg.smtp_port} erfolgreich"
        except Exception as e:
            test_error = f"Verbindung fehlgeschlagen: {e}"
    return templates.TemplateResponse("alias_domains.html", {
        "request": request,
        "configs": all_configs,
        "test_success": test_success,
        "test_error": test_error,
        "tested_id": config_id,
    })


@router.post("/alias-domains/{config_id}/vps-setup", response_class=HTMLResponse)
async def alias_domain_vps_setup(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    import paramiko

    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    all_configs = (await db.execute(
        select(AliasDomainConfig).order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()

    if not cfg:
        return templates.TemplateResponse("alias_domains.html", {
            "request": request, "configs": all_configs,
            "setup_error": "Konfiguration nicht gefunden", "setup_id": config_id,
        })

    api_secret = os.getenv("API_SECRET", "")
    missing = [n for n, v in [
        ("VPS-Host", cfg.vps_host), ("SSH-Key", cfg.vps_ssh_key),
        ("Alias-Domain", cfg.alias_domain), ("API-URL für VPS", cfg.api_url_for_vps),
    ] if not v]
    if missing:
        return templates.TemplateResponse("alias_domains.html", {
            "request": request, "configs": all_configs,
            "setup_error": f"Fehlende Felder: {', '.join(missing)}", "setup_id": config_id,
        })

    script = (
        _VPS_SETUP_SCRIPT
        .replace("__ALIAS_DOMAIN__", cfg.alias_domain)
        .replace("__API_URL__", cfg.api_url_for_vps)
        .replace("__API_SECRET__", api_secret)
    )

    def _run_ssh() -> str:
        key = None
        last_err = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                key = cls.from_private_key(io.StringIO(cfg.vps_ssh_key))
                break
            except Exception as e:
                last_err = e
        if key is None:
            raise ValueError(f"SSH-Key konnte nicht geladen werden: {last_err}")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(cfg.vps_host, port=cfg.vps_port, username=cfg.vps_user, pkey=key, timeout=15)
        try:
            sftp = client.open_sftp()
            with sftp.open("/tmp/_emailrelay_setup.sh", "w") as f:
                f.write(script)
            sftp.close()
            _, stdout, stderr = client.exec_command("bash /tmp/_emailrelay_setup.sh; rm -f /tmp/_emailrelay_setup.sh")
            output = stdout.read().decode(errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace")
            if exit_code != 0:
                raise RuntimeError(f"Exit {exit_code}:\n{err or output}")
            return output
        finally:
            client.close()

    setup_log = None
    setup_error = None
    try:
        setup_log = await asyncio.get_event_loop().run_in_executor(None, _run_ssh)
    except Exception as e:
        setup_error = str(e)

    return templates.TemplateResponse("alias_domains.html", {
        "request": request,
        "configs": all_configs,
        "setup_log": setup_log,
        "setup_error": setup_error,
        "setup_id": config_id,
    })


# ── Domains ────────────────────────────────────────────────────────────────────

@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request, db: AsyncSession = Depends(get_db)):
    if r := _redirect_if_not_logged_in(request): return r
    domains = (await db.execute(
        select(Domain)
        .options(selectinload(Domain.alias_domain_config))
        .order_by(Domain.created_at.desc())
    )).scalars().all()
    alias_configs = (await db.execute(
        select(AliasDomainConfig)
        .where(AliasDomainConfig.active == True)
        .order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()
    return templates.TemplateResponse("domains.html", {
        "request": request,
        "domains": domains,
        "alias_configs": alias_configs,
    })


@router.post("/domains")
async def domain_add(
    request: Request,
    db: AsyncSession = Depends(get_db),
    domain: str = Form(...),
    alias_domain_config_id: str = Form(""),
):
    if r := _redirect_if_not_logged_in(request): return r
    domain = domain.strip().lower()
    config_id = int(alias_domain_config_id) if alias_domain_config_id.strip() else None
    existing = (await db.execute(select(Domain).where(Domain.domain == domain))).scalar_one_or_none()
    if not existing:
        db.add(Domain(domain=domain, alias_domain_config_id=config_id))
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
    if r := _redirect_if_not_logged_in(request): return r
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
    if r := _redirect_if_not_logged_in(request): return r
    aliases = (
        await db.execute(select(Alias).order_by(Alias.created_at.desc()))
    ).scalars().all()
    email_addresses = (
        await db.execute(select(EmailAddress).where(EmailAddress.active == True).order_by(EmailAddress.address))
    ).scalars().all()
    return templates.TemplateResponse("aliases.html", {
        "request": request,
        "aliases": aliases,
        "email_addresses": email_addresses,
    })


@router.post("/aliases/create")
async def alias_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    real_address: str = Form(...),
    label: str = Form(""),
):
    if r := _redirect_if_not_logged_in(request): return r

    email_addr = (await db.execute(
        select(EmailAddress)
        .options(selectinload(EmailAddress.domain).selectinload(Domain.alias_domain_config))
        .where(EmailAddress.address == real_address, EmailAddress.active == True)
    )).scalar_one_or_none()
    if not email_addr:
        return RedirectResponse("/aliases", status_code=303)

    alias_domain = None
    if email_addr.domain and email_addr.domain.alias_domain_config and email_addr.domain.alias_domain_config.active:
        alias_domain = email_addr.domain.alias_domain_config.alias_domain
    if not alias_domain:
        result = await db.execute(select(Setting).where(Setting.key == "alias_domain"))
        s = result.scalar_one_or_none()
        alias_domain = s.value if s else None
    if not alias_domain:
        return RedirectResponse("/aliases", status_code=303)

    chars = string.ascii_lowercase + string.digits
    for _ in range(10):
        local = "".join(secrets.choice(chars) for _ in range(10))
        candidate = f"{local}@{alias_domain}"
        existing = (await db.execute(select(Alias).where(Alias.alias_address == candidate))).scalar_one_or_none()
        if not existing:
            break
    else:
        return RedirectResponse("/aliases", status_code=303)

    db.add(Alias(alias_address=candidate, real_address=real_address, label=label.strip()))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)


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


@router.post("/aliases/{alias_id}/rotate")
async def alias_rotate(alias_id: int, db: AsyncSession = Depends(get_db)):
    """Deaktiviert den aktuellen Alias und erstellt einen neuen für dieselbe Adresse."""
    a = (await db.execute(
        select(Alias).where(Alias.id == alias_id, Alias.active == True)
    )).scalar_one_or_none()
    if not a:
        return RedirectResponse("/aliases", status_code=303)

    real_address = a.real_address

    # Alias-Domain ermitteln (über EmailAddress → Domain → AliasDomainConfig)
    email_addr = (await db.execute(
        select(EmailAddress)
        .options(selectinload(EmailAddress.domain).selectinload(Domain.alias_domain_config))
        .where(EmailAddress.address == real_address, EmailAddress.active == True)
    )).scalar_one_or_none()

    alias_domain = None
    if email_addr and email_addr.domain and email_addr.domain.alias_domain_config:
        alias_domain = email_addr.domain.alias_domain_config.alias_domain
    if not alias_domain:
        result = await db.execute(select(Setting).where(Setting.key == "alias_domain"))
        s = result.scalar_one_or_none()
        alias_domain = s.value if s else None
    if not alias_domain:
        return RedirectResponse("/aliases", status_code=303)

    # Neuen eindeutigen Alias generieren
    chars = string.ascii_lowercase + string.digits
    for _ in range(10):
        local = "".join(secrets.choice(chars) for _ in range(10))
        candidate = f"{local}@{alias_domain}"
        existing = (await db.execute(select(Alias).where(Alias.alias_address == candidate))).scalar_one_or_none()
        if not existing:
            break
    else:
        return RedirectResponse("/aliases", status_code=303)

    # Alten deaktivieren, neuen anlegen
    a.active = False
    db.add(Alias(alias_address=candidate, real_address=real_address))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)


# ── Hilfe ──────────────────────────────────────────────────────────────────────

@router.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request):
    if r := _redirect_if_not_logged_in(request): return r
    return templates.TemplateResponse("guide.html", {"request": request})
