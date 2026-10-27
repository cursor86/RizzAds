from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

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

TH = Thresholds(
    target_cpa=Decimal("20"),
    target_roas=Decimal("2"),
    kill_cpa_multiple=2.0,
    kill_min_hook_impressions=2000,
    kill_min_hook_rate_pct=15.0,
    scale_step=Decimal("0.15"),
    scale_cpa_stability_cv=0.25,
    scale_min_conversion_days=3,
)
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def day(offset: int, **kw) -> DayMetrics:
    base = dict(
        day=date(2026, 10, 1) + timedelta(days=offset),
        spend=Decimal("0"),
        impressions=0,
        conversions=0,
        conversion_value=Decimal("0"),
        video_3s=None,
    )
    base.update(kw)
    return DayMetrics(**base)


# --- Kill rules -------------------------------------------------------------------------------


def test_kills_ad_that_spent_over_twice_target_cpa_with_no_conversions():
    days = [day(0, spend=Decimal("41"), impressions=5000)]
    decision = evaluate_ad("ad-1", days, TH)
    assert decision is not None
    assert decision.type == DecisionType.PAUSE_AD
    assert decision.target_id == "ad-1"


def test_does_not_kill_at_exactly_twice_target_cpa():
    days = [day(0, spend=Decimal("40"), impressions=5000)]
    assert evaluate_ad("ad-1", days, TH) is None


def test_does_not_kill_when_ad_has_converted():
    days = [day(0, spend=Decimal("100"), impressions=5000, conversions=1, conversion_value=Decimal("200"))]
    assert evaluate_ad("ad-1", days, TH) is None


def test_kills_ad_with_low_hook_rate_after_2000_impressions():
    days = [day(0, spend=Decimal("5"), impressions=2000, video_3s=299)]
    decision = evaluate_ad("ad-2", days, TH)
    assert decision is not None
    assert decision.type == DecisionType.PAUSE_AD
    assert "hook rate" in decision.reason


def test_hook_rule_waits_for_minimum_impressions():
    days = [day(0, spend=Decimal("5"), impressions=1999, video_3s=0)]
    assert evaluate_ad("ad-2", days, TH) is None


def test_hook_rule_skips_non_video_ads():
    days = [day(0, spend=Decimal("5"), impressions=5000, video_3s=None)]
    assert evaluate_ad("ad-3", days, TH) is None


def test_hook_rate_exactly_15_percent_survives():
    days = [day(0, spend=Decimal("5"), impressions=2000, video_3s=300)]
    assert evaluate_ad("ad-4", days, TH) is None


# --- Scale rule -------------------------------------------------------------------------------


def _stable_winning_days() -> list[DayMetrics]:
    # Three days, each with CPA exactly 10 (zero variation), ROAS 3.0.
    return [
        day(i, spend=Decimal("50"), impressions=3000, conversions=5, conversion_value=Decimal("150"))
        for i in range(3)
    ]


def test_scales_budget_by_exactly_15_percent():
    decision = evaluate_campaign_scale(_stable_winning_days(), 10_000, None, NOW, TH, cooldown_hours=72)
    assert decision is not None
    assert decision.type == DecisionType.SCALE_BUDGET
    assert decision.new_budget_minor == 11_500


def test_scale_respects_cooldown():
    recent = NOW - timedelta(hours=10)
    assert evaluate_campaign_scale(_stable_winning_days(), 10_000, recent, NOW, TH, cooldown_hours=72) is None


def test_scale_allowed_after_cooldown_expires():
    old = NOW - timedelta(hours=73)
    assert evaluate_campaign_scale(_stable_winning_days(), 10_000, old, NOW, TH, cooldown_hours=72) is not None


def test_no_scale_when_roas_below_target():
    days = [
        day(i, spend=Decimal("50"), impressions=3000, conversions=5, conversion_value=Decimal("80"))
        for i in range(3)
    ]
    assert evaluate_campaign_scale(days, 10_000, None, NOW, TH, cooldown_hours=72) is None


def test_no_scale_when_cpa_is_volatile():
    days = [
        day(0, spend=Decimal("50"), impressions=3000, conversions=10, conversion_value=Decimal("300")),
        day(1, spend=Decimal("50"), impressions=3000, conversions=1, conversion_value=Decimal("300")),
        day(2, spend=Decimal("50"), impressions=3000, conversions=5, conversion_value=Decimal("300")),
    ]
    assert evaluate_campaign_scale(days, 10_000, None, NOW, TH, cooldown_hours=72) is None


def test_no_scale_without_enough_converting_days():
    days = _stable_winning_days()[:2]
    assert evaluate_campaign_scale(days, 10_000, None, NOW, TH, cooldown_hours=72) is None


# --- Iteration rule ---------------------------------------------------------------------------


def _totals(roas: str, conversions: int = 25) -> Totals:
    spend = Decimal("100")
    return Totals(
        spend=spend,
        impressions=10_000,
        conversions=conversions,
        conversion_value=spend * Decimal(roas),
        video_3s=None,
    )


def test_flags_angle_outperforming_median_by_1_5x():
    totals = {"pain_point": _totals("3.0"), "benefit": _totals("1.5"), "social_proof": _totals("1.6")}
    decisions = evaluate_iteration(totals, min_conversions=20, roas_multiple=1.5, recent_angles=set())
    assert [d.angle for d in decisions] == ["pain_point"]
    assert decisions[0].type == DecisionType.REQUEST_ITERATION


def test_does_not_flag_when_margin_is_below_threshold():
    totals = {"pain_point": _totals("2.0"), "benefit": _totals("1.5"), "social_proof": _totals("1.6")}
    assert evaluate_iteration(totals, min_conversions=20, roas_multiple=1.5, recent_angles=set()) == []


def test_does_not_flag_below_minimum_conversions():
    totals = {"pain_point": _totals("3.0", conversions=5), "benefit": _totals("1.0"), "social_proof": _totals("1.0")}
    assert evaluate_iteration(totals, min_conversions=20, roas_multiple=1.5, recent_angles=set()) == []


def test_does_not_flag_angle_already_requested_recently():
    totals = {"pain_point": _totals("3.0"), "benefit": _totals("1.0"), "social_proof": _totals("1.0")}
    assert evaluate_iteration(totals, min_conversions=20, roas_multiple=1.5, recent_angles={"pain_point"}) == []


# --- Aggregation ------------------------------------------------------------------------------


def test_aggregate_by_day_sums_ads_per_day():
    rows = [
        day(0, spend=Decimal("10"), impressions=100, conversions=1, conversion_value=Decimal("20")),
        day(0, spend=Decimal("5"), impressions=50, conversions=0, conversion_value=Decimal("0")),
        day(1, spend=Decimal("7"), impressions=70, conversions=1, conversion_value=Decimal("14")),
    ]
    daily = aggregate_by_day(rows)
    assert [d.day for d in daily] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert daily[0].spend == Decimal("15")
    assert daily[0].conversions == 1


def test_sum_days_keeps_video_none_when_no_video_data():
    totals = sum_days([day(0, spend=Decimal("1"), impressions=10)])
    assert totals.video_3s is None
    assert totals.hook_rate_pct is None
