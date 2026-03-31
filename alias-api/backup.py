"""Datensicherung: CSV-Export/Import, SSH-Backup, automatischer Backup-Scheduler."""
import asyncio
import csv
import io
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Alias, Domain, EmailAddress, User, Setting


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else default


async def _save_setting(db: AsyncSession, key: str, value: str):
    row = (await db.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


async def generate_user_aliases_csv(db: AsyncSession, user_id: int) -> str:
    """Alle Aliases eines Benutzers als CSV-String."""
    aliases = (await db.execute(
        select(Alias).where(Alias.user_id == user_id).order_by(Alias.created_at)
    )).scalars().all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["alias_address", "real_address", "label", "active"])
    for a in aliases:
        w.writerow([a.alias_address, a.real_address, a.label or "", "ja" if a.active else "nein"])
    return out.getvalue()


async def import_user_aliases_csv(db: AsyncSession, user_id: int, csv_text: str) -> dict:
    """Aliases aus CSV importieren. Gibt {'created', 'skipped', 'errors'} zurück."""
    created = 0
    skipped = 0
    errors = []
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
    except Exception as e:
        return {"created": 0, "skipped": 0, "errors": [f"CSV-Parsing-Fehler: {e}"]}

    for i, row in enumerate(rows, start=2):
        alias_addr = (row.get("alias_address") or "").strip().lower()
        real_addr = (row.get("real_address") or "").strip().lower()
        label = (row.get("label") or "").strip()
        active_str = (row.get("active") or "ja").strip().lower()
        active = active_str not in ("nein", "false", "0", "no", "inaktiv")

        if not alias_addr or "@" not in alias_addr:
            errors.append(f"Zeile {i}: Ungültige Alias-Adresse '{alias_addr[:40]}'")
            continue
        if not real_addr or "@" not in real_addr:
            errors.append(f"Zeile {i}: Ungültige Zieladresse '{real_addr[:40]}'")
            continue

        existing = (await db.execute(
            select(Alias).where(Alias.alias_address == alias_addr)
        )).scalar_one_or_none()
        if existing:
            skipped += 1
            continue

        db.add(Alias(alias_address=alias_addr, real_address=real_addr,
                     label=label, active=active, user_id=user_id))
        created += 1

    if created > 0:
        await db.commit()
    return {"created": created, "skipped": skipped, "errors": errors[:10]}


async def generate_full_backup_zip(db: AsyncSession) -> bytes:
    """Vollständiges System-Backup als ZIP (Admin)."""
    buf = io.BytesIO()
    ts = datetime.now(timezone.utc)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Aliases (alle Benutzer)
        aliases = (await db.execute(
            select(Alias).order_by(Alias.user_id, Alias.created_at)
        )).scalars().all()
        alias_out = io.StringIO()
        aw = csv.writer(alias_out)
        aw.writerow(["alias_address", "real_address", "label", "active", "user_id", "created_at", "last_used"])
        for a in aliases:
            aw.writerow([
                a.alias_address, a.real_address, a.label or "",
                "ja" if a.active else "nein", a.user_id or "",
                a.created_at.isoformat() if a.created_at else "",
                a.last_used.isoformat() if a.last_used else "",
            ])
        zf.writestr("aliases.csv", alias_out.getvalue())

        # Benutzer (ohne Passwort-Hashes)
        users = (await db.execute(select(User).order_by(User.id))).scalars().all()
        user_out = io.StringIO()
        uw = csv.writer(user_out)
        uw.writerow(["username", "email", "is_admin", "active", "created_at"])
        for u in users:
            uw.writerow([
                u.username, u.email or "",
                "ja" if u.is_admin else "nein",
                "ja" if u.active else "nein",
                u.created_at.isoformat() if u.created_at else "",
            ])
        zf.writestr("users.csv", user_out.getvalue())

        # Domains
        domains = (await db.execute(select(Domain).order_by(Domain.id))).scalars().all()
        dom_out = io.StringIO()
        dw = csv.writer(dom_out)
        dw.writerow(["domain", "active", "user_id"])
        for d in domains:
            dw.writerow([d.domain, "ja" if d.active else "nein", d.user_id or ""])
        zf.writestr("domains.csv", dom_out.getvalue())

        # E-Mail-Adressen
        addrs = (await db.execute(select(EmailAddress).order_by(EmailAddress.id))).scalars().all()
        addr_out = io.StringIO()
        adw = csv.writer(addr_out)
        adw.writerow(["address", "active"])
        for a in addrs:
            adw.writerow([a.address, "ja" if a.active else "nein"])
        zf.writestr("addresses.csv", addr_out.getvalue())

        # Metadaten
        zf.writestr("backup_info.txt", (
            f"E-Mail Relay Backup\n"
            f"Datum: {ts.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Benutzer: {len(users)}, Aliases: {len(aliases)}\n"
        ))

    return buf.getvalue()


