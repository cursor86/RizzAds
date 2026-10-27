from datetime import date

import pytest

from app.services.naming import AD_NAME_PATTERN, ad_name, campaign_name


def test_ad_name_follows_convention():
    name = ad_name("pain_point", date(2026, 10, 27), 42)
    assert name == "AdEngine_PainPoint_20261027_CR42"
    assert AD_NAME_PATTERN.match(name)


@pytest.mark.parametrize(
    ("angle", "label"),
    [("benefit", "Benefit"), ("social_proof", "SocialProof")],
)
def test_angle_labels(angle, label):
    assert ad_name(angle, date(2026, 1, 2), 1) == f"AdEngine_{label}_20260102_CR1"


def test_unknown_angle_is_rejected():
    with pytest.raises(ValueError):
        ad_name("urgency", date(2026, 1, 2), 1)


def test_creative_id_must_be_positive():
    with pytest.raises(ValueError):
        ad_name("benefit", date(2026, 1, 2), 0)


def test_campaign_name_strips_unsafe_characters():
    assert campaign_name("Elite Club!", date(2026, 10, 27)) == "AdEngine_EliteClub_20261027"


def test_campaign_name_rejects_empty_slug():
    with pytest.raises(ValueError):
        campaign_name("!!!", date(2026, 10, 27))
