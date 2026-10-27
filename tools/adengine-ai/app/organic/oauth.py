"""Client onboarding: exchange the short-lived token from the Facebook Login button for a long-lived token,
then discover the client's Pages and linked Instagram Business accounts.

Security notes:
* The short-lived token is used once and never stored.
* The app secret stays on the server. The frontend only ever sees the short-lived user token it received from
  Facebook Login, and the long-lived token is never returned to the client.
* Page tokens are fetched only when the client selects a Page, and they are stored encrypted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.errors import AdEngineError
from app.organic.graph import GraphClient

# Permissions the app needs to publish Reels to both destinations. Missing any of these blocks onboarding.
REQUIRED_SCOPES = frozenset(
    {
        "pages_show_list",
        "pages_read_engagement",
        "pages_manage_posts",
        "instagram_basic",
        "instagram_content_publish",
    }
)
DEFAULT_TOKEN_LIFETIME = timedelta(days=60)


class OnboardingError(AdEngineError):
    """The client's grants are not enough to publish, or the token exchange failed."""


@dataclass(frozen=True)
class LongLivedToken:
    access_token: str
    expires_at: datetime


@dataclass(frozen=True)
class PageOption:
    """What the client sees when choosing a Page. Carries no tokens."""

    page_id: str
    name: str
    instagram_business_account_id: str | None
    instagram_username: str | None


@dataclass(frozen=True)
class PageWithToken(PageOption):
    page_access_token: str = field(repr=False, default="")


@dataclass(frozen=True)
class TokenInspection:
    is_valid: bool
    user_id: str | None
    scopes: frozenset[str]
    expires_at: datetime | None
    data_access_expires_at: datetime | None


def _to_datetime(epoch: Any) -> datetime | None:
    if not epoch:  # 0 or missing means the token never expires
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


async def exchange_for_long_lived(graph: GraphClient, short_lived_token: str, now: datetime) -> LongLivedToken:
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise OnboardingError("META_APP_ID and META_APP_SECRET must be set")
    payload = await graph.request(
        "GET",
        "oauth/access_token",
        token=None,
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": short_lived_token,
        },
    )
    token = payload.get("access_token")
    if not token:
        raise OnboardingError("Meta did not return a long-lived token")
    lifetime = timedelta(seconds=int(payload["expires_in"])) if payload.get("expires_in") else DEFAULT_TOKEN_LIFETIME
    return LongLivedToken(access_token=token, expires_at=now + lifetime)


async def inspect_token(graph: GraphClient, user_token: str) -> TokenInspection:
    """Ask Meta whether a token is still valid, which scopes it has, and when it expires.

    Uses the app access token (app_id|app_secret) so that the inspection itself needs no user session.
    """
    settings = get_settings()
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret}"
    payload = await graph.request(
        "GET",
        "debug_token",
        token=app_token,
        params={"input_token": user_token},
    )
    data = payload.get("data", {})
    return TokenInspection(
        is_valid=bool(data.get("is_valid")),
        user_id=str(data["user_id"]) if data.get("user_id") else None,
        scopes=frozenset(data.get("scopes", [])),
        expires_at=_to_datetime(data.get("expires_at")),
        data_access_expires_at=_to_datetime(data.get("data_access_expires_at")),
    )


async def fetch_pages(graph: GraphClient, user_token: str) -> list[PageWithToken]:
    rows = await graph.paginate(
        "me/accounts",
        token=user_token,
        params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "limit": 100,
        },
    )
    pages: list[PageWithToken] = []
    for row in rows:
        ig = row.get("instagram_business_account") or {}
        pages.append(
            PageWithToken(
                page_id=str(row["id"]),
                name=row.get("name", ""),
                instagram_business_account_id=str(ig["id"]) if ig.get("id") else None,
                instagram_username=ig.get("username"),
                page_access_token=row.get("access_token", ""),
            )
        )
    return pages


def missing_scopes(granted: frozenset[str]) -> list[str]:
    return sorted(REQUIRED_SCOPES - granted)


def needs_refresh(expires_at: datetime, now: datetime, days_before: int) -> bool:
    return expires_at - now <= timedelta(days=days_before)