def _ssh_test_sync(host: str, port: int, username: str, key_pem: str, remote_path: str):
    """Synchron: SSH-Verbindung und Schreibrechte prüfen."""
    import paramiko
    key = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            key = cls.from_private_key(io.StringIO(key_pem))
            break
        except Exception:
            pass
    if key is None:
        raise ValueError("SSH-Key konnte nicht gelesen werden (unterstützte Formate: Ed25519, RSA, ECDSA, DSS).")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, pkey=key, timeout=10)
    try:
        sftp = client.open_sftp()
        sftp.stat(remote_path)
        test_file = remote_path.rstrip("/") + "/.emailrelay_backup_test"
        with sftp.open(test_file, "w") as f:
            f.write("ok")
        sftp.remove(test_file)
        sftp.close()
    finally:
        client.close()


def _ssh_upload_sync(host: str, port: int, username: str, key_pem: str,
                     remote_path: str, data: bytes, filename: str):
    """Synchron: ZIP-Backup per SFTP hochladen."""
    import paramiko
    key = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey):
        try:
            key = cls.from_private_key(io.StringIO(key_pem))
            break
        except Exception:
            pass
    if key is None:
        raise ValueError("SSH-Key konnte nicht gelesen werden.")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, pkey=key, timeout=20)
    try:
        sftp = client.open_sftp()
        remote_file = remote_path.rstrip("/") + "/" + filename
        with sftp.open(remote_file, "wb") as f:
            f.write(data)
        sftp.close()
    finally:
        client.close()


async def run_ssh_backup(db: AsyncSession):
    """Backup erstellen und per SFTP hochladen. Ergebnis in Settings speichern."""
    host = await _get_setting(db, "backup_ssh_host")
    port_str = await _get_setting(db, "backup_ssh_port")
    port = int(port_str) if port_str and port_str.isdigit() else 22
    ssh_user = await _get_setting(db, "backup_ssh_user")
    key_pem = await _get_setting(db, "backup_ssh_key_pem")
    remote_path = await _get_setting(db, "backup_ssh_remote_path")

    if not all([host, ssh_user, key_pem, remote_path]):
        raise ValueError("SSH-Backup-Konfiguration unvollständig (Host, Benutzer, Schlüssel und Pfad erforderlich)")

    data = await generate_full_backup_zip(db)
    filename = f"emailrelay-backup-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.zip"

    await asyncio.get_event_loop().run_in_executor(
        None, _ssh_upload_sync, host, port, ssh_user, key_pem, remote_path, data, filename
    )

    await _save_setting(db, "backup_last_run", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    await _save_setting(db, "backup_last_status", "ok")
    await db.commit()


async def backup_scheduler():
    """Hintergrundtask: Prüft stündlich ob ein automatisches Backup fällig ist."""
    from database import AsyncSessionLocal
    await asyncio.sleep(300)  # 5 Minuten nach Start warten
    while True:
        try:
            async with AsyncSessionLocal() as db:
                schedule = await _get_setting(db, "backup_schedule")
                if schedule and schedule != "disabled":
                    last_run_str = await _get_setting(db, "backup_last_run")
                    now = datetime.now(timezone.utc)
                    due = not last_run_str
                    if not due and last_run_str:
                        try:
                            last_run = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                            delta = now - last_run
                            if schedule == "daily" and delta.total_seconds() >= 86400:
                                due = True
                            elif schedule == "weekly" and delta.total_seconds() >= 604800:
                                due = True
                        except Exception:
                            due = True
                    if due:
                        try:
                            await run_ssh_backup(db)
                        except Exception as e:
                            await _save_setting(db, "backup_last_run", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
                            await _save_setting(db, "backup_last_status", f"Fehler: {str(e)[:150]}")
                            await db.commit()
        except asyncio.CancelledError:
            break
        except Exception:
            pass
        try:
            await asyncio.sleep(3600)  # Jede Stunde prüfen
        except asyncio.CancelledError:
            break
