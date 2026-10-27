"""Google Ads API adapter: keyword custom segment, Demand Gen campaign, responsive video ads, and optimization reads.

The google-ads client is synchronous gRPC, so every call runs in a worker thread and goes through the same
backoff wrapper as the Meta client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, TypeVar

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

from app.config import Settings
from app.errors import ExternalAPIError
from app.integrations.base import SnapshotRow
from app.integrations.retry import call_with_backoff

T = TypeVar("T")

MICROS_PER_MINOR_UNIT = 10_000  # 1 cent or fils = 0.01 currency units = 10,000 micros
RATE_LIMIT_WAIT_SECONDS = 60.0
TRANSIENT_GRPC_CODES = frozenset({"UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "UNKNOWN"})
SEARCH_CHUNK_SIZE = 200


def google_retry_hint(exc: BaseException) -> float | None:
    if not isinstance(exc, GoogleAdsException):
        return None
    code = exc.error.code().name
    if code == "RESOURCE_EXHAUSTED":
        return RATE_LIMIT_WAIT_SECONDS
    if code in TRANSIENT_GRPC_CODES:
        return 0.0
    return None


def build_google_client(settings: Settings, refresh_token: str) -> GoogleAdsClient:
    config: dict[str, Any] = {
        "developer_token": settings.google_developer_token,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    if settings.google_login_customer_id:
        config["login_customer_id"] = settings.google_login_customer_id
    return GoogleAdsClient.load_from_dict(config)


def _customer(customer_id: str) -> str:
    return customer_id.replace("-", "")


class GoogleAdsAdapter:
    def __init__(self, client: GoogleAdsClient, customer_id: str) -> None:
        self._client = client
        self._customer_id = _customer(customer_id)

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        try:
            return await call_with_backoff(lambda: asyncio.to_thread(fn, *args), retry_hint=google_retry_hint)
        except GoogleAdsException as exc:
            messages = "; ".join(error.message for error in exc.failure.errors) or exc.error.code().name
            raise ExternalAPIError(f"Google Ads API error {exc.error.code().name}: {messages}") from exc

    def _enum(self, enum_name: str, value_name: str, value: str) -> Any:
        return getattr(getattr(getattr(self._client.enums, enum_name), value_name), value)

    def _search(self, query: str) -> list[Any]:
        service = self._client.get_service("GoogleAdsService")
        rows: list[Any] = []
        for batch in service.search_stream(customer_id=self._customer_id, query=query):
            rows.extend(batch.results)
        return rows

    # --- Deploy -----------------------------------------------------------------------------

    def _create_custom_segment(self, name: str, keywords: list[str]) -> str:
        operation = self._client.get_type("CustomAudienceOperation")
        audience = operation.create
        audience.name = name
        audience.description = f"AdEngine intent segment: {name}"
        audience.type_ = self._enum("CustomAudienceTypeEnum", "CustomAudienceType", "INTENT")
        for keyword in keywords:
            member = audience.members.add()
            member.member_type = self._enum("CustomAudienceMemberTypeEnum", "CustomAudienceMemberType", "KEYWORD")
            member.keyword = keyword
        response = self._client.get_service("CustomAudienceService").mutate_custom_audiences(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_custom_segment(self, name: str, keywords: list[str]) -> str:
        return await self._run(self._create_custom_segment, name, keywords)

    def _create_budget(self, name: str, amount_micros: int) -> str:
        operation = self._client.get_type("CampaignBudgetOperation")
        budget = operation.create
        budget.name = name
        budget.amount_micros = amount_micros
        budget.delivery_method = self._enum("BudgetDeliveryMethodEnum", "BudgetDeliveryMethod", "STANDARD")
        budget.explicitly_shared = False
        response = self._client.get_service("CampaignBudgetService").mutate_campaign_budgets(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_budget(self, name: str, daily_budget_minor: int) -> str:
        return await self._run(self._create_budget, name, daily_budget_minor * MICROS_PER_MINOR_UNIT)

    def _create_campaign(self, name: str, budget_resource: str) -> str:
        operation = self._client.get_type("CampaignOperation")
        campaign = operation.create
        campaign.name = name
        campaign.status = self._enum("CampaignStatusEnum", "CampaignStatus", "PAUSED")
        campaign.advertising_channel_type = self._enum(
            "AdvertisingChannelTypeEnum", "AdvertisingChannelType", "DEMAND_GEN"
        )
        campaign.campaign_budget = budget_resource
        campaign.bidding_strategy_type = self._enum(
            "BiddingStrategyTypeEnum", "BiddingStrategyType", "MAXIMIZE_CONVERSIONS"
        )
        campaign.contains_eu_political_advertising = self._enum(
            "EuPoliticalAdvertisingStatusEnum",
            "EuPoliticalAdvertisingStatus",
            "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        )
        response = self._client.get_service("CampaignService").mutate_campaigns(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_campaign(self, name: str, budget_resource: str) -> str:
        return await self._run(self._create_campaign, name, budget_resource)

    def _create_ad_group(self, name: str, campaign_resource: str) -> str:
        operation = self._client.get_type("AdGroupOperation")
        ad_group = operation.create
        ad_group.name = name
        ad_group.campaign = campaign_resource
        ad_group.status = self._enum("AdGroupStatusEnum", "AdGroupStatus", "ENABLED")
        ad_group.type_ = self._enum("AdGroupTypeEnum", "AdGroupType", "DEMAND_GEN_MULTI_ASSET_AD_GROUP")
        response = self._client.get_service("AdGroupService").mutate_ad_groups(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_ad_group(self, name: str, campaign_resource: str) -> str:
        return await self._run(self._create_ad_group, name, campaign_resource)

    def _attach_custom_segment(self, ad_group_resource: str, segment_resource: str) -> None:
        operation = self._client.get_type("AdGroupCriterionOperation")
        criterion = operation.create
        criterion.ad_group = ad_group_resource
        criterion.status = self._enum("AdGroupCriterionStatusEnum", "AdGroupCriterionStatus", "ENABLED")
        criterion.custom_audience.custom_audience = segment_resource
        self._client.get_service("AdGroupCriterionService").mutate_ad_group_criteria(
            customer_id=self._customer_id, operations=[operation]
        )

    async def attach_custom_segment(self, ad_group_resource: str, segment_resource: str) -> None:
        await self._run(self._attach_custom_segment, ad_group_resource, segment_resource)

    def _create_youtube_video_asset(self, name: str, youtube_video_id: str) -> str:
        operation = self._client.get_type("AssetOperation")
        asset = operation.create
        asset.name = name
        asset.youtube_video_asset.youtube_video_id = youtube_video_id
        response = self._client.get_service("AssetService").mutate_assets(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_youtube_video_asset(self, name: str, youtube_video_id: str) -> str:
        return await self._run(self._create_youtube_video_asset, name, youtube_video_id)

    def _create_demand_gen_ad(
        self,
        ad_group_resource: str,
        video_resources: list[str],
        headlines: list[str],
        description: str,
        business_name: str,
        final_url: str,
    ) -> str:
        operation = self._client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.create
        ad_group_ad.ad_group = ad_group_resource
        ad_group_ad.status = self._enum("AdGroupAdStatusEnum", "AdGroupAdStatus", "ENABLED")
        ad_group_ad.ad.final_urls.append(final_url)
        responsive = ad_group_ad.ad.demand_gen_video_responsive_ad
        for resource in video_resources:
            responsive.videos.add().asset = resource
        for headline in headlines:
            responsive.headlines.add().text = headline
        responsive.descriptions.add().text = description
        responsive.business_name = business_name
        response = self._client.get_service("AdGroupAdService").mutate_ad_group_ads(
            customer_id=self._customer_id, operations=[operation]
        )
        return response.results[0].resource_name

    async def create_demand_gen_ad(
        self,
        ad_group_resource: str,
        video_resources: list[str],
        headlines: list[str],
        description: str,
        business_name: str,
        final_url: str,
    ) -> str:
        return await self._run(
            self._create_demand_gen_ad,
            ad_group_resource,
            video_resources,
            headlines,
            description,
            business_name,
            final_url,
        )

    def _set_campaign_status(self, campaign_resource: str, status: str) -> None:
        operation = self._client.get_type("CampaignOperation")
        operation.update.resource_name = campaign_resource
        operation.update.status = self._enum("CampaignStatusEnum", "CampaignStatus", status)
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        self._client.get_service("CampaignService").mutate_campaigns(
            customer_id=self._customer_id, operations=[operation]
        )

    async def set_campaign_status(self, campaign_resource: str, status: str) -> None:
        await self._run(self._set_campaign_status, campaign_resource, status)

    # --- Optimization (AdPlatform) -----------------------------------------------------------

    def _fetch_metrics(self, ad_resources: list[str], since: date, until: date) -> list[SnapshotRow]:
        quoted = ", ".join(f"'{resource}'" for resource in ad_resources)
        query = (
            "SELECT ad_group_ad.resource_name, segments.date, metrics.cost_micros, metrics.impressions, "
            "metrics.clicks, metrics.conversions, metrics.conversions_value "
            "FROM ad_group_ad "
            f"WHERE segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}' "
            f"AND ad_group_ad.resource_name IN ({quoted})"
        )
        rows: list[SnapshotRow] = []
        for row in self._search(query):
            rows.append(
                SnapshotRow(
                    ad_external_id=row.ad_group_ad.resource_name,
                    day=date.fromisoformat(row.segments.date),
                    spend=Decimal(row.metrics.cost_micros) / Decimal(1_000_000),
                    impressions=int(row.metrics.impressions),
                    link_clicks=int(row.metrics.clicks),
                    conversions=int(row.metrics.conversions),
                    conversion_value=Decimal(str(row.metrics.conversions_value)),
                    # Google does not expose a 3-second or 15-second view count in this report.
                    # Hook and hold rules therefore never apply to Google ads.
                    video_3s=None,
                    video_15s=None,
                )
            )
        return rows

    async def fetch_daily_metrics(self, ad_external_ids: list[str], since: date, until: date) -> list[SnapshotRow]:
        rows: list[SnapshotRow] = []
        for start in range(0, len(ad_external_ids), SEARCH_CHUNK_SIZE):
            chunk = ad_external_ids[start : start + SEARCH_CHUNK_SIZE]
            rows.extend(await self._run(self._fetch_metrics, chunk, since, until))
        return rows

    def _set_ad_status(self, ad_group_ad_resource: str, status: str) -> None:
        operation = self._client.get_type("AdGroupAdOperation")
        operation.update.resource_name = ad_group_ad_resource
        operation.update.status = self._enum("AdGroupAdStatusEnum", "AdGroupAdStatus", status)
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        self._client.get_service("AdGroupAdService").mutate_ad_group_ads(
            customer_id=self._customer_id, operations=[operation]
        )

    async def pause_ad(self, ad_external_id: str) -> None:
        await self._run(self._set_ad_status, ad_external_id, "PAUSED")

    def _read_budget_micros(self, campaign_resource: str) -> int:
        rows = self._search(
            "SELECT campaign_budget.amount_micros FROM campaign "
            f"WHERE campaign.resource_name = '{campaign_resource}'"
        )
        if not rows:
            raise ExternalAPIError(f"Campaign {campaign_resource} not found")
        return int(rows[0].campaign_budget.amount_micros)

    async def get_daily_budget(self, campaign_external_id: str) -> int:
        micros = await self._run(self._read_budget_micros, campaign_external_id)
        return micros // MICROS_PER_MINOR_UNIT

    def _write_budget(self, campaign_resource: str, amount_micros: int) -> None:
        rows = self._search(f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.resource_name = '{campaign_resource}'")
        if not rows:
            raise ExternalAPIError(f"Campaign {campaign_resource} not found")
        operation = self._client.get_type("CampaignBudgetOperation")
        operation.update.resource_name = rows[0].campaign.campaign_budget
        operation.update.amount_micros = amount_micros
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["amount_micros"]))
        self._client.get_service("CampaignBudgetService").mutate_campaign_budgets(
            customer_id=self._customer_id, operations=[operation]
        )

    async def set_daily_budget(self, campaign_external_id: str, minor_units: int) -> None:
        await self._run(self._write_budget, campaign_external_id, minor_units * MICROS_PER_MINOR_UNIT)
