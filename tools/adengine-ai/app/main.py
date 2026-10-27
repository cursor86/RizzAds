"""FastAPI service: setup, creative approval, launches, reporting, and manual optimization triggers."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal, engine, get_session
from app.errors import AdEngineError, DeployValidationError, ExternalAPIError
from app.models import (
    Ad,
    AdAccount,
    AdSet,
    AngleTag,
    AssetKind,
    Base,
    Brand,
    Campaign,
    CreativeAsset,
    Orientation,
    PerformanceSnapshot,
    Platform,
)
from app.services.deploy_google import GoogleLaunchSpec, launch_google_campaign
from app.services.deploy_meta import MetaLaunchSpec, launch_meta_campaign
from app.services.iteration import process_pending_iterations
from app.services.optimization_loop import run_optimization_cycle


async def _lifespan(_: FastAPI):
    if get_settings().auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="AdEngine-AI", version="0.1.0", lifespan=_lifespan)


@app.exception_handler(DeployValidationError)
async def _validation_handler(_: Request, exc: DeployValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation", "detail": str(exc)})


@app.exception_handler(ExternalAPIError)
async def _external_handler(_: Request, exc: ExternalAPIError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"error": "platform_api", "detail": str(exc)})


@app.exception_handler(AdEngineError)
async def _engine_handler(_: Request, exc: AdEngineError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "internal", "detail": str(exc)})


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_api_key.encode(), get_settings().api_key.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")


AuthDep = Depends(require_api_key)
SessionDep = Depends(get_session)


# --- Schemas ----------------------------------------------------------------------------------


class BrandIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    brief: str = Field(default="", max_length=8000)


class AdAccountIn(BaseModel):
    platform: Platform
    external_id: str = Field(pattern=r"^\d{5,20}$")
    secret_ref: str = Field(min_length=3, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    page_id: str | None = Field(default=None, pattern=r"^\d+$")
    pixel_id: str | None = Field(default=None, pattern=r"^\d+$")
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    timezone: str = Field(min_length=3, max_length=64)
    target_cpa: Decimal = Field(gt=0)
    target_roas: Decimal = Field(gt=0)


class CreativeIn(BaseModel):
    angle: AngleTag
    kind: AssetKind
    text: str | None = Field(default=None, max_length=2200)
    media_url: str | None = Field(default=None, max_length=2000)
    orientation: Orientation | None = None
    google_youtube_video_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{6,20}$")
    source: str = Field(default="human", max_length=16)

    @field_validator("media_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("media_url must be https")
        return value

    @model_validator(mode="after")
    def _kind_requires_payload(self) -> "CreativeIn":
        if self.kind in (AssetKind.HOOK, AssetKind.BODY, AssetKind.HEADLINE) and not self.text:
            raise ValueError(f"{self.kind.value} assets need text")
        if self.kind in (AssetKind.IMAGE, AssetKind.VIDEO) and not self.media_url:
            raise ValueError(f"{self.kind.value} assets need media_url")
        if self.kind == AssetKind.VIDEO and self.google_youtube_video_id and self.orientation is None:
            raise ValueError("video assets with a YouTube id need an orientation")
        return self


class MetaLaunchIn(BaseModel):
    daily_budget_minor: int = Field(gt=0)
    countries: list[str] = Field(min_length=1, max_length=50)
    age_min: int = Field(ge=13, le=65)
    age_max: int = Field(ge=13, le=65)
    link_url: str = Field(max_length=2000)
    cta: str = Field(default="SHOP_NOW", max_length=40)


class GoogleLaunchIn(BaseModel):
    daily_budget_minor: int = Field(gt=0)
    keywords: list[str] = Field(min_length=1, max_length=200)
    final_url: str = Field(max_length=2000)
    business_name: str = Field(min_length=1, max_length=25)


# --- Setup ------------------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/brands", status_code=201, dependencies=[AuthDep])
async def create_brand(payload: BrandIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    brand = Brand(name=payload.name, brief=payload.brief)
    session.add(brand)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A brand with this name already exists")
    return {"id": brand.id, "name": brand.name}


@app.post("/brands/{brand_id}/ad-accounts", status_code=201, dependencies=[AuthDep])
async def create_ad_account(brand_id: int, payload: AdAccountIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    if await session.get(Brand, brand_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    account = AdAccount(brand_id=brand_id, **payload.model_dump())
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This ad account is already registered")
    return {"id": account.id, "platform": account.platform.value}


@app.post("/brands/{brand_id}/creatives", status_code=201, dependencies=[AuthDep])
async def create_creative(brand_id: int, payload: CreativeIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    if await session.get(Brand, brand_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brand not found")
    asset = CreativeAsset(brand_id=brand_id, approved=False, **payload.model_dump())
    session.add(asset)
    await session.commit()
    return {"id": asset.id, "approved": asset.approved}


@app.post("/creatives/{creative_id}/approve", dependencies=[AuthDep])
async def approve_creative(creative_id: int, session: AsyncSession = SessionDep) -> dict[str, Any]:
    asset = await session.get(CreativeAsset, creative_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Creative not found")
    asset.approved = True
    await session.commit()
    return {"id": asset.id, "approved": True}


# --- Launch -----------------------------------------------------------------------------------


async def _account_or_404(session: AsyncSession, account_id: int, platform: Platform) -> AdAccount:
    account = await session.get(AdAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ad account not found")
    if account.platform != platform:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ad account is {account.platform.value}, not {platform.value}")
    return account


@app.post("/ad-accounts/{account_id}/launch/meta", status_code=201, dependencies=[AuthDep])
async def launch_meta(account_id: int, payload: MetaLaunchIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    account = await _account_or_404(session, account_id, Platform.META)
    spec = MetaLaunchSpec(
        daily_budget_minor=payload.daily_budget_minor,
        countries=payload.countries,
        age_min=payload.age_min,
        age_max=payload.age_max,
        link_url=payload.link_url,
        cta=payload.cta,
    )
    campaign = await launch_meta_campaign(session, account, spec)
    return {"campaign_id": campaign.id, "external_id": campaign.external_id, "name": campaign.name}


@app.post("/ad-accounts/{account_id}/launch/google", status_code=201, dependencies=[AuthDep])
async def launch_google(account_id: int, payload: GoogleLaunchIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    account = await _account_or_404(session, account_id, Platform.GOOGLE)
    spec = GoogleLaunchSpec(
        daily_budget_minor=payload.daily_budget_minor,
        keywords=payload.keywords,
        final_url=payload.final_url,
        business_name=payload.business_name,
    )
    campaign = await launch_google_campaign(session, account, spec)
    return {"campaign_id": campaign.id, "external_id": campaign.external_id, "name": campaign.name}


# --- Reporting and operations -----------------------------------------------------------------


@app.get("/campaigns/{campaign_id}/performance", dependencies=[AuthDep])
async def campaign_performance(
    campaign_id: int,
    days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = SessionDep,
) -> dict[str, Any]:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    rows = (
        await session.execute(
            select(PerformanceSnapshot, Ad)
            .join(Ad, PerformanceSnapshot.ad_id == Ad.id)
            .join(AdSet, Ad.ad_set_id == AdSet.id)
            .where(AdSet.campaign_id == campaign.id, PerformanceSnapshot.snapshot_date >= since)
            .order_by(PerformanceSnapshot.snapshot_date, Ad.id)
        )
    ).all()
    return {
        "campaign": {
            "id": campaign.id,
            "name": campaign.name,
            "status": campaign.status,
            "daily_budget_minor": campaign.daily_budget_minor,
        },
        "snapshots": [
            {
                "ad": snapshot_ad.name,
                "angle": snapshot_ad.angle.value,
                "status": snapshot_ad.status,
                "date": snapshot.snapshot_date.isoformat(),
                "spend": str(snapshot.spend),
                "impressions": snapshot.impressions,
                "link_clicks": snapshot.link_clicks,
                "ctr": snapshot.ctr,
                "conversions": snapshot.conversions,
                "cpa": str(snapshot.cpa) if snapshot.cpa is not None else None,
                "roas": snapshot.roas,
                "hook_rate": snapshot.hook_rate,
                "hold_rate": snapshot.hold_rate,
            }
            for snapshot, snapshot_ad in rows
        ],
    }


@app.post("/ops/optimize", dependencies=[AuthDep])
async def optimize_now() -> dict[str, Any]:
    async with SessionLocal() as session:
        summary = await run_optimization_cycle(session, get_settings(), now=datetime.now(timezone.utc))
    return {
        "paused": summary.paused,
        "scaled": summary.scaled,
        "iterations_requested": summary.iterations_requested,
        "errors": summary.errors,
    }


@app.post("/ops/iterations", dependencies=[AuthDep])
async def generate_iterations() -> dict[str, int]:
    async with SessionLocal() as session:
        completed = await process_pending_iterations(session, get_settings())
    return {"completed": completed}
