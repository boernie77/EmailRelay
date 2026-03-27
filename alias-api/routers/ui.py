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
from models import Setting, Domain, EmailAddress, Alias, AliasDomainConfig, AliasDomainAccess, VpsConfig, User

_VPS_SETUP_SCRIPT = r'''#!/bin/bash
set -e
API_URL='__API_URL__'
API_SECRET='__API_SECRET__'

echo "=== E-Mail Relay VPS Setup ==="
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
virtual_mailbox_domains = __ALIAS_DOMAINS_POSTFIX__
virtual_transport = emailrelay
virtual_mailbox_maps = regexp:/etc/postfix/virtual_mailbox_regex
smtpd_relay_restrictions = permit_mynetworks, reject_unauth_destination
smtpd_tls_security_level = may
smtp_tls_security_level = may
message_size_limit = 52428800
MAINCF

cat > /etc/postfix/virtual_mailbox_regex << 'REGEXEOF'
__ALIAS_DOMAINS_REGEX__
REGEXEOF

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
    if resp.status_code == 410:
        sys.exit(0)   # blockiert: still verwerfen
    if resp.status_code != 200:
        sys.exit(67)  # nicht gefunden: bounce (User unknown)
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

PRIVACY_POLICY_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>E-Mail Relay – Datenschutzerklärung</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 720px; margin: 40px auto; padding: 0 24px; color: #1f2937; line-height: 1.7; }
    h1 { font-size: 1.6rem; margin-bottom: 4px; }
    h2 { font-size: 1.1rem; margin-top: 2rem; margin-bottom: 4px; }
    .sub { color: #6b7280; font-size: 0.9rem; margin-bottom: 2rem; }
    p, li { font-size: 0.95rem; color: #374151; }
    ul { padding-left: 1.4rem; }
  </style>
</head>
<body>
  <h1>Datenschutzerklärung</h1>
  <p class="sub">E-Mail Relay Chrome Extension &mdash; Stand: März 2026</p>

  <h2>1. Verantwortlicher</h2>
  <p>Diese Chrome Extension ist ein selbst gehostetes, privates Werkzeug. Sie kommuniziert ausschließlich mit dem eigenen E-Mail-Relay-Server des Betreibers.</p>

  <h2>2. Welche Daten werden gespeichert?</h2>
  <p>Die Extension speichert folgende Daten <strong>ausschließlich lokal im Browser</strong> (chrome.storage.sync, d.h. im Google-Konto des Nutzers):</p>
  <ul>
    <li>API-URL des eigenen Servers</li>
    <li>API-Secret (Zugriffstoken)</li>
    <li>Optionaler Benutzername und Passwort für den eigenen Server</li>
  </ul>
  <p>Diese Daten werden <strong>nicht</strong> an Dritte weitergegeben und nicht auf fremden Servern gespeichert.</p>

  <h2>3. Welche Daten werden übertragen?</h2>
  <p>Die Extension sendet Anfragen ausschließlich an die vom Nutzer selbst konfigurierte API-URL (eigener Server). Dabei werden übertragen:</p>
  <ul>
    <li>API-Secret als HTTP-Header zur Authentifizierung</li>
    <li>Optionaler Benutzername und Passwort als HTTP-Header</li>
    <li>Beim Erstellen eines Alias: die gewählte echte E-Mail-Adresse und eine Bezeichnung</li>
  </ul>
  <p>Es findet <strong>keine Kommunikation mit Drittservern</strong> statt. Keine Tracking-, Analyse- oder Werbedienste.</p>

  <h2>4. Zugriffsrechte</h2>
  <ul>
    <li><strong>storage</strong>: Lokales Speichern der Einstellungen</li>
    <li><strong>activeTab / tabs</strong>: Ermitteln der aktuellen Domain als Bezeichnungsvorschlag</li>
    <li><strong>scripting</strong>: Einsetzen des erstellten Alias in das zuletzt fokussierte Eingabefeld</li>
    <li><strong>host_permissions (alle URLs)</strong>: Notwendig, um Content Scripts auf beliebigen Seiten auszuführen (für die Autofill-Funktion)</li>
  </ul>

  <h2>5. Datenweitergabe</h2>
  <p>Keine. Die Extension gibt keine Daten an Dritte weiter.</p>

  <h2>6. Kontakt</h2>
  <p>Bei Fragen: <a href="https://byboernie.de">byboernie.de</a></p>
</body>
</html>"""

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="templates")


# ── Auto-VPS-Setup ─────────────────────────────────────────────────────────────

async def _auto_vps_setup(vps_id: int):
    """Führt VPS-Setup automatisch im Hintergrund aus wenn eine Alias-Domain geändert wird."""
    import paramiko
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        vps = (await db.execute(
            select(VpsConfig)
            .options(selectinload(VpsConfig.alias_domain_configs))
            .where(VpsConfig.id == vps_id)
        )).scalar_one_or_none()
        if not vps or not vps.host or not vps.ssh_key or not vps.api_url:
            return
        alias_domains = [
            cfg.alias_domain for cfg in vps.alias_domain_configs if cfg.active and cfg.alias_domain
        ]
        if not alias_domains:
            return
        api_secret = os.getenv("API_SECRET", "")
        domains_postfix = ", ".join(alias_domains)
        domains_regex = "\n".join("/@" + d.replace(".", r"\.") + "$/  OK" for d in alias_domains)
        script = (
            _VPS_SETUP_SCRIPT
            .replace("__ALIAS_DOMAINS_POSTFIX__", domains_postfix)
            .replace("__ALIAS_DOMAINS_REGEX__", domains_regex)
            .replace("__API_URL__", vps.api_url)
            .replace("__API_SECRET__", api_secret)
        )

        def _run_ssh():
            key = None
            for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
                try:
                    key = cls.from_private_key(io.StringIO(vps.ssh_key))
                    break
                except Exception:
                    pass
            if key is None:
                return
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(vps.host, port=vps.port, username=vps.user, pkey=key, timeout=15)
            try:
                sftp = client.open_sftp()
                with sftp.open("/tmp/_emailrelay_setup.sh", "w") as f:
                    f.write(script)
                sftp.close()
                _, stdout, _ = client.exec_command(
                    "bash /tmp/_emailrelay_setup.sh; rm -f /tmp/_emailrelay_setup.sh"
                )
                stdout.channel.recv_exit_status()
            finally:
                client.close()

        try:
            await asyncio.get_event_loop().run_in_executor(None, _run_ssh)
        except Exception:
            pass


# ── Auth-Helpers ───────────────────────────────────────────────────────────────

async def get_current_user(request: Request, db: AsyncSession) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return (await db.execute(
        select(User).where(User.id == user_id, User.active == True)
    )).scalar_one_or_none()


def redirect_login():
    return RedirectResponse("/login", status_code=302)


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


async def get_user_alias_configs(db: AsyncSession, user: User) -> list[AliasDomainConfig]:
    """Gibt die für einen User freigegebenen Alias-Domain-Configs zurück."""
    result = await db.execute(
        select(AliasDomainConfig)
        .join(AliasDomainAccess, AliasDomainAccess.alias_domain_config_id == AliasDomainConfig.id)
        .where(AliasDomainAccess.user_id == user.id, AliasDomainConfig.active == True)
        .order_by(AliasDomainConfig.created_at.desc())
    )
    return result.scalars().all()


# ── Auth ───────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    has_users = bool((await db.execute(select(User))).scalars().first())
    is_upgrade = not has_users and bool(await get_setting(db, "ui_password_hash"))
    registration_enabled = await get_setting(db, "registration_enabled", "false") == "true"
    return templates.TemplateResponse("login.html", {
        "request": request, "has_users": has_users, "is_upgrade": is_upgrade,
        "registration_enabled": registration_enabled,
    })


@router.post("/login")
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
):
    has_users = bool((await db.execute(select(User))).scalars().first())
    is_upgrade = not has_users and bool(await get_setting(db, "ui_password_hash"))

    if not has_users:
        # Beim Upgrade: Passwort gegen alten Hash prüfen
        stored_hash = await get_setting(db, "ui_password_hash")
        if stored_hash and not _bcrypt.checkpw(password.encode(), stored_hash.encode()):
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Falsches Passwort",
                "has_users": False, "is_upgrade": is_upgrade,
            })
        pw_hash = stored_hash if stored_hash else _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
        admin = User(username=username.strip(), password_hash=pw_hash, is_admin=True)
        db.add(admin)
        await db.flush()
        for cfg in (await db.execute(select(AliasDomainConfig))).scalars().all():
            db.add(AliasDomainAccess(user_id=admin.id, alias_domain_config_id=cfg.id))
        for d in (await db.execute(select(Domain).where(Domain.user_id == None))).scalars().all():
            d.user_id = admin.id
        for a in (await db.execute(select(Alias).where(Alias.user_id == None))).scalars().all():
            a.user_id = admin.id
        await db.commit()
        request.session["user_id"] = admin.id
        request.session["is_admin"] = True
        return RedirectResponse("/", status_code=302)

    user = (await db.execute(
        select(User).where(User.username == username.strip(), User.active == True)
    )).scalar_one_or_none()

    if not user or not _bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Falscher Benutzername oder Passwort",
            "has_users": has_users, "is_upgrade": False,
        })

    request.session["user_id"] = user.id
    request.session["is_admin"] = user.is_admin
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    alias_count = (await db.execute(select(Alias).where(Alias.user_id == user.id))).scalars().all()
    domain_count = (await db.execute(select(Domain).where(Domain.user_id == user.id))).scalars().all()
    address_count = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .where(Domain.user_id == user.id)
    )).scalars().all()
    recent_aliases = (await db.execute(
        select(Alias).where(Alias.user_id == user.id).order_by(Alias.created_at.desc()).limit(10)
    )).scalars().all()

    # VPS-Warnung: letzte 403 neuer als letzter Erfolg?
    from datetime import datetime, timezone
    vps_warning = False
    if user.is_admin:
        s403 = (await db.execute(select(Setting).where(Setting.key == "last_vps_403"))).scalar_one_or_none()
        sok = (await db.execute(select(Setting).where(Setting.key == "last_vps_ok"))).scalar_one_or_none()
        if s403:
            t403 = datetime.fromisoformat(s403.value)
            tok = datetime.fromisoformat(sok.value) if sok else None
            vps_warning = tok is None or t403 > tok

    # Nicht-Admins ohne E-Mail-Adressen zum Setup-Wizard leiten
    if not user.is_admin and not address_count and not request.session.get("setup_skipped"):
        alias_configs = await get_user_alias_configs(db, user)
        if alias_configs:
            return RedirectResponse("/setup", status_code=302)

    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_user": user,
        "alias_count": len(alias_count),
        "domain_count": len(domain_count),
        "address_count": len(address_count),
        "recent_aliases": recent_aliases,
        "vps_warning": vps_warning,
    })


# ── Einstellungen ──────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    ntfy_setting = (await db.execute(select(Setting).where(Setting.key == "ntfy_url"))).scalar_one_or_none()
    ntfy_url = ntfy_setting.value if ntfy_setting else ""
    random_suffix = secrets.token_hex(4)
    saved = request.query_params.get("saved") == "1"
    system_smtp = {}
    if user.is_admin:
        for key in ["system_smtp_host", "system_smtp_port", "system_smtp_user",
                    "system_smtp_from", "system_smtp_use_tls", "registration_enabled"]:
            system_smtp[key] = await get_setting(db, key)
        system_smtp["has_password"] = bool(await get_setting(db, "system_smtp_password"))
    return templates.TemplateResponse("settings.html", {
        "request": request, "current_user": user,
        "ntfy_url": ntfy_url, "random_suffix": random_suffix,
        "system_smtp": system_smtp, "saved": saved,
    })


@router.post("/settings")
async def settings_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    error = None
    success = None

    if new_password:
        if not _bcrypt.checkpw(old_password.encode(), user.password_hash.encode()):
            error = "Aktuelles Passwort ist falsch."
        elif new_password != new_password2:
            error = "Neues Passwort und Bestätigung stimmen nicht überein."
        else:
            user.password_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
            await db.commit()
            success = "Passwort wurde geändert."
    else:
        error = "Bitte neues Passwort eingeben."

    ntfy_setting = (await db.execute(select(Setting).where(Setting.key == "ntfy_url"))).scalar_one_or_none()
    return templates.TemplateResponse("settings.html", {
        "request": request, "current_user": user, "error": error, "success": success,
        "ntfy_url": ntfy_setting.value if ntfy_setting else "",
        "random_suffix": secrets.token_hex(4),
    })


@router.post("/settings/ntfy", response_class=HTMLResponse)
async def settings_save_ntfy(
    request: Request,
    db: AsyncSession = Depends(get_db),
    ntfy_url: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    if not user.is_admin:
        return RedirectResponse("/settings", status_code=302)
    ntfy_url = ntfy_url.strip()
    existing = (await db.execute(select(Setting).where(Setting.key == "ntfy_url"))).scalar_one_or_none()
    if existing:
        existing.value = ntfy_url
    else:
        db.add(Setting(key="ntfy_url", value=ntfy_url))
    await db.commit()
    return templates.TemplateResponse("settings.html", {
        "request": request, "current_user": user,
        "success": "ntfy-URL gespeichert.",
        "ntfy_url": ntfy_url, "random_suffix": secrets.token_hex(4),
    })


@router.post("/settings/test-ntfy")
async def settings_test_ntfy(request: Request, db: AsyncSession = Depends(get_db)):
    import httpx
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        from fastapi import HTTPException
        raise HTTPException(status_code=403)
    ntfy_setting = (await db.execute(select(Setting).where(Setting.key == "ntfy_url"))).scalar_one_or_none()
    ntfy_url = ntfy_setting.value if ntfy_setting and ntfy_setting.value else ""
    if not ntfy_url:
        return {"ok": False, "error": "Keine ntfy-URL konfiguriert"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(ntfy_url, content="Test-Benachrichtigung von E-Mail Relay ✓".encode(),
                              headers={"Title": "E-Mail Relay: Test", "Priority": "default"}, timeout=5)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── Admin: Benutzerverwaltung ──────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    users = (await db.execute(
        select(User).options(selectinload(User.alias_domain_access)).order_by(User.created_at)
    )).scalars().all()
    all_configs = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.active == True).order_by(AliasDomainConfig.created_at)
    )).scalars().all()
    return templates.TemplateResponse("admin_users.html", {
        "request": request, "current_user": user, "users": users, "all_configs": all_configs,
    })


@router.post("/admin/users/create")
async def admin_user_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form("false"),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    username = username.strip()
    existing = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not existing:
        pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
        db.add(User(username=username, password_hash=pw_hash, is_admin=(is_admin == "true")))
        await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{uid}/delete")
async def admin_user_delete(uid: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    if uid != user.id:  # Admin kann sich nicht selbst löschen
        await db.execute(delete(User).where(User.id == uid))
        await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{uid}/toggle")
async def admin_user_toggle(uid: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    if uid != user.id:
        target = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        if target:
            target.active = not target.active
            await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{uid}/set-password")
async def admin_user_set_password(
    uid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    new_password: str = Form(...),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    target = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if target:
        target.password_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
        await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{uid}/alias-access")
async def admin_user_alias_access(
    uid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    form = await request.form()
    selected_ids = {int(v) for k, v in form.multi_items() if k == "config_ids"}

    # Bestehende Zugänge löschen und neu setzen
    await db.execute(delete(AliasDomainAccess).where(AliasDomainAccess.user_id == uid))
    for config_id in selected_ids:
        db.add(AliasDomainAccess(user_id=uid, alias_domain_config_id=config_id))
    await db.commit()
    return RedirectResponse("/admin/users", status_code=303)


# ── VPS-Konfiguration (nur Admin) ─────────────────────────────────────────────

@router.get("/vps", response_class=HTMLResponse)
async def vps_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    vpss = (await db.execute(select(VpsConfig).order_by(VpsConfig.created_at))).scalars().all()
    return templates.TemplateResponse("vps_configs.html", {
        "request": request, "current_user": user, "vpss": vpss,
    })


@router.post("/vps")
async def vps_add(
    request: Request,
    db: AsyncSession = Depends(get_db),
    label: str = Form(""),
    host: str = Form(...),
    port: str = Form("22"),
    user_str: str = Form("root"),
    ssh_key: str = Form(""),
    api_url: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    db.add(VpsConfig(
        label=label.strip(),
        host=host.strip(),
        port=int(port or 22),
        user=user_str.strip() or "root",
        ssh_key=ssh_key,
        api_url=api_url.strip(),
    ))
    await db.commit()
    return RedirectResponse("/vps", status_code=303)


@router.get("/vps/{vps_id}/edit", response_class=HTMLResponse)
async def vps_edit_page(vps_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    vps = (await db.execute(select(VpsConfig).where(VpsConfig.id == vps_id))).scalar_one_or_none()
    if not vps:
        return RedirectResponse("/vps", status_code=302)
    return templates.TemplateResponse("vps_config_edit.html", {
        "request": request, "current_user": user, "vps": vps,
    })


@router.post("/vps/{vps_id}/edit")
async def vps_edit_save(
    vps_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    label: str = Form(""),
    host: str = Form(...),
    port: str = Form("22"),
    user_str: str = Form("root"),
    ssh_key: str = Form(""),
    api_url: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    vps = (await db.execute(select(VpsConfig).where(VpsConfig.id == vps_id))).scalar_one_or_none()
    if vps:
        vps.label = label.strip()
        vps.host = host.strip()
        vps.port = int(port or 22)
        vps.user = user_str.strip() or "root"
        vps.ssh_key = ssh_key
        vps.api_url = api_url.strip()
        await db.commit()
    return RedirectResponse("/vps", status_code=303)


@router.post("/vps/{vps_id}/delete")
async def vps_delete(vps_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    await db.execute(delete(VpsConfig).where(VpsConfig.id == vps_id))
    await db.commit()
    return RedirectResponse("/vps", status_code=303)


@router.post("/vps/{vps_id}/setup", response_class=HTMLResponse)
async def vps_setup(vps_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    import paramiko

    vps = (await db.execute(
        select(VpsConfig)
        .options(selectinload(VpsConfig.alias_domain_configs))
        .where(VpsConfig.id == vps_id)
    )).scalar_one_or_none()
    vpss = (await db.execute(select(VpsConfig).order_by(VpsConfig.created_at))).scalars().all()

    if not vps:
        return templates.TemplateResponse("vps_configs.html", {
            "request": request, "current_user": user, "vpss": vpss,
            "setup_error": "VPS-Konfiguration nicht gefunden", "setup_id": vps_id,
        })

    missing = [n for n, v in [
        ("Host", vps.host), ("SSH-Key", vps.ssh_key), ("API-URL", vps.api_url),
    ] if not v]
    if missing:
        return templates.TemplateResponse("vps_configs.html", {
            "request": request, "current_user": user, "vpss": vpss,
            "setup_error": f"Fehlende Felder: {', '.join(missing)}", "setup_id": vps_id,
        })

    # Alle aktiven Alias-Domains für diesen VPS sammeln
    alias_domains = [
        cfg.alias_domain for cfg in vps.alias_domain_configs if cfg.active and cfg.alias_domain
    ]
    if not alias_domains:
        return templates.TemplateResponse("vps_configs.html", {
            "request": request, "current_user": user, "vpss": vpss,
            "setup_error": "Keine aktiven Alias-Domains für diesen VPS konfiguriert.", "setup_id": vps_id,
        })

    api_secret = os.getenv("API_SECRET", "")
    # Postfix-Domains (kommagetrennt) und Regex-Einträge (eine Zeile pro Domain)
    domains_postfix = ", ".join(alias_domains)
    domains_regex = "\n".join("/@" + d.replace(".", r"\.") + "$/  OK" for d in alias_domains)

    script = (
        _VPS_SETUP_SCRIPT
        .replace("__ALIAS_DOMAINS_POSTFIX__", domains_postfix)
        .replace("__ALIAS_DOMAINS_REGEX__", domains_regex)
        .replace("__API_URL__", vps.api_url)
        .replace("__API_SECRET__", api_secret)
    )

    def _run_ssh() -> str:
        key = None
        last_err = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                key = cls.from_private_key(io.StringIO(vps.ssh_key))
                break
            except Exception as e:
                last_err = e
        if key is None:
            raise ValueError(f"SSH-Key konnte nicht geladen werden: {last_err}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(vps.host, port=vps.port, username=vps.user, pkey=key, timeout=15)
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

    return templates.TemplateResponse("vps_configs.html", {
        "request": request, "current_user": user, "vpss": vpss,
        "setup_log": setup_log, "setup_error": setup_error, "setup_id": vps_id,
    })


@router.post("/vps/{vps_id}/test", response_class=HTMLResponse)
async def vps_test(vps_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    import paramiko, re

    vps = (await db.execute(select(VpsConfig).where(VpsConfig.id == vps_id))).scalar_one_or_none()
    vpss = (await db.execute(select(VpsConfig).order_by(VpsConfig.created_at))).scalars().all()

    if not vps:
        return templates.TemplateResponse("vps_configs.html", {
            "request": request, "current_user": user, "vpss": vpss,
            "test_error": "VPS nicht gefunden", "test_id": vps_id,
        })

    if not vps.host or not vps.ssh_key:
        return templates.TemplateResponse("vps_configs.html", {
            "request": request, "current_user": user, "vpss": vpss,
            "test_error": "Host oder SSH-Key fehlt", "test_id": vps_id,
        })

    local_secret = os.getenv("API_SECRET", "")

    def _run_test() -> str:
        key = None
        last_err = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
            try:
                key = cls.from_private_key(io.StringIO(vps.ssh_key))
                break
            except Exception as e:
                last_err = e
        if key is None:
            raise ValueError(f"SSH-Key konnte nicht geladen werden: {last_err}")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(vps.host, port=vps.port, username=vps.user, pkey=key, timeout=15)
        try:
            _, stdout, _ = client.exec_command(
                "grep '^API_SECRET' /usr/local/bin/emailrelay-forward.py | head -1"
            )
            line = stdout.read().decode().strip()
            m = re.search(r'API_SECRET\s*=\s*["\']([^"\']+)["\']', line)
            vps_secret = m.group(1) if m else None
            if vps_secret is None:
                raise ValueError("API_SECRET nicht im Forward-Script gefunden — Setup noch nicht ausgeführt?")
            if vps_secret != local_secret:
                raise ValueError("API_SECRET veraltet — bitte Setup ausführen um zu synchronisieren")
            return "API-Secret stimmt überein"
        finally:
            client.close()

    test_ok = None
    test_error = None
    try:
        test_ok = await asyncio.get_event_loop().run_in_executor(None, _run_test)
    except Exception as e:
        test_error = str(e)

    return templates.TemplateResponse("vps_configs.html", {
        "request": request, "current_user": user, "vpss": vpss,
        "test_ok": test_ok, "test_error": test_error, "test_id": vps_id,
    })


# ── Alias-Domains (nur Admin) ──────────────────────────────────────────────────

@router.get("/alias-domains", response_class=HTMLResponse)
async def alias_domains_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    configs = (await db.execute(
        select(AliasDomainConfig)
        .options(selectinload(AliasDomainConfig.vps_config))
        .order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()
    vpss = (await db.execute(select(VpsConfig).where(VpsConfig.active == True).order_by(VpsConfig.created_at))).scalars().all()
    return templates.TemplateResponse("alias_domains.html", {
        "request": request, "current_user": user, "configs": configs, "vpss": vpss,
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
    vps_config_id: str = Form(""),
    catchall_enabled: str = Form("false"),
    catchall_target_address: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    alias_domain = alias_domain.strip().lower()
    existing = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.alias_domain == alias_domain)
    )).scalar_one_or_none()
    if not existing:
        cfg = AliasDomainConfig(
            label=label.strip(),
            alias_domain=alias_domain,
            smtp_host=smtp_host.strip(),
            smtp_port=int(smtp_port or 587),
            smtp_user=smtp_user.strip(),
            smtp_password=smtp_password,
            smtp_use_tls=smtp_use_tls != "false",
            vps_config_id=int(vps_config_id) if vps_config_id.strip() else None,
            catchall_enabled=catchall_enabled == "true",
            catchall_target_address=catchall_target_address.strip().lower(),
        )
        db.add(cfg)
        await db.flush()
        db.add(AliasDomainAccess(user_id=user.id, alias_domain_config_id=cfg.id))
        await db.commit()
        if cfg.vps_config_id:
            asyncio.create_task(_auto_vps_setup(cfg.vps_config_id))
    return RedirectResponse("/alias-domains", status_code=303)


@router.get("/alias-domains/{config_id}/edit", response_class=HTMLResponse)
async def alias_domain_edit_page(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    if not cfg:
        return RedirectResponse("/alias-domains", status_code=302)
    vpss = (await db.execute(select(VpsConfig).where(VpsConfig.active == True).order_by(VpsConfig.created_at))).scalars().all()
    return templates.TemplateResponse("alias_domain_edit.html", {
        "request": request, "current_user": user, "cfg": cfg, "vpss": vpss,
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
    vps_config_id: str = Form(""),
    is_default: str = Form("false"),
    catchall_enabled: str = Form("false"),
    catchall_target_address: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
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
        cfg.vps_config_id = int(vps_config_id) if vps_config_id.strip() else None
        cfg.is_default = is_default == "true"
        cfg.catchall_enabled = catchall_enabled == "true"
        cfg.catchall_target_address = catchall_target_address.strip().lower()
        await db.commit()
        if cfg.vps_config_id:
            asyncio.create_task(_auto_vps_setup(cfg.vps_config_id))
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/delete")
async def alias_domain_delete(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    await db.execute(delete(AliasDomainConfig).where(AliasDomainConfig.id == config_id))
    await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/toggle")
async def alias_domain_toggle(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    if cfg:
        cfg.active = not cfg.active
        await db.commit()
    return RedirectResponse("/alias-domains", status_code=303)


@router.post("/alias-domains/{config_id}/test", response_class=HTMLResponse)
async def alias_domain_test(config_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == config_id)
    )).scalar_one_or_none()
    all_configs = (await db.execute(
        select(AliasDomainConfig).options(selectinload(AliasDomainConfig.vps_config)).order_by(AliasDomainConfig.created_at.desc())
    )).scalars().all()
    vpss = (await db.execute(select(VpsConfig).where(VpsConfig.active == True))).scalars().all()
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
        "request": request, "current_user": user,
        "configs": all_configs, "vpss": vpss,
        "test_success": test_success, "test_error": test_error, "tested_id": config_id,
    })


# ── Domains ────────────────────────────────────────────────────────────────────

@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    domains = (await db.execute(
        select(Domain)
        .options(selectinload(Domain.alias_domain_config))
        .where(Domain.user_id == user.id)
        .order_by(Domain.created_at.desc())
    )).scalars().all()
    alias_configs = await get_user_alias_configs(db, user)
    return templates.TemplateResponse("domains.html", {
        "request": request, "current_user": user,
        "domains": domains, "alias_configs": alias_configs,
    })


@router.post("/domains")
async def domain_add(
    request: Request,
    db: AsyncSession = Depends(get_db),
    domain: str = Form(...),
    alias_domain_config_id: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    domain = domain.strip().lower()
    config_id = int(alias_domain_config_id) if alias_domain_config_id.strip() else None
    existing = (await db.execute(select(Domain).where(Domain.domain == domain))).scalar_one_or_none()
    if not existing:
        db.add(Domain(domain=domain, alias_domain_config_id=config_id, user_id=user.id))
        await db.commit()
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/{domain_id}/delete")
async def domain_delete(domain_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    await db.execute(delete(Domain).where(Domain.id == domain_id, Domain.user_id == user.id))
    await db.commit()
    return RedirectResponse("/domains", status_code=303)


@router.post("/domains/{domain_id}/toggle")
async def domain_toggle(domain_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    d = (await db.execute(
        select(Domain).where(Domain.id == domain_id, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if d:
        d.active = not d.active
        await db.commit()
    return RedirectResponse("/domains", status_code=303)


# ── E-Mail-Adressen ────────────────────────────────────────────────────────────

@router.get("/addresses", response_class=HTMLResponse)
async def addresses_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    addresses = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .options(selectinload(EmailAddress.domain))
        .where(Domain.user_id == user.id)
        .order_by(EmailAddress.created_at.desc())
    )).scalars().all()
    domains = (await db.execute(
        select(Domain).where(Domain.active == True, Domain.user_id == user.id)
    )).scalars().all()
    return templates.TemplateResponse("addresses.html", {
        "request": request, "current_user": user,
        "addresses": addresses, "domains": domains,
    })


@router.post("/addresses")
async def address_add(
    request: Request,
    db: AsyncSession = Depends(get_db),
    address: str = Form(...),
    domain_id: int = Form(...),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    address = address.strip().lower()
    # Sicherstellen dass die Domain dem User gehört
    domain = (await db.execute(
        select(Domain).where(Domain.id == domain_id, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if not domain:
        return RedirectResponse("/addresses", status_code=303)
    existing = (await db.execute(select(EmailAddress).where(EmailAddress.address == address))).scalar_one_or_none()
    if not existing:
        db.add(EmailAddress(address=address, domain_id=domain_id))
        await db.commit()
    return RedirectResponse("/addresses", status_code=303)


@router.post("/addresses/{addr_id}/delete")
async def address_delete(addr_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    addr = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .where(EmailAddress.id == addr_id, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if addr:
        await db.execute(delete(EmailAddress).where(EmailAddress.id == addr_id))
        await db.commit()
    return RedirectResponse("/addresses", status_code=303)


@router.post("/addresses/{addr_id}/toggle")
async def address_toggle(addr_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    addr = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .where(EmailAddress.id == addr_id, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if addr:
        addr.active = not addr.active
        await db.commit()
    return RedirectResponse("/addresses", status_code=303)


# ── Aliases ────────────────────────────────────────────────────────────────────

@router.get("/aliases", response_class=HTMLResponse)
async def aliases_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    aliases = (await db.execute(
        select(Alias).where(Alias.user_id == user.id).order_by(Alias.created_at.desc())
    )).scalars().all()
    email_addresses = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .where(EmailAddress.active == True, Domain.user_id == user.id)
        .order_by(EmailAddress.address)
    )).scalars().all()
    alias_domain_configs = await get_user_alias_configs(db, user)
    return templates.TemplateResponse("aliases.html", {
        "request": request, "current_user": user,
        "aliases": aliases, "email_addresses": email_addresses,
        "alias_domain_configs": alias_domain_configs,
    })


@router.post("/aliases/create")
async def alias_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    real_address: str = Form(...),
    alias_domain_id: int = Form(...),
    label: str = Form(""),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()

    email_addr = (await db.execute(
        select(EmailAddress)
        .join(Domain, EmailAddress.domain_id == Domain.id)
        .where(EmailAddress.address == real_address, EmailAddress.active == True, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if not email_addr:
        return RedirectResponse("/aliases", status_code=303)

    alias_cfg = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.id == alias_domain_id, AliasDomainConfig.active == True)
    )).scalar_one_or_none()
    if not alias_cfg:
        return RedirectResponse("/aliases", status_code=303)
    alias_domain = alias_cfg.alias_domain

    chars = string.ascii_lowercase + string.digits
    for _ in range(10):
        local = "".join(secrets.choice(chars) for _ in range(10))
        candidate = f"{local}@{alias_domain}"
        existing = (await db.execute(select(Alias).where(Alias.alias_address == candidate))).scalar_one_or_none()
        if not existing:
            break
    else:
        return RedirectResponse("/aliases", status_code=303)

    db.add(Alias(alias_address=candidate, real_address=real_address, label=label.strip(), user_id=user.id))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)


@router.post("/aliases/{alias_id}/toggle")
async def alias_toggle(alias_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    a = (await db.execute(
        select(Alias).where(Alias.id == alias_id, Alias.user_id == user.id)
    )).scalar_one_or_none()
    if a:
        a.active = not a.active
        await db.commit()
    return RedirectResponse("/aliases", status_code=303)


@router.post("/aliases/{alias_id}/delete")
async def alias_delete(alias_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    await db.execute(delete(Alias).where(Alias.id == alias_id, Alias.user_id == user.id))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)


@router.post("/aliases/{alias_id}/rotate")
async def alias_rotate(alias_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    a = (await db.execute(
        select(Alias).where(Alias.id == alias_id, Alias.active == True, Alias.user_id == user.id)
    )).scalar_one_or_none()
    if not a:
        return RedirectResponse("/aliases", status_code=303)

    alias_domain = a.alias_address.split("@", 1)[-1] if "@" in a.alias_address else None
    if not alias_domain:
        return RedirectResponse("/aliases", status_code=303)
    real_address = a.real_address

    chars = string.ascii_lowercase + string.digits
    for _ in range(10):
        local = "".join(secrets.choice(chars) for _ in range(10))
        candidate = f"{local}@{alias_domain}"
        existing = (await db.execute(select(Alias).where(Alias.alias_address == candidate))).scalar_one_or_none()
        if not existing:
            break
    else:
        return RedirectResponse("/aliases", status_code=303)

    a.active = False
    db.add(Alias(alias_address=candidate, real_address=real_address, label=a.label, user_id=user.id))
    await db.commit()
    return RedirectResponse("/aliases", status_code=303)


# ── Hilfe ──────────────────────────────────────────────────────────────────────

@router.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    return templates.TemplateResponse("guide.html", {"request": request, "current_user": user})


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return HTMLResponse(content=PRIVACY_POLICY_HTML)


# ── Registrierung ──────────────────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await get_setting(db, "registration_enabled", "false") != "true":
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/register")
async def register_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    if await get_setting(db, "registration_enabled", "false") != "true":
        return RedirectResponse("/login", status_code=302)

    username = username.strip()
    email = email.strip().lower()
    error = None

    if password != password2:
        error = "Passwörter stimmen nicht überein."
    elif len(password) < 8:
        error = "Passwort muss mindestens 8 Zeichen lang sein."
    elif not username:
        error = "Benutzername darf nicht leer sein."
    else:
        existing = (await db.execute(
            select(User).where(User.username == username)
        )).scalar_one_or_none()
        if existing:
            error = "Benutzername bereits vergeben."

    if error:
        return templates.TemplateResponse("register.html", {
            "request": request, "error": error, "username": username, "email": email,
        })

    pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
    new_user = User(username=username, password_hash=pw_hash, email=email, is_admin=False)
    db.add(new_user)
    await db.flush()

    # Zugriff auf alle Standard-Alias-Domains automatisch gewähren
    default_configs = (await db.execute(
        select(AliasDomainConfig)
        .where(AliasDomainConfig.is_default == True, AliasDomainConfig.active == True)
    )).scalars().all()
    for cfg in default_configs:
        db.add(AliasDomainAccess(user_id=new_user.id, alias_domain_config_id=cfg.id))

    await db.commit()

    # Admin per ntfy benachrichtigen
    ntfy = await get_setting(db, "ntfy_url")
    if ntfy:
        import httpx
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    ntfy,
                    content=f"Neuer Benutzer registriert: {username} ({email})".encode(),
                    headers={"Title": "E-Mail Relay: Neue Registrierung", "Priority": "default"},
                    timeout=5,
                )
            except Exception:
                pass

    return templates.TemplateResponse("register.html", {"request": request, "success": True})


# ── Passwort vergessen ─────────────────────────────────────────────────────────

@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    email: str = Form(...),
):
    from datetime import datetime, timezone, timedelta
    from email_utils import send_system_email

    email = email.strip().lower()
    user = (await db.execute(
        select(User).where(User.email == email, User.active == True)
    )).scalar_one_or_none()

    if user:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
        user.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()
        base_url = str(request.base_url).rstrip("/")
        reset_link = f"{base_url}/reset-password/{token}"
        html = (
            f"<p>Hallo {user.username},</p>"
            f"<p>du hast ein neues Passwort für dein E-Mail Relay Konto angefordert.</p>"
            f"<p><a href='{reset_link}'>Passwort jetzt zurücksetzen</a></p>"
            f"<p>Der Link ist 1 Stunde gültig.</p>"
            f"<p>Falls du diese Anfrage nicht gestellt hast, ignoriere diese E-Mail.</p>"
        )
        await send_system_email(email, "E-Mail Relay – Passwort zurücksetzen", html, db)

    # Immer dieselbe Meldung (verhindert User-Enumeration)
    return templates.TemplateResponse("forgot_password.html", {"request": request, "sent": True})


@router.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    user = (await db.execute(
        select(User).where(User.reset_token == token)
    )).scalar_one_or_none()
    valid = bool(
        user and user.token_expiry
        and user.token_expiry.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    )
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "token": token, "valid": valid,
    })


@router.post("/reset-password/{token}")
async def reset_password_submit(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    new_password: str = Form(...),
    new_password2: str = Form(...),
):
    from datetime import datetime, timezone
    user = (await db.execute(
        select(User).where(User.reset_token == token)
    )).scalar_one_or_none()
    valid = bool(
        user and user.token_expiry
        and user.token_expiry.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    )
    if not valid:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token, "valid": False,
        })
    if new_password != new_password2 or len(new_password) < 8:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, "token": token, "valid": True,
            "error": "Passwörter stimmen nicht überein oder zu kurz (min. 8 Zeichen).",
        })
    user.password_hash = _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt()).decode()
    user.reset_token = None
    user.token_expiry = None
    await db.commit()
    return templates.TemplateResponse("reset_password.html", {
        "request": request, "token": token, "valid": True, "done": True,
    })


# ── Setup-Wizard ───────────────────────────────────────────────────────────────

@router.get("/setup", response_class=HTMLResponse)
async def setup_wizard(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    alias_configs = await get_user_alias_configs(db, user)
    if not alias_configs:
        request.session["setup_skipped"] = True
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("setup.html", {
        "request": request, "current_user": user, "alias_configs": alias_configs,
    })


@router.post("/setup/skip")
async def setup_skip(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    request.session["setup_skipped"] = True
    return RedirectResponse("/", status_code=302)


@router.post("/setup")
async def setup_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    email_address: str = Form(...),
    alias_domain_config_id: int = Form(...),
):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()

    email_address = email_address.strip().lower()
    alias_configs = await get_user_alias_configs(db, user)

    # Sicherstellen dass der User Zugriff auf die gewählte Alias-Domain hat
    alias_cfg = next((c for c in alias_configs if c.id == alias_domain_config_id), None)

    if not alias_cfg or "@" not in email_address:
        return templates.TemplateResponse("setup.html", {
            "request": request, "current_user": user,
            "alias_configs": alias_configs,
            "error": "Bitte eine gültige E-Mail-Adresse eingeben.",
        })

    domain_name = email_address.split("@", 1)[1]

    # Domain anlegen falls noch nicht vorhanden
    domain = (await db.execute(
        select(Domain).where(Domain.domain == domain_name, Domain.user_id == user.id)
    )).scalar_one_or_none()
    if not domain:
        domain = Domain(
            domain=domain_name,
            alias_domain_config_id=alias_domain_config_id,
            user_id=user.id,
        )
        db.add(domain)
        await db.flush()

    # E-Mail-Adresse anlegen falls noch nicht vorhanden
    existing_addr = (await db.execute(
        select(EmailAddress).where(EmailAddress.address == email_address)
    )).scalar_one_or_none()
    if not existing_addr:
        db.add(EmailAddress(address=email_address, domain_id=domain.id))

    await db.commit()
    request.session["setup_skipped"] = True
    return RedirectResponse("/aliases", status_code=302)


# ── System-SMTP-Einstellungen (nur Admin) ──────────────────────────────────────

@router.post("/settings/system-smtp")
async def settings_system_smtp(
    request: Request,
    db: AsyncSession = Depends(get_db),
    system_smtp_host: str = Form(""),
    system_smtp_port: str = Form("587"),
    system_smtp_user: str = Form(""),
    system_smtp_password: str = Form(""),
    system_smtp_from: str = Form(""),
    system_smtp_use_tls: str = Form("true"),
    registration_enabled: str = Form("false"),
):
    user = await get_current_user(request, db)
    if not user or not user.is_admin:
        return redirect_login()
    for key, val in [
        ("system_smtp_host", system_smtp_host.strip()),
        ("system_smtp_port", system_smtp_port.strip() or "587"),
        ("system_smtp_user", system_smtp_user.strip()),
        ("system_smtp_from", system_smtp_from.strip()),
        ("system_smtp_use_tls", system_smtp_use_tls),
        ("registration_enabled", registration_enabled),
    ]:
        await save_setting(db, key, val)
    # Passwort nur überschreiben wenn neu eingegeben
    if system_smtp_password:
        await save_setting(db, "system_smtp_password", system_smtp_password)
    await db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)
