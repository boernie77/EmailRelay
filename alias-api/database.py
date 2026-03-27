import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://emailrelay:emailrelay@postgres:5432/emailrelay")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    for stmt in [
        "ALTER TABLE domains ADD COLUMN alias_domain_config_id INTEGER "
        "REFERENCES alias_domain_configs(id) ON DELETE SET NULL",
        "ALTER TABLE aliases ADD COLUMN label VARCHAR DEFAULT ''",
        "ALTER TABLE alias_domain_configs ADD COLUMN catchall_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE alias_domain_configs ADD COLUMN catchall_target_address VARCHAR DEFAULT ''",
        "ALTER TABLE domains ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE aliases ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE alias_domain_configs ADD COLUMN vps_config_id INTEGER "
        "REFERENCES vps_configs(id) ON DELETE SET NULL",
        "ALTER TABLE users ADD COLUMN email VARCHAR",
        "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN reset_token VARCHAR",
        "ALTER TABLE users ADD COLUMN token_expiry TIMESTAMPTZ",
        "ALTER TABLE alias_domain_configs ADD COLUMN is_default BOOLEAN DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN invite_code_used VARCHAR",
        "ALTER TABLE users ADD COLUMN preset_alias_domain VARCHAR",
    ]:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception:
            pass

    await _migrate_to_alias_domain_configs()
    await _migrate_to_vps_configs()


async def _migrate_to_alias_domain_configs():
    from sqlalchemy import select
    from models import AliasDomainConfig, Domain, Setting

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(AliasDomainConfig))).scalars().first()
        if not existing:
            async def gs(key):
                r = (await session.execute(
                    select(Setting).where(Setting.key == key)
                )).scalar_one_or_none()
                return r.value if r else ""

            alias_domain = await gs("alias_domain")
            smtp_host = await gs("smtp_host")
            if alias_domain or smtp_host:
                cfg = AliasDomainConfig(
                    label="Standard",
                    alias_domain=alias_domain or "alias.example.com",
                    smtp_host=await gs("smtp_host"),
                    smtp_port=int(await gs("smtp_port") or 587),
                    smtp_user=await gs("smtp_user"),
                    smtp_password=await gs("smtp_password"),
                    smtp_use_tls=(await gs("smtp_use_tls")) != "false",
                )
                session.add(cfg)
                await session.flush()
                for d in (await session.execute(select(Domain))).scalars().all():
                    d.alias_domain_config_id = cfg.id
                await session.commit()
        else:
            first = (await session.execute(
                select(AliasDomainConfig).where(AliasDomainConfig.active == True)
            )).scalars().first()
            if first:
                for d in (await session.execute(
                    select(Domain).where(Domain.alias_domain_config_id == None)
                )).scalars().all():
                    d.alias_domain_config_id = first.id
                await session.commit()


async def _migrate_to_vps_configs():
    """Erstellt VpsConfig aus alten VPS-Feldern in alias_domain_configs (falls vorhanden)."""
    from sqlalchemy import select
    from models import VpsConfig, AliasDomainConfig

    async with AsyncSessionLocal() as session:
        existing_vps = (await session.execute(select(VpsConfig))).scalars().first()
        if existing_vps:
            # Bereits migriert – alle AliasDomainConfigs ohne VPS dem ersten zuweisen
            for cfg in (await session.execute(
                select(AliasDomainConfig).where(AliasDomainConfig.vps_config_id == None)
            )).scalars().all():
                cfg.vps_config_id = existing_vps.id
            await session.commit()
            return

        # Alte VPS-Daten aus DB lesen (Felder existieren in DB, aber nicht mehr im Modell)
        rows = (await session.execute(text(
            "SELECT id, vps_host, vps_port, vps_user, vps_ssh_key, api_url_for_vps "
            "FROM alias_domain_configs "
            "WHERE vps_host IS NOT NULL AND vps_host != '' "
            "LIMIT 1"
        ))).fetchall()

        if not rows:
            return

        row = rows[0]
        vps = VpsConfig(
            label="VPS",
            host=row[1] or "",
            port=row[2] or 22,
            user=row[3] or "root",
            ssh_key=row[4] or "",
            api_url=row[5] or "",
        )
        session.add(vps)
        await session.flush()

        for cfg in (await session.execute(select(AliasDomainConfig))).scalars().all():
            cfg.vps_config_id = vps.id
        await session.commit()
