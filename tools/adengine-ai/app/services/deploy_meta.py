"""Launches a Meta CBO conversions campaign with one broad ad set and one ad per creative angle.

Launch order: campaign (paused) -> ad set (paused) -> media upload -> creatives -> ads (paused) -> activate.
If anything fails after the campaign exists, the campaign is paused so no budget is spent on a partial launch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, resolve_secret
from app.errors import DeployValidationError
from app.integrations.meta import MetaAdapter, MetaClient
from app.models import AdAccount, AssetKind, Brand, Campaign, CreativeAsset
from app.services.bundles import build_meta_bundle
from app.services.naming import ad_name, campaign_name
from app.services.persistence import LaunchedAd, load_approved_assets, record_launch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetaLaunchSpec:
    daily_budget_minor: int
    countries: list[str]
    age_min: int
    age_max: int
    link_url: str
    cta: str = "SHOP_NOW"

    def validate(self) -> None:
        if self.daily_budget_minor <= 0:
            raise DeployValidationError("daily_budget_minor must be positive")
        if not self.countries or any(len(code) != 2 or not code.isupper() for code in self.countries):
            raise DeployValidationError("countries must be a non-empty list of uppercase ISO 3166-1 alpha-2 codes")
        if not (13 <= self.age_min <= self.age_max <= 65):
            raise DeployValidationError("age range must satisfy 13 <= age_min <= age_max <= 65")
        if not self.link_url.startswith("https://"):
            raise DeployValidationError("link_url must be an https URL")


async def _ensure_meta_media(session: AsyncSession, adapter: MetaAdapter, visuals: list[CreativeAsset]) -> list[str]:
    """Uploads each visual once, caches the Meta media id on the asset, and returns the ids in matrix order."""
    refs: list[str] = []
    for asset in visuals:
        if asset.meta_media_id is None:
            if asset.kind == AssetKind.IMAGE:
                asset.meta_media_id = await adapter.upload_image(asset.media_url or "")
            else:
                video_id = await adapter.upload_video(asset.media_url or "")
                await adapter.wait_until_video_ready(video_id)
                asset.meta_media_id = video_id
            session.add(asset)
        refs.append(asset.meta_media_id)
    return refs


async def launch_meta_campaign(
    session: AsyncSession,
    account: AdAccount,
    spec: MetaLaunchSpec,
    now: datetime | None = None,
) -> Campaign:
    spec.validate()
    if account.page_id is None or account.pixel_id is None:
        raise DeployValidationError("Meta ad accounts need page_id and pixel_id before launching")

    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    brand = await session.get(Brand, account.brand_id)
    if brand is None:
        raise DeployValidationError(f"Brand {account.brand_id} does not exist")

    bundle = build_meta_bundle(await load_approved_assets(session, brand.id))
    client = MetaClient(resolve_secret(account.secret_ref), settings.meta_api_version)
    adapter = MetaAdapter(client, account.external_id)

    day = now.date()
    cname = campaign_name(brand.name, day)
    campaign_id: str | None = None
    try:
        campaign_id = await adapter.create_campaign(cname, spec.daily_budget_minor)
        ad_set_name = f"{cname}_AdSet"
        ad_set_id = await adapter.create_ad_set(
            campaign_id, ad_set_name, account.pixel_id, spec.countries, spec.age_min, spec.age_max
        )

        visual_refs = await _ensure_meta_media(session, adapter, bundle.visuals)
        is_video = bundle.visuals[0].kind == AssetKind.VIDEO
        headlines = [headline.text or "" for headline in bundle.headlines]

        launched: list[LaunchedAd] = []
        for angle, body in bundle.bodies.items():
            creative_name = f"{cname}_{angle.value}_creative"
            creative_id = await adapter.create_creative(
                creative_name,
                account.page_id,
                bodies=[body.text or ""],
                titles=headlines,
                link_url=spec.link_url,
                cta=spec.cta,
                image_hashes=None if is_video else visual_refs,
                video_ids=visual_refs if is_video else None,
            )
            name = ad_name(angle.value, day, body.id)
            ad_id = await adapter.create_ad(ad_set_id, name, creative_id)
            asset_ids = [body.id, *[h.id for h in bundle.headlines], *[v.id for v in bundle.visuals]]
            launched.append(LaunchedAd(external_id=ad_id, name=name, angle=angle, creative_asset_ids=asset_ids))

        await adapter.set_status(campaign_id, "ACTIVE")
    except Exception:
        if campaign_id is not None:
            try:
                await adapter.set_status(campaign_id, "PAUSED")
            except Exception:
                logger.exception("Could not pause partially launched Meta campaign %s", campaign_id)
        await session.rollback()
        raise
    finally:
        await client.aclose()

    return await record_launch(
        session,
        account=account,
        campaign_external_id=campaign_id,
        campaign_name=cname,
        daily_budget_minor=spec.daily_budget_minor,
        ad_set_external_id=ad_set_id,
        ad_set_name=ad_set_name,
        age_min=spec.age_min,
        age_max=spec.age_max,
        countries=spec.countries,
        ads=launched,
        now=now,
    )
