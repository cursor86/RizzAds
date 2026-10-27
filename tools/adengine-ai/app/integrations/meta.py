"""Meta Marketing API (Graph API) client and adapter, with rate-limit aware retries.

Object model used here: Campaign (CBO, conversions objective) -> Ad Set (broad targeting with Advantage+ audience)
-> one Ad per creative angle. Each ad's creative carries the angle's body copy plus the shared headline and
visual matrix through asset_feed_spec, so Meta's dynamic creative chooses the combinations.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.errors import ExternalAPIError, MediaProcessingError
from app.integrations.base import SnapshotRow
from app.integrations.retry import call_with_backoff

GRAPH_BASE_URL = "https://graph.facebook.com"

# Meta's app/user/page/business usage limit codes. Documented as "too many calls" and retried after a wait.
RATE_LIMIT_ERROR_CODES = frozenset({4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80007, 80008, 80009, 80014})
TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504})
# Meta's usage windows are rolling hour windows, so a long default wait is more effective than exponential backoff.
RATE_LIMIT_WAIT_SECONDS = 60.0

# Meta reports both "purchase" and the pixel-level purchase type. Use one, never both, or conversions double count.
PURCHASE_ACTION_TYPES = ("purchase", "offsite_conversion.fb_pixel_purchase")

INSIGHT_FIELDS = (
    "ad_id",
    "date_start",
    "spend",
    "impressions",
    "inline_link_clicks",
    "actions",
    "action_values",
    "video_3_sec_watched_actions",
    "video_15_sec_watched_actions",
)


class MetaAPIError(ExternalAPIError):
    def __init__(
        self,
        *,
        status: int,
        code: int | None,
        subcode: int | None,
        message: str,
        transient: bool,
        retry_after: float | None,
        fbtrace_id: str | None,
    ) -> None:
        super().__init__(
            f"Meta API error status={status} code={code} subcode={subcode} fbtrace_id={fbtrace_id}: {message}"
        )
        self.status = status
        self.code = code
        self.subcode = subcode
        self.transient = transient
        self.retry_after = retry_after
        self.fbtrace_id = fbtrace_id

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429 or self.code in RATE_LIMIT_ERROR_CODES


def meta_retry_hint(exc: BaseException) -> float | None:
    if isinstance(exc, MetaAPIError):
        if exc.is_rate_limited:
            return exc.retry_after or RATE_LIMIT_WAIT_SECONDS
        if exc.transient or exc.status in TRANSIENT_HTTP_STATUSES:
            return 0.0
        return None
    if isinstance(exc, httpx.TransportError):
        return 0.0
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_success and "error" not in payload:
        return payload
    error = payload.get("error", {})
    raise MetaAPIError(
        status=response.status_code,
        code=error.get("code"),
        subcode=error.get("error_subcode"),
        message=error.get("message") or response.text[:500],
        transient=bool(error.get("is_transient")),
        retry_after=_parse_retry_after(response.headers.get("Retry-After")),
        fbtrace_id=error.get("fbtrace_id"),
    )


class MetaClient:
    def __init__(self, access_token: str, api_version: str, http: httpx.AsyncClient | None = None) -> None:
        self._access_token = access_token
        self._api_version = api_version
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._send("GET", f"{GRAPH_BASE_URL}/{self._api_version}/{path.lstrip('/')}", params=params)

    async def post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return await self._send("POST", f"{GRAPH_BASE_URL}/{self._api_version}/{path.lstrip('/')}", data=data)

    async def paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        payload = await self.get(path, params)
        while True:
            rows.extend(payload.get("data", []))
            next_url = payload.get("paging", {}).get("next")
            if not next_url:
                return rows
            payload = await self._send("GET", next_url)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._access_token}"}

        async def attempt() -> dict[str, Any]:
            response = await self._http.request(method, url, params=params, data=data, headers=headers)
            return _parse_response(response)

        return await call_with_backoff(attempt, retry_hint=meta_retry_hint)


def _action_value(items: list[dict[str, Any]] | None, field: str = "value") -> Decimal:
    """Purchase total from Meta's actions/action_values list, preferring the aggregate purchase type."""
    if not items:
        return Decimal("0")
    by_type = {item.get("action_type"): Decimal(str(item.get(field, "0"))) for item in items}
    for action_type in PURCHASE_ACTION_TYPES:
        if action_type in by_type:
            return by_type[action_type]
    return Decimal("0")


def _video_total(row: dict[str, Any], key: str) -> int | None:
    if key not in row:
        return None
    return int(sum((Decimal(str(item.get("value", "0"))) for item in row[key]), Decimal("0")))


def _to_snapshot(row: dict[str, Any]) -> SnapshotRow:
    return SnapshotRow(
        ad_external_id=str(row["ad_id"]),
        day=date.fromisoformat(row["date_start"]),
        spend=Decimal(str(row.get("spend", "0"))),
        impressions=int(row.get("impressions", 0)),
        link_clicks=int(row.get("inline_link_clicks", 0)),
        conversions=int(_action_value(row.get("actions"))),
        conversion_value=_action_value(row.get("action_values")),
        video_3s=_video_total(row, "video_3_sec_watched_actions"),
        video_15s=_video_total(row, "video_15_sec_watched_actions"),
    )


