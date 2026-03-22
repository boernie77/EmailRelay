"""UI-Routen für das Web-Interface."""
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from database import get_db
from models import Setting, Domain, EmailAddress, Alias

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
