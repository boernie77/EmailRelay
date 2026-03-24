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

echo "=== EmailRelay VPS Setup ==="
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

router = APIRouter(tags=["ui"])
templates = Jinja2Templates(directory="templates")


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
    # Upgrade-Hinweis: Keine User, aber altes Passwort vorhanden
    is_upgrade = not has_users and bool(await get_setting(db, "ui_password_hash"))
    return templates.TemplateResponse("login.html", {
        "request": request, "has_users": has_users, "is_upgrade": is_upgrade,
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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "current_user": user,
        "alias_count": len(alias_count),
        "domain_count": len(domain_count),
        "address_count": len(address_count),
        "recent_aliases": recent_aliases,
    })


# ── Einstellungen ──────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return redirect_login()
    return templates.TemplateResponse("settings.html", {"request": request, "current_user": user})


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

    return templates.TemplateResponse("settings.html", {
        "request": request, "current_user": user, "error": error, "success": success,
    })


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
    domains_regex = "\n".join(f"/@{d.replace('.', r'\.')}$/  OK" for d in alias_domains)

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
        cfg.catchall_enabled = catchall_enabled == "true"
        cfg.catchall_target_address = catchall_target_address.strip().lower()
        await db.commit()
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
    alias_domain_configs = (await db.execute(
        select(AliasDomainConfig).where(AliasDomainConfig.active == True).order_by(AliasDomainConfig.alias_domain)
    )).scalars().all()
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
