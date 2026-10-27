"""The 24-hour optimization cycle: sync metrics, pause losers, scale winners, and flag winning angles.

Each ad account is committed on its own, so a failure in one account rolls back only that account's database
changes and never blocks the others. Platform actions that already happened for an account are logged, so an
interrupted run can be audited.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings, resolve_secret
from app.integrations.base import AdPlatform, SnapshotRow
from app.integrations.google_ads import GoogleAdsAdapter, build_google_client
from app.integrations.meta import MetaAdapter, MetaClient
from app.models import (
    ActionLog,
    ActionType,
    Ad,
    AdAccount,
    AdSet,
    AngleTag,
    AssetKind,
    Campaign,
    CreativeAsset,
    IterationRequest,
    IterationStatus,
    PerformanceSnapshot,
    Platform,
)
from app.services.optimizer import (
    DayMetrics,
    DecisionType,
    Thresholds,
    Totals,
    aggregate_by_day,
    evaluate_ad,
    evaluate_campaign_scale,
    evaluate_iteration,
    sum_days,
)

logger = logging.getLogger(__name__)

Closer = Callable[[], Awaitable[None]]


@dataclass
class CycleSummary:
    paused: int = 0
    scaled: int = 0
    iterations_requested: int = 0
    errors: list[dict[str, object]] = field(default_factory=list)


async def open_adapter(account: AdAccount, settings: Settings) -> tuple[AdPlatform, Closer]:
    if account.platform == Platform.META:
        client = MetaClient(resolve_secret(account.secret_ref), settings.meta_api_version)
        return MetaAdapter(client, account.external_id), client.aclose
    if account.platform == Platform.GOOGLE:
        client = build_google_client(settings, resolve_secret(account.secret_ref))

        async def _nothing() -> None:
            return None

        return GoogleAdsAdapter(client, account.external_id), _nothing
    raise ValueError(f"Unsupported platform {account.platform}")


def _thresholds(account: AdAccount, settings: Settings) -> Thresholds:
    return Thresholds(
        target_cpa=Decimal(account.target_cpa),
        target_roas=Decimal(account.target_roas),
        kill_cpa_multiple=settings.kill_cpa_multiple,
        kill_min_hook_impressions=settings.kill_min_hook_impressions,
        kill_min_hook_rate_pct=settings.kill_min_hook_rate_pct,
        scale_step=Decimal(str(settings.scale_step)),
        scale_cpa_stability_cv=settings.scale_cpa_stability_cv,
        scale_min_conversion_days=settings.scale_min_conversion_days,
    )


def _rates(row: SnapshotRow, video_3s: int | None, video_15s: int | None) -> dict[str, float | Decimal | None]:
    impressions = row.impressions
    return {
        "ctr": row.link_clicks / impressions * 100 if impressions else None,
        "cpa": row.spend / row.conversions if row.conversions else None,
        "roas": float(row.conversion_value / row.spend) if row.spend > 0 else None,
        "hook_rate": video_3s / impressions * 100 if video_3s is not None and impressions else None,
        "hold_rate": video_15s / impressions * 100 if video_15s is not None and impressions else None,
    }


async def _upsert_snapshot(session: AsyncSession, ad: Ad, row: SnapshotRow, is_video: bool) -> None:
    snapshot = await session.scalar(
        select(PerformanceSnapshot).where(
            PerformanceSnapshot.ad_id == ad.id,
            PerformanceSnapshot.snapshot_date == row.day,
        )
    )
    if snapshot is None:
        snapshot = PerformanceSnapshot(ad_id=ad.id, snapshot_date=row.day)
        session.add(snapshot)

    # A video ad with no 3-second views reports no field at all. Treat that as zero, not as unknown.
    video_3s = row.video_3s if row.video_3s is not None else (0 if is_video else None)
    video_15s = row.video_15s if row.video_15s is not None else (0 if is_video else None)
    rates = _rates(row, video_3s, video_15s)

    snapshot.spend = row.spend
    snapshot.impressions = row.impressions
    snapshot.link_clicks = row.link_clicks
    snapshot.conversions = row.conversions
    snapshot.conversion_value = row.conversion_value
    snapshot.video_3s = video_3s
    snapshot.video_15s = video_15s
    snapshot.ctr = rates["ctr"]
    snapshot.cpa = rates["cpa"]
    snapshot.roas = rates["roas"]
    snapshot.hook_rate = rates["hook_rate"]
    snapshot.hold_rate = rates["hold_rate"]


async def _creative_kinds(session: AsyncSession, asset_ids: set[int]) -> dict[int, AssetKind]:
    if not asset_ids:
        return {}
    rows = await session.execute(
        select(CreativeAsset.id, CreativeAsset.kind).where(CreativeAsset.id.in_(asset_ids))
    )
    return {asset_id: kind for asset_id, kind in rows.all()}


def _to_day_metrics(row: SnapshotRow, is_video: bool) -> DayMetrics:
    video_3s = row.video_3s if row.video_3s is not None else (0 if is_video else None)
    return DayMetrics(
        day=row.day,
        spend=row.spend,
        impressions=row.impressions,
        conversions=row.conversions,
        conversion_value=row.conversion_value,
        video_3s=video_3s,
    )


async def _optimize_account(
    session: AsyncSession,
    account: AdAccount,
    settings: Settings,
    since: date,
    until: date,
    now: datetime,
    angle_days: dict[int, dict[AngleTag, list[DayMetrics]]],
    summary: CycleSummary,
) -> None:
    campaigns = (
        await session.scalars(
            select(Campaign).where(Campaign.ad_account_id == account.id, Campaign.status == "active")
        )
    ).all()
    if not campaigns:
        return

    ad_rows = (
        await session.execute(
            select(Ad, Campaign)
            .join(AdSet, Ad.ad_set_id == AdSet.id)
            .join(Campaign, AdSet.campaign_id == Campaign.id)
            .where(Campaign.ad_account_id == account.id, Ad.status == "active")
        )
    ).all()
    if not ad_rows:
        return

    all_asset_ids = {asset_id for ad, _ in ad_rows for asset_id in ad.creative_asset_ids}
    kinds = await _creative_kinds(session, all_asset_ids)
    ads_by_external: dict[str, tuple[Ad, Campaign, bool]] = {}
    for ad, campaign in ad_rows:
        is_video = any(kinds.get(asset_id) == AssetKind.VIDEO for asset_id in ad.creative_asset_ids)
        ads_by_external[ad.external_id] = (ad, campaign, is_video)

    adapter, close = await open_adapter(account, settings)
    try:
        rows = await adapter.fetch_daily_metrics(list(ads_by_external), since, until)
        per_ad: dict[str, list[DayMetrics]] = defaultdict(list)
        for row in rows:
            if row.ad_external_id not in ads_by_external:
                continue
            ad, _, is_video = ads_by_external[row.ad_external_id]
            await _upsert_snapshot(session, ad, row, is_video)
            metrics = _to_day_metrics(row, is_video)
            per_ad[row.ad_external_id].append(metrics)
            angle_days[account.brand_id][ad.angle].append(metrics)

        thresholds = _thresholds(account, settings)
        paused: set[str] = set()
        for external_id, days in per_ad.items():
            ad, campaign, _ = ads_by_external[external_id]
            decision = evaluate_ad(external_id, days, thresholds)
            if decision is None or decision.type != DecisionType.PAUSE_AD:
                continue
            await adapter.pause_ad(external_id)
            ad.status = "paused"
            ad.paused_reason = decision.reason
            session.add(
                ActionLog(
                    ad_id=ad.id,
                    campaign_id=campaign.id,
                    action=ActionType.PAUSE_AD,
                    detail={"external_id": external_id, "reason": decision.reason},
                )
            )
            paused.add(external_id)
            summary.paused += 1
            logger.info("Paused ad %s: %s", external_id, decision.reason)

        cooldown = settings.scale_cooldown_hours
        step = Decimal(str(settings.scale_step))
        for campaign in campaigns:
            live_ads = [
                ext for ext, (_, c, _) in ads_by_external.items() if c.id == campaign.id and ext not in paused
            ]
            campaign_days = [m for ext in live_ads for m in per_ad.get(ext, [])]
            if not campaign_days:
                continue
            current_budget = await adapter.get_daily_budget(campaign.external_id)
            decision = evaluate_campaign_scale(
                aggregate_by_day(campaign_days),
                current_budget,
                campaign.last_budget_change_at,
                now,
                thresholds,
                cooldown,
            )
            if decision is None or decision.new_budget_minor is None:
                continue
            await adapter.set_daily_budget(campaign.external_id, decision.new_budget_minor)
            campaign.daily_budget_minor = decision.new_budget_minor
            campaign.last_budget_change_at = now
            session.add(
                ActionLog(
                    campaign_id=campaign.id,
                    action=ActionType.SCALE_BUDGET,
                    detail={
                        "external_campaign_id": campaign.external_id,
                        "from_minor": current_budget,
                        "to_minor": decision.new_budget_minor,
                        "step": str(step),
                        "reason": decision.reason,
                    },
                )
            )
            summary.scaled += 1
            logger.info("Scaled campaign %s from %s to %s", campaign.external_id, current_budget, decision.new_budget_minor)
    finally:
        await close()


async def _request_iterations(
    session: AsyncSession,
    settings: Settings,
    angle_days: dict[int, dict[AngleTag, list[DayMetrics]]],
    now: datetime,
    summary: CycleSummary,
) -> None:
    cutoff = now - timedelta(days=settings.iteration_cooldown_days)
    for brand_id, by_angle in angle_days.items():
        totals: dict[str, Totals] = {angle.value: sum_days(days) for angle, days in by_angle.items()}
        recent = set(
            (
                await session.scalars(
                    select(IterationRequest.angle).where(
                        IterationRequest.brand_id == brand_id,
                        IterationRequest.created_at >= cutoff,
                    )
                )
            ).all()
        )
        recent_values = {angle.value for angle in recent}
        for decision in evaluate_iteration(
            totals,
            min_conversions=settings.iteration_min_conversions,
            roas_multiple=settings.iteration_roas_multiple,
            recent_angles=recent_values,
        ):
            angle = AngleTag(decision.angle)
            session.add(
                IterationRequest(
                    brand_id=brand_id,
                    angle=angle,
                    reason=decision.reason,
                    status=IterationStatus.PENDING,
                )
            )
            session.add(
                ActionLog(
                    action=ActionType.REQUEST_ITERATION,
                    detail={"brand_id": brand_id, "angle": angle.value, "reason": decision.reason},
                )
            )
            summary.iterations_requested += 1
            logger.info("Requested iteration for brand %s angle %s", brand_id, angle.value)


async def run_optimization_cycle(
    session: AsyncSession,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> CycleSummary:
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    until = now.date() - timedelta(days=1)  # yesterday, so partial-day data is never used
    since = until - timedelta(days=settings.lookback_days - 1)

    accounts = (await session.scalars(select(AdAccount).order_by(AdAccount.id))).all()
    summary = CycleSummary()
    angle_days: dict[int, dict[AngleTag, list[DayMetrics]]] = defaultdict(lambda: defaultdict(list))

    for account in accounts:
        try:
            await _optimize_account(session, account, settings, since, until, now, angle_days, summary)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.exception("Optimization failed for ad account %s", account.id)
            summary.errors.append({"ad_account_id": account.id, "error": str(exc)})

    await _request_iterations(session, settings, angle_days, now, summary)
    await session.commit()
    return summary
