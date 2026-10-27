"""Strict naming convention: [AdEngine]_[AngleType]_[Date]_[CreativeID]. Pure functions, no I/O."""

import re
from datetime import date

ANGLE_LABELS = {
    "pain_point": "PainPoint",
    "benefit": "Benefit",
    "social_proof": "SocialProof",
}

AD_NAME_PATTERN = re.compile(r"^AdEngine_(PainPoint|Benefit|SocialProof)_\d{8}_CR\d+$")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def angle_label(angle: str) -> str:
    try:
        return ANGLE_LABELS[angle]
    except KeyError as exc:
        raise ValueError(f"Unknown angle tag: {angle!r}") from exc


def ad_name(angle: str, day: date, creative_id: int) -> str:
    if creative_id <= 0:
        raise ValueError("creative_id must be a positive database id")
    name = f"AdEngine_{angle_label(angle)}_{day:%Y%m%d}_CR{creative_id}"
    if not AD_NAME_PATTERN.match(name):  # pragma: no cover - guards the format above
        raise ValueError(f"Generated ad name violates the naming convention: {name}")
    return name


def campaign_name(brand: str, day: date) -> str:
    slug = _NON_ALNUM.sub("", brand)
    if not slug:
        raise ValueError("Brand name has no characters usable in a campaign name")
    return f"AdEngine_{slug}_{day:%Y%m%d}"
