from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    email = Column(String, nullable=True)
    email_verified = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    reset_token = Column(String, nullable=True)
    token_expiry = Column(DateTime(timezone=True), nullable=True)
    invite_code_used = Column(String, nullable=True)
    preset_alias_domain = Column(String, nullable=True)
    domains = relationship("Domain", back_populates="user")
    aliases = relationship("Alias", back_populates="user")
    alias_domain_access = relationship("AliasDomainAccess", back_populates="user", cascade="all, delete-orphan")


class AliasDomainAccess(Base):
    __tablename__ = "alias_domain_access"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    alias_domain_config_id = Column(Integer, ForeignKey("alias_domain_configs.id", ondelete="CASCADE"), primary_key=True)
    user = relationship("User", back_populates="alias_domain_access")
    config = relationship("AliasDomainConfig", back_populates="user_access")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class VpsConfig(Base):
    __tablename__ = "vps_configs"
    id = Column(Integer, primary_key=True)
    label = Column(String, default="")
    host = Column(String, default="")
    port = Column(Integer, default=22)
    user = Column(String, default="root")
    ssh_key = Column(Text, default="")
    api_url = Column(String, default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    alias_domain_configs = relationship("AliasDomainConfig", back_populates="vps_config")


class AliasDomainConfig(Base):
    __tablename__ = "alias_domain_configs"
    id = Column(Integer, primary_key=True)
    label = Column(String, default="")
    alias_domain = Column(String, unique=True, nullable=False)
    smtp_host = Column(String, default="")
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String, default="")
    smtp_password = Column(String, default="")
    smtp_use_tls = Column(Boolean, default=True)
    active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    catchall_enabled = Column(Boolean, default=False)
    catchall_target_address = Column(String, default="")
    vps_config_id = Column(Integer, ForeignKey("vps_configs.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    vps_config = relationship("VpsConfig", back_populates="alias_domain_configs")
    domains = relationship("Domain", back_populates="alias_domain_config")
    user_access = relationship("AliasDomainAccess", back_populates="config", cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (UniqueConstraint("domain", "user_id", name="uq_domain_user"),)
    id = Column(Integer, primary_key=True)
    domain = Column(String, nullable=False)
    alias_domain = Column(String, nullable=True)  # Legacy-Spalte
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    alias_domain_config_id = Column(Integer, ForeignKey("alias_domain_configs.id", ondelete="SET NULL"), nullable=True)
    user = relationship("User", back_populates="domains")
    alias_domain_config = relationship("AliasDomainConfig", back_populates="domains")
    email_addresses = relationship("EmailAddress", back_populates="domain", cascade="all, delete-orphan")


class EmailAddress(Base):
    __tablename__ = "email_addresses"
    id = Column(Integer, primary_key=True)
    address = Column(String, unique=True, nullable=False)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    domain = relationship("Domain", back_populates="email_addresses")


class Alias(Base):
    __tablename__ = "aliases"
    id = Column(Integer, primary_key=True)
    alias_address = Column(String, unique=True, nullable=False)
    real_address = Column(String, nullable=False)
    label = Column(String, default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used = Column(DateTime(timezone=True), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    user = relationship("User", back_populates="aliases")


class AliasMessageLog(Base):
    """Speichert Message-ID → Alias-Zuordnung für korrekte Alias-Auswahl bei Antworten."""
    __tablename__ = "alias_message_logs"
    id = Column(Integer, primary_key=True)
    message_id = Column(String, unique=True, nullable=False, index=True)
    alias_address = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
