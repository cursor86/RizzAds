import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Platform(str, enum.Enum):
    META = "meta"
    GOOGLE = "google"


class AngleTag(str, enum.Enum):
    PAIN_POINT = "pain_point"
    BENEFIT = "benefit"
    SOCIAL_PROOF = "social_proof"


class AssetKind(str, enum.Enum):
    HOOK = "hook"
    BODY = "body"
    HEADLINE = "headline"
    IMAGE = "image"
    VIDEO = "video"


class Orientation(str, enum.Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    SQUARE = "square"


class ActionType(str, enum.Enum):
    LAUNCH = "launch"
    PAUSE_AD = "pause_ad"
    SCALE_BUDGET = "scale_budget"
    REQUEST_ITERATION = "request_iteration"


class IterationStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


def _enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    # Stored as VARCHAR with a CHECK constraint, so migrations stay portable across databases.
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )


def _created_at() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    brief: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = _created_at()


class AdAccount(Base):
    """A client's ad account on one platform. Tokens are referenced by env var name, never stored."""

    __tablename__ = "ad_accounts"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_ad_account_platform_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    platform: Mapped[Platform] = mapped_column(_enum(Platform, "platform"))
    external_id: Mapped[str] = mapped_column(String(64))
    secret_ref: Mapped[str] = mapped_column(String(128))
    page_id: Mapped[str | None] = mapped_column(String(64))
    pixel_id: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(64))
    target_cpa: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    target_roas: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    created_at: Mapped[dt.datetime] = _created_at()


class CreativeAsset(Base):
    """One element of the creative matrix. Only approved assets are eligible for deployment."""

    __tablename__ = "creative_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    angle: Mapped[AngleTag] = mapped_column(_enum(AngleTag, "angle_tag"))
    kind: Mapped[AssetKind] = mapped_column(_enum(AssetKind, "asset_kind"))
    text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    meta_media_id: Mapped[str | None] = mapped_column(String(64))
    google_youtube_video_id: Mapped[str | None] = mapped_column(String(32))
    orientation: Mapped[Orientation | None] = mapped_column(_enum(Orientation, "orientation"))
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="human")
    created_at: Mapped[dt.datetime] = _created_at()


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (UniqueConstraint("ad_account_id", "external_id", name="uq_campaign_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_account_id: Mapped[int] = mapped_column(ForeignKey("ad_accounts.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="active")
    daily_budget_minor: Mapped[int] = mapped_column(BigInteger)
    last_budget_change_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()


class AdSet(Base):
    """Meta ad set, or Google ad group. Holds the broad targeting limits (country, age floor)."""

    __tablename__ = "ad_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    age_min: Mapped[int | None] = mapped_column(Integer)
    age_max: Mapped[int | None] = mapped_column(Integer)
    countries: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = _created_at()


class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_set_id: Mapped[int] = mapped_column(ForeignKey("ad_sets.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    angle: Mapped[AngleTag] = mapped_column(_enum(AngleTag, "ad_angle_tag"))
    creative_asset_ids: Mapped[list[int]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="active")
    paused_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    __table_args__ = (UniqueConstraint("ad_id", "snapshot_date", name="uq_snapshot_ad_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int] = mapped_column(ForeignKey("ads.id", ondelete="CASCADE"), index=True)
    snapshot_date: Mapped[dt.date] = mapped_column(Date)
    spend: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    impressions: Mapped[int] = mapped_column(Integer)
    link_clicks: Mapped[int] = mapped_column(Integer)
    conversions: Mapped[int] = mapped_column(Integer)
    conversion_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    video_3s: Mapped[int | None] = mapped_column(Integer)
    video_15s: Mapped[int | None] = mapped_column(Integer)
    ctr: Mapped[float | None] = mapped_column(Float)
    cpa: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    roas: Mapped[float | None] = mapped_column(Float)
    hook_rate: Mapped[float | None] = mapped_column(Float)
    hold_rate: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[dt.datetime] = _created_at()


class ActionLog(Base):
    """Audit trail of every change the engine makes on a platform."""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[int | None] = mapped_column(ForeignKey("ads.id", ondelete="SET NULL"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), index=True)
    action: Mapped[ActionType] = mapped_column(_enum(ActionType, "action_type"))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = _created_at()


class IterationRequest(Base):
    """Flag raised when an angle outperforms. Generates reviewable variations, never deploys them."""

    __tablename__ = "iteration_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id", ondelete="CASCADE"), index=True)
    angle: Mapped[AngleTag] = mapped_column(_enum(AngleTag, "iteration_angle_tag"))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[IterationStatus] = mapped_column(
        _enum(IterationStatus, "iteration_status"), default=IterationStatus.PENDING
    )
    created_at: Mapped[dt.datetime] = _created_at()
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
