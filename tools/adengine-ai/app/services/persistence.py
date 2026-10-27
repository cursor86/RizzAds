"""Reads approved creative and writes the local record of a launch after the platform has accepted it."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActionLog, ActionType, Ad, AdAccount, AdSet, AngleTag, Campaign, CreativeAsset


@dataclass(frozen=True)
class LaunchedAd:
    external_id: str
    name: str
    angle: AngleTag
    creative_asset_ids: list[int]


async def load_approved_assets(session: AsyncSession, brand_id: int) -> list[CreativeAsset]:
    result = await session.scalars(
        select(CreativeAsset)
        .where(CreativeAsset.brand_id == brand_id, CreativeAsset.approved.is_(True))
        .order_by(CreativeAsset.id)
    )
    return list(result.all())


async def record_launch(
    session: AsyncSession,
    *,
    account: AdAccount,
    campaign_external_id: str,
    campaign_name: str,
    daily_budget_minor: int,
    ad_set_external_id: str,
    ad_set_name: str,
    age_min: int | None,
    age_max: int | None,
    countries: list[str] | None,
    ads: list[LaunchedAd],
    now: datetime,
) -> Campaign:
    campaign = Campaign(
        ad_account_id=account.id,
        external_id=campaign_external_id,
        name=campaign_name,
        status="active",
        daily_budget_minor=daily_budget_minor,
        last_budget_change_at=None,
    )
    session.add(campaign)
    await session.flush()

    ad_set = AdSet(
        campaign_id=campaign.id,
        external_id=ad_set_external_id,
        name=ad_set_name,
        age_min=age_min,
        age_max=age_max,
        countries=countries,
    )
    session.add(ad_set)
    await session.flush()

    for launched in ads:
        session.add(
            Ad(
                ad_set_id=ad_set.id,
                external_id=launched.external_id,
                name=launched.name,
                angle=launched.angle,
                creative_asset_ids=launched.creative_asset_ids,
                status="active",
            )
        )

    session.add(
        ActionLog(
            campaign_id=campaign.id,
            action=ActionType.LAUNCH,
            detail={
                "external_campaign_id": campaign_external_id,
                "daily_budget_minor": daily_budget_minor,
                "ads": [a.name for a in ads],
                "launched_at": now.isoformat(),
            },
        )
    )
    await session.commit()
    return campaign
