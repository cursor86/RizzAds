"""Launches a Demand Gen campaign on YouTube, targeted by a keyword custom segment.

Each angle becomes one responsive video ad. Each ad carries all three orientations (horizontal, vertical, square),
the shared headline set, and that angle's description. The campaign starts paused and is enabled only after every
resource has been created. If a step fails, the campaign is paused.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, resolve_secret
from app.errors import DeployValidationError
from app.integrations.google_ads import GoogleAdsAdapter, build_google_client
from app.models import AdAccount, Brand, Campaign, Orientation
from app.services.bundles import build_google_bundle
from app.services.naming import ad_name, campaign_name
from app.services.persistence import LaunchedAd, load_approved_assets, record_launch

logger = logging.getLogger(__name__)

ORIENTATION_ORDER = (Orientation.HORIZONTAL, Orientation.VERTICAL, Orientation.SQUARE)
MAX_KEYWORDS = 200


@dataclass(frozen=True)
class GoogleLaunchSpec:
    daily_budget_minor: int
    keywords: list[str]
    final_url: str
    business_name: str

    def validate(self) -> None:
        if self.daily_budget_minor <= 0:
            raise DeployValidationError("daily_budget_minor must be positive")
        cleaned = [keyword.strip() for keyword in self.keywords if keyword.strip()]
        if not cleaned or len(cleaned) > MAX_KEYWORDS:
            raise DeployValidationError(f"Provide between 1 and {MAX_KEYWORDS} non-empty keywords")
        if not self.final_url.startswith("https://"):
            raise DeployValidationError("final_url must be an https URL")
        if not self.business_name.strip() or len(self.business_name) > 25:
            raise DeployValidationError("business_name must be 1-25 characters")


async def launch_google_campaign(
    session: AsyncSession,
    account: AdAccount,
    spec: GoogleLaunchSpec,
    now: datetime | None = None,
) -> Campaign:
    spec.validate()
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    brand = await session.get(Brand, account.brand_id)
    if brand is None:
        raise DeployValidationError(f"Brand {account.brand_id} does not exist")

    bundle = build_google_bundle(await load_approved_assets(session, brand.id))
    client = build_google_client(settings, resolve_secret(account.secret_ref))
    adapter = GoogleAdsAdapter(client, account.external_id)

    day = now.date()
    cname = campaign_name(brand.name, day)
    keywords = [keyword.strip() for keyword in spec.keywords if keyword.strip()]
    campaign_resource: str | None = None
    try:
        segment_resource = await adapter.create_custom_segment(f"{cname}_Intent", keywords)
        budget_resource = await adapter.create_budget(f"{cname}_budget_{now:%H%M%S}", spec.daily_budget_minor)
        campaign_resource = await adapter.create_campaign(cname, budget_resource)
        ad_group_name = f"{cname}_AdGroup"
        ad_group_resource = await adapter.create_ad_group(ad_group_name, campaign_resource)
        await adapter.attach_custom_segment(ad_group_resource, segment_resource)

        video_resources: list[str] = []
        for orientation in ORIENTATION_ORDER:
            asset = bundle.videos[orientation]
            video_resources.append(
                await adapter.create_youtube_video_asset(
                    f"{cname}_{orientation.value}", asset.google_youtube_video_id or ""
                )
            )

        headlines = [headline.text or "" for headline in bundle.headlines]
        video_asset_ids = [bundle.videos[o].id for o in ORIENTATION_ORDER]
        launched: list[LaunchedAd] = []
        for angle, description_asset in bundle.descriptions.items():
            ad_resource = await adapter.create_demand_gen_ad(
                ad_group_resource,
                video_resources,
                headlines,
                description_asset.text or "",
                spec.business_name,
                spec.final_url,
            )
            name = ad_name(angle.value, day, description_asset.id)
            asset_ids = [description_asset.id, *[h.id for h in bundle.headlines], *video_asset_ids]
            launched.append(LaunchedAd(external_id=ad_resource, name=name, angle=angle, creative_asset_ids=asset_ids))

        await adapter.set_campaign_status(campaign_resource, "ENABLED")
    except Exception:
        if campaign_resource is not None:
            try:
                await adapter.set_campaign_status(campaign_resource, "PAUSED")
            except Exception:
                logger.exception("Could not pause partially launched Google campaign %s", campaign_resource)
        await session.rollback()
        raise

    return await record_launch(
        session,
        account=account,
        campaign_external_id=campaign_resource,
        campaign_name=cname,
        daily_budget_minor=spec.daily_budget_minor,
        ad_set_external_id=ad_group_resource,
        ad_set_name=ad_group_name,
        age_min=None,
        age_max=None,
        countries=None,
        ads=launched,
        now=now,
    )
