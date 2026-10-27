"""Tables for organic publishing: encrypted client credentials, a publish queue, and a credential audit trail."""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, _enum
from app.organic.crypto import EncryptedText


class CredentialStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class PublishPlatform(str, enum.Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class PublishStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHED = "published"
    FAILED = "failed"
    BLOCKED = "blocked"


class CredentialEventType(str, enum.Enum):
    CONNECTED = "connected"
    PAGE_SELECTED = "page_selected"
    TOKEN_CHECKED = "token_checked"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_INVALID = "token_invalid"
    PERMISSION_REVOKED = "permission_revoked"
    DISCONNECTED = "disconnected"


def _now_column() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class ClientPlatformCredential(Base):
    """One row per client. Holds the long-lived user token and the selected Page and Instagram account.

    The page token is set only after the client picks a Page, and is never returned by any API.
    """

    __tablename__ = "client_platform_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), unique=True)
    fb_user_id: Mapped[str] = mapped_column(String(64))
    fb_user_access_token: Mapped[str] = mapped_column(EncryptedText)
    user_token_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    fb_page_id: Mapped[str | None] = mapped_column(String(64))
    fb_page_name: Mapped[str | None] = mapped_column(String(255))
    fb_page_access_token: Mapped[str | None] = mapped_column(EncryptedText)
    instagram_business_account_id: Mapped[str | None] = mapped_column(String(64))
    instagram_username: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[CredentialStatus] = mapped_column(
        _enum(CredentialStatus, "credential_status"), default=CredentialStatus.ACTIVE
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now_column()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (Index("ix_publish_jobs_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    platform: Mapped[PublishPlatform] = mapped_column(_enum(PublishPlatform, "publish_platform"))
    video_url: Mapped[str] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text)
    status: Mapped[PublishStatus] = mapped_column(_enum(PublishStatus, "publish_status"), default=PublishStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ig_container_id: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now_column()


class CredentialEvent(Base):
    """Append-only audit trail of token checks, revocations, and connections. Never contains token values."""

    __tablename__ = "credential_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    event: Mapped[CredentialEventType] = mapped_column(_enum(CredentialEventType, "credential_event_type"))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _now_column()
