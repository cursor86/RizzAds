"""Decision logic for the optimization loop. Pure functions with no I/O, so every rule is unit-testable.

Rules (see the tests for worked examples):
  * KILL  an ad that spent more than kill_cpa_multiple x target CPA with zero conversions.
  * KILL  an ad whose hook rate is below the minimum after the minimum number of impressions.
  * SCALE a campaign whose ROAS meets target and whose CPA is at or below target and stable,
          by exactly scale_step, outside the cooldown window.
  * ITERATE an angle whose ROAS is at least iteration_roas_multiple x the median of the other angles.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum


class DecisionType(str, Enum):
    PAUSE_AD = "pause_ad"
    SCALE_BUDGET = "scale_budget"
    REQUEST_ITERATION = "request_iteration"


@dataclass(frozen=True)
class DayMetrics:
    day: date
    spend: Decimal
    impressions: int
    conversions: int
    conversion_value: Decimal
    video_3s: int | None = None


@dataclass(frozen=True)
class Totals:
    spend: Decimal
    impressions: int
    conversions: int
    conversion_value: Decimal
    video_3s: int | None

    @property
    def cpa(self) -> Decimal | None:
        return self.spend / self.conversions if self.conversions else None

    @property
    def roas(self) -> Decimal | None:
        return self.conversion_value / self.spend if self.spend > 0 else None

    @property
    def hook_rate_pct(self) -> float | None:
        if self.video_3s is None or self.impressions == 0:
            return None
        return self.video_3s / self.impressions * 100


@dataclass(frozen=True)
class Thresholds:
    target_cpa: Decimal
    target_roas: Decimal
    kill_cpa_multiple: float
    kill_min_hook_impressions: int
    kill_min_hook_rate_pct: float
    scale_step: Decimal
    scale_cpa_stability_cv: float
    scale_min_conversion_days: int


@dataclass(frozen=True)
class Decision:
    type: DecisionType
    reason: str
    target_id: str | None = None
    angle: str | None = None
    new_budget_minor: int | None = None


def sum_days(days: list[DayMetrics]) -> Totals:
    video_values = [day.video_3s for day in days if day.video_3s is not None]
    return Totals(
        spend=sum((day.spend for day in days), Decimal("0")),
        impressions=sum(day.impressions for day in days),
        conversions=sum(day.conversions for day in days),
        conversion_value=sum((day.conversion_value for day in days), Decimal("0")),
        video_3s=sum(video_values) if video_values else None,
    )


def aggregate_by_day(days: list[DayMetrics]) -> list[DayMetrics]:
    buckets: dict[date, list[DayMetrics]] = defaultdict(list)
    for day in days:
        buckets[day.day].append(day)
    result = []
    for day, items in sorted(buckets.items()):
        totals = sum_days(items)
        result.append(
            DayMetrics(
                day=day,
                spend=totals.spend,
                impressions=totals.impressions,
                conversions=totals.conversions,
                conversion_value=totals.conversion_value,
                video_3s=totals.video_3s,
            )
        )
    return result


def evaluate_ad(ad_external_id: str, days: list[DayMetrics], th: Thresholds) -> Decision | None:
    totals = sum_days(days)

    spend_ceiling = th.target_cpa * Decimal(str(th.kill_cpa_multiple))
    if totals.conversions == 0 and totals.spend > spend_ceiling:
        return Decision(
            DecisionType.PAUSE_AD,
            reason=(
                f"spent {totals.spend} which exceeds {th.kill_cpa_multiple}x target CPA "
                f"({th.target_cpa}) with 0 conversions"
            ),
            target_id=ad_external_id,
        )

    hook = totals.hook_rate_pct
    if hook is not None and totals.impressions >= th.kill_min_hook_impressions and hook < th.kill_min_hook_rate_pct:
        return Decision(
            DecisionType.PAUSE_AD,
            reason=(
                f"hook rate {hook:.1f}% is below {th.kill_min_hook_rate_pct}% "
                f"after {totals.impressions} impressions"
            ),
            target_id=ad_external_id,
        )
    return None


def evaluate_campaign_scale(
    daily: list[DayMetrics],
    current_budget_minor: int,
    last_change_at: datetime | None,
    now: datetime,
    th: Thresholds,
    cooldown_hours: int,
) -> Decision | None:
    if last_change_at is not None and now - last_change_at < timedelta(hours=cooldown_hours):
        return None

    totals = sum_days(daily)
    if totals.roas is None or totals.roas < th.target_roas:
        return None
    if totals.cpa is None or totals.cpa > th.target_cpa:
        return None

    daily_cpas = [float(day.spend / day.conversions) for day in daily if day.conversions > 0]
    if len(daily_cpas) < th.scale_min_conversion_days:
        return None
    mean_cpa = statistics.fmean(daily_cpas)
    cpa_cv = statistics.pstdev(daily_cpas) / mean_cpa if mean_cpa else 0.0
    if cpa_cv > th.scale_cpa_stability_cv:
        return None

    new_budget = int(
        (Decimal(current_budget_minor) * (Decimal("1") + th.scale_step)).to_integral_value(rounding=ROUND_HALF_UP)
    )
    if new_budget <= current_budget_minor:
        return None

    return Decision(
        DecisionType.SCALE_BUDGET,
        reason=(
            f"ROAS {totals.roas:.2f} >= target {th.target_roas}, CPA {totals.cpa:.2f} <= target "
            f"{th.target_cpa}, daily CPA variation {cpa_cv:.2f} <= {th.scale_cpa_stability_cv}"
        ),
        new_budget_minor=new_budget,
    )


def evaluate_iteration(
    totals_by_angle: dict[str, Totals],
    *,
    min_conversions: int,
    roas_multiple: float,
    recent_angles: set[str],
) -> list[Decision]:
    decisions: list[Decision] = []
    for angle, totals in totals_by_angle.items():
        if angle in recent_angles or totals.conversions < min_conversions or totals.roas is None:
            continue
        baseline = [
            float(other.roas)
            for name, other in totals_by_angle.items()
            if name != angle and other.roas is not None
        ]
        if not baseline:
            continue
        median_roas = statistics.median(baseline)
        if median_roas <= 0 or float(totals.roas) < median_roas * roas_multiple:
            continue
        decisions.append(
            Decision(
                DecisionType.REQUEST_ITERATION,
                reason=(
                    f"angle {angle} ROAS {float(totals.roas):.2f} is at least {roas_multiple}x the "
                    f"median of other angles ({median_roas:.2f}) over {totals.conversions} conversions"
                ),
                angle=angle,
            )
        )
    return decisions
