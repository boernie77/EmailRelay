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
    # Jede Migration in eigener Transaktion (PostgreSQL bricht bei Fehler die ganze TX ab)
    for stmt in [
        "ALTER TABLE domains ADD COLUMN alias_domain_config_id INTEGER "
        "REFERENCES alias_domain_configs(id) ON DELETE SET NULL",
        "ALTER TABLE aliases ADD COLUMN label VARCHAR DEFAULT ''",
        "ALTER TABLE alias_domain_configs ADD COLUMN catchall_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE alias_domain_configs ADD COLUMN catchall_target_address VARCHAR DEFAULT ''",
    ]:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception:
            pass

    await _migrate_to_alias_domain_configs()


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
                    vps_host=await gs("vps_host"),
                    vps_port=int(await gs("vps_port") or 22),
                    vps_user=await gs("vps_user") or "root",
                    vps_ssh_key=await gs("vps_ssh_key"),
                    api_url_for_vps=await gs("api_url_for_vps"),
                )
                session.add(cfg)
                await session.flush()
                domains = (await session.execute(select(Domain))).scalars().all()
                for d in domains:
                    d.alias_domain_config_id = cfg.id
                await session.commit()
        else:
            # Nicht zugewiesene Domains der ersten aktiven Config zuordnen
            first = (await session.execute(
                select(AliasDomainConfig).where(AliasDomainConfig.active == True)
            )).scalars().first()
            if first:
                unassigned = (await session.execute(
                    select(Domain).where(Domain.alias_domain_config_id == None)
                )).scalars().all()
                if unassigned:
                    for d in unassigned:
                        d.alias_domain_config_id = first.id
                    await session.commit()
