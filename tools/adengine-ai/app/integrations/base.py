from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class SnapshotRow:
    """One ad's metrics for one day, normalised across platforms.

    video_3s and video_15s are None when the platform reports no video data for the ad (for example, a static image).
    """

    ad_external_id: str
    day: date
    spend: Decimal
    impressions: int
    link_clicks: int
    conversions: int
    conversion_value: Decimal
    video_3s: int | None = None
    video_15s: int | None = None


class AdPlatform(Protocol):
    async def fetch_daily_metrics(self, ad_external_ids: list[str], since: date, until: date) -> list[SnapshotRow]: ...

    async def pause_ad(self, ad_external_id: str) -> None: ...

    async def get_daily_budget(self, campaign_external_id: str) -> int:
        """Daily budget in minor currency units (cents, fils)."""
        ...

    async def set_daily_budget(self, campaign_external_id: str, minor_units: int) -> None: ...
