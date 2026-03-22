from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True)
    domain = Column(String, unique=True, nullable=False)
    alias_domain = Column(String, nullable=True)  # Domain für Alias-Adressen
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
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
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used = Column(DateTime(timezone=True), nullable=True)


class SmtpAccount(Base):
    __tablename__ = "smtp_accounts"
    id = Column(Integer, primary_key=True)
    label = Column(String, default="")
    pattern = Column(String, unique=True, nullable=False)  # z.B. "user@gmail.com" oder "@gmail.com"
    smtp_host = Column(String, nullable=False)
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String, nullable=False)
    smtp_password = Column(String, nullable=False)
    smtp_use_tls = Column(Boolean, default=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