class MetaAdapter:
    """Platform operations for one Meta ad account. Implements AdPlatform plus deploy helpers."""

    def __init__(self, client: MetaClient, ad_account_id: str) -> None:
        self._client = client
        self._account = f"act_{ad_account_id}"

    # --- Deploy -----------------------------------------------------------------------------

    async def create_campaign(self, name: str, daily_budget_minor: int) -> str:
        payload = await self._client.post(
            f"{self._account}/campaigns",
            {
                "name": name,
                "objective": "OUTCOME_SALES",
                "status": "PAUSED",
                "special_ad_categories": json.dumps([]),
                "daily_budget": str(daily_budget_minor),
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "is_adset_budget_sharing_enabled": "false",
            },
        )
        return str(payload["id"])

    async def create_ad_set(
        self,
        campaign_id: str,
        name: str,
        pixel_id: str,
        countries: list[str],
        age_min: int,
        age_max: int,
    ) -> str:
        targeting = {
            "geo_locations": {"countries": countries},
            "age_min": age_min,
            "age_max": age_max,
            # Advantage+ audience lets Meta expand beyond these limits. This is the intended broad-targeting mode.
            "targeting_automation": {"advantage_audience": 1},
        }
        payload = await self._client.post(
            f"{self._account}/adsets",
            {
                "name": name,
                "campaign_id": campaign_id,
                "status": "PAUSED",
                "optimization_goal": "OFFSITE_CONVERSIONS",
                "billing_event": "IMPRESSIONS",
                "promoted_object": json.dumps({"pixel_id": pixel_id, "custom_event_type": "PURCHASE"}),
                "targeting": json.dumps(targeting),
            },
        )
        return str(payload["id"])

    async def upload_image(self, url: str) -> str:
        payload = await self._client.post(f"{self._account}/adimages", {"url": url})
        images = payload.get("images", {})
        if not images:
            raise MediaProcessingError(f"Meta returned no image hash for {url}")
        return str(next(iter(images.values()))["hash"])

    async def upload_video(self, file_url: str) -> str:
        payload = await self._client.post(f"{self._account}/advideos", {"file_url": file_url})
        return str(payload["id"])

    async def wait_until_video_ready(self, video_id: str, timeout_seconds: float = 900, poll_seconds: float = 15) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            payload = await self._client.get(video_id, {"fields": "status"})
            state = payload.get("status", {}).get("video_status")
            if state == "ready":
                return
            if state == "error":
                raise MediaProcessingError(f"Meta failed to process video {video_id}")
            if time.monotonic() > deadline:
                raise MediaProcessingError(f"Video {video_id} was not ready after {timeout_seconds:.0f}s")
            await asyncio.sleep(poll_seconds)

    async def create_creative(
        self,
        name: str,
        page_id: str,
        bodies: list[str],
        titles: list[str],
        link_url: str,
        cta: str,
        *,
        image_hashes: list[str] | None = None,
        video_ids: list[str] | None = None,
    ) -> str:
        if bool(image_hashes) == bool(video_ids):
            raise ValueError("Provide exactly one of image_hashes or video_ids")
        feed: dict[str, Any] = {
            "bodies": [{"text": body} for body in bodies],
            "titles": [{"text": title} for title in titles],
            "link_urls": [{"website_url": link_url}],
            "call_to_action_types": [cta],
            "ad_formats": ["AUTOMATIC_FORMAT"],
        }
        if image_hashes:
            feed["images"] = [{"hash": image_hash} for image_hash in image_hashes]
        else:
            feed["videos"] = [{"video_id": video_id} for video_id in video_ids or []]
        payload = await self._client.post(
            f"{self._account}/adcreatives",
            {
                "name": name,
                "object_story_spec": json.dumps({"page_id": page_id}),
                "asset_feed_spec": json.dumps(feed),
            },
        )
        return str(payload["id"])

    async def create_ad(self, ad_set_id: str, name: str, creative_id: str) -> str:
        payload = await self._client.post(
            f"{self._account}/ads",
            {
                "name": name,
                "adset_id": ad_set_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": "PAUSED",
            },
        )
        return str(payload["id"])

    async def set_status(self, object_id: str, status: str) -> None:
        await self._client.post(object_id, {"status": status})

    # --- Optimization (AdPlatform) -----------------------------------------------------------

    async def fetch_daily_metrics(self, ad_external_ids: list[str], since: date, until: date) -> list[SnapshotRow]:
        rows: list[SnapshotRow] = []
        for start in range(0, len(ad_external_ids), 100):
            chunk = ad_external_ids[start : start + 100]
            raw = await self._client.paginate(
                f"{self._account}/insights",
                {
                    "level": "ad",
                    "time_increment": 1,
                    "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
                    "fields": ",".join(INSIGHT_FIELDS),
                    "filtering": json.dumps([{"field": "ad.id", "operator": "IN", "value": chunk}]),
                    "limit": 500,
                },
            )
            rows.extend(_to_snapshot(item) for item in raw)
        return rows

    async def pause_ad(self, ad_external_id: str) -> None:
        await self.set_status(ad_external_id, "PAUSED")

    async def get_daily_budget(self, campaign_external_id: str) -> int:
        payload = await self._client.get(campaign_external_id, {"fields": "daily_budget"})
        return int(payload["daily_budget"])

    async def set_daily_budget(self, campaign_external_id: str, minor_units: int) -> None:
        await self._client.post(campaign_external_id, {"daily_budget": str(minor_units)})
