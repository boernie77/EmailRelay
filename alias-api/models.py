from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    domains = relationship("Domain", back_populates="user")
    aliases = relationship("Alias", back_populates="user")
    alias_domain_access = relationship("AliasDomainAccess", back_populates="user", cascade="all, delete-orphan")


class AliasDomainAccess(Base):
    """Welcher User darf welche AliasDomainConfig verwenden."""
    __tablename__ = "alias_domain_access"
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    alias_domain_config_id = Column(Integer, ForeignKey("alias_domain_configs.id", ondelete="CASCADE"), primary_key=True)
    user = relationship("User", back_populates="alias_domain_access")
    config = relationship("AliasDomainConfig", back_populates="user_access")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


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
    vps_host = Column(String, default="")
    vps_port = Column(Integer, default=22)
    vps_user = Column(String, default="root")
    vps_ssh_key = Column(Text, default="")
    api_url_for_vps = Column(String, default="")
    active = Column(Boolean, default=True)
    catchall_enabled = Column(Boolean, default=False)
    catchall_target_address = Column(String, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    domains = relationship("Domain", back_populates="alias_domain_config")
    user_access = relationship("AliasDomainAccess", back_populates="config", cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True)
    domain = Column(String, unique=True, nullable=False)
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
