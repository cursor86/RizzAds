"""HTTP endpoints for client onboarding, Page selection, status, publishing, and disconnect.

The access token is only ever accepted once, from the Facebook Login button, and is never echoed back. Responses
contain Page and Instagram account identifiers, names, and publish state, but no tokens.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Brand
from app.organic.gates import credential_block_reason, platform_block_reason
from app.organic.graph import GraphClient, GraphError
from app.organic.models import (
    ClientPlatformCredential,
    CredentialEventType,
    CredentialStatus,
    PublishJob,
    PublishPlatform,
)
from app.organic.oauth import (
    OnboardingError,
    exchange_for_long_lived,
    fetch_pages,
    inspect_token,
    missing_scopes,
)
from app.organic.service import block_client_queue, enqueue, record_event, requeue_blocked
from app.security import require_api_key

logger = logging.getLogger("adengine.organic.api")

router = APIRouter(prefix="/brands/{brand_id}/organic", tags=["organic"], dependencies=[Depends(require_api_key)])
jobs_router = APIRouter(prefix="/organic", tags=["organic"], dependencies=[Depends(require_api_key)])

SessionDep = Depends(get_session)
MAX_CAPTION = 2200


class ConnectIn(BaseModel):
    short_lived_token: str = Field(min_length=20, max_length=4096)


class SelectPageIn(BaseModel):
    page_id: str = Field(pattern=r"^\d{5,25}$")


class ReelIn(BaseModel):
    video_url: str = Field(max_length=2000)
    caption: str = Field(default="", max_length=MAX_CAPTION)
    platforms: list[PublishPlatform] = Field(default_factory=lambda: [PublishPlatform.INSTAGRAM, PublishPlatform.FACEBOOK], min_length=1)

    @field_validator("video_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("video_url must be a public https URL")
        return value

    @field_validator("platforms")
    @classmethod
    def _unique(cls, value: list[PublishPlatform]) -> list[PublishPlatform]:
        return list(dict.fromkeys(value))


async def _brand_or_404(session: AsyncSession, brand_id: int) -> Brand:
    brand = await session.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return brand


async def _credential_or_404(session: AsyncSession, brand_id: int) -> ClientPlatformCredential:
    cred = await session.scalar(select(ClientPlatformCredential).where(ClientPlatformCredential.client_id == brand_id))
    if cred is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Facebook account connected for this client")
    return cred


def _page_view(cred: ClientPlatformCredential) -> dict[str, Any] | None:
    if not cred.fb_page_id:
        return None
    return {
        "page_id": cred.fb_page_id,
        "name": cred.fb_page_name,
        "instagram_business_account_id": cred.instagram_business_account_id,
        "instagram_username": cred.instagram_username,
    }


def _readiness(cred: ClientPlatformCredential, now: datetime) -> dict[str, str | None]:
    settings = get_settings()
    margin = timedelta(hours=settings.token_expiry_margin_hours)
    reasons: dict[str, str | None] = {}
    global_reason = credential_block_reason(
        status=cred.status, user_token_expires_at=cred.user_token_expires_at, now=now, margin=margin
    )
    for platform in PublishPlatform:
        reasons[platform.value] = global_reason or platform_block_reason(
            platform=platform,
            page_id=cred.fb_page_id,
            page_token=cred.fb_page_access_token,
            ig_account_id=cred.instagram_business_account_id,
        )
    return reasons


@router.post("/connect", status_code=status.HTTP_201_CREATED)
async def connect(brand_id: int, payload: ConnectIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    """Exchange the short-lived Facebook Login token for a long-lived one and list the client's Pages."""
    await _brand_or_404(session, brand_id)
    now = datetime.now(timezone.utc)
    graph = GraphClient()
    try:
        long_lived = await exchange_for_long_lived(graph, payload.short_lived_token, now)
        inspection = await inspect_token(graph, long_lived.access_token)
        if not inspection.is_valid or inspection.user_id is None:
            raise OnboardingError("Facebook rejected the login token. Log in again.")
        absent = missing_scopes(inspection.scopes)
        if absent:
            raise OnboardingError(f"Missing permissions: {', '.join(absent)}. Approve them and try again.")
        pages = await fetch_pages(graph, long_lived.access_token)
    except OnboardingError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error))
    except GraphError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Facebook API error: {error.message}")
    finally:
        await graph.aclose()

    cred = await session.scalar(select(ClientPlatformCredential).where(ClientPlatformCredential.client_id == brand_id))
    if cred is None:
        cred = ClientPlatformCredential(client_id=brand_id, fb_user_id=inspection.user_id, fb_user_access_token="")
        session.add(cred)
    cred.fb_user_id = inspection.user_id
    cred.fb_user_access_token = long_lived.access_token
    cred.user_token_expires_at = long_lived.expires_at
    cred.granted_scopes = sorted(inspection.scopes)
    cred.status = CredentialStatus.ACTIVE
    cred.status_reason = None
    cred.last_checked_at = now
    # A new login may belong to a different Facebook user, so the old Page selection is cleared.
    cred.fb_page_id = None
    cred.fb_page_name = None
    cred.fb_page_access_token = None
    cred.instagram_business_account_id = None
    cred.instagram_username = None
    await record_event(session, brand_id, CredentialEventType.CONNECTED, {"pages": len(pages)})
    await session.commit()

    return {
        "client_id": brand_id,
        "status": CredentialStatus.ACTIVE.value,
        "user_token_expires_at": long_lived.expires_at.isoformat(),
        "pages": [
            {
                "page_id": page.page_id,
                "name": page.name,
                "instagram_business_account_id": page.instagram_business_account_id,
                "instagram_username": page.instagram_username,
            }
            for page in pages
        ],
    }


@router.post("/page")
async def select_page(brand_id: int, payload: SelectPageIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    """Store the chosen Page's token. Jobs that were blocked for lack of a Page go back on the queue."""
    cred = await _credential_or_404(session, brand_id)
    if cred.status != CredentialStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Reconnect the Facebook account before selecting a Page")
    graph = GraphClient()
    try:
        pages = await fetch_pages(graph, cred.fb_user_access_token)
    except GraphError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Facebook API error: {error.message}")
    finally:
        await graph.aclose()

    chosen = next((page for page in pages if page.page_id == payload.page_id), None)
    if chosen is None or not chosen.page_access_token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That Page is not available to this Facebook account")

    cred.fb_page_id = chosen.page_id
    cred.fb_page_name = chosen.name
    cred.fb_page_access_token = chosen.page_access_token
    cred.instagram_business_account_id = chosen.instagram_business_account_id
    cred.instagram_username = chosen.instagram_username
    await record_event(
        session,
        brand_id,
        CredentialEventType.PAGE_SELECTED,
        {"page_id": chosen.page_id, "instagram_linked": chosen.instagram_business_account_id is not None},
    )
    await session.commit()
    requeued = await requeue_blocked(session, brand_id)
    return {"page": _page_view(cred), "requeued_jobs": requeued}


@router.get("/status")
async def status_check(brand_id: int, session: AsyncSession = SessionDep) -> dict[str, Any]:
    """Live check against Meta. A token that Meta reports as invalid is marked and the queue is blocked."""
    cred = await _credential_or_404(session, brand_id)
    now = datetime.now(timezone.utc)
    graph = GraphClient()
    try:
        inspection = await inspect_token(graph, cred.fb_user_access_token)
    except GraphError as error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Facebook API error: {error.message}")
    finally:
        await graph.aclose()

    cred.last_checked_at = now
    if not inspection.is_valid and cred.status == CredentialStatus.ACTIVE:
        reason = "Meta reports the access token is no longer valid. Reconnect the account."
        cred.status = CredentialStatus.EXPIRED
        cred.status_reason = reason
        await block_client_queue(session, brand_id, reason)
        await record_event(session, brand_id, CredentialEventType.TOKEN_INVALID, {"source": "status_check"})
    elif inspection.is_valid and inspection.expires_at is not None:
        cred.user_token_expires_at = inspection.expires_at
    await session.commit()

    return {
        "client_id": brand_id,
        "status": cred.status.value,
        "status_reason": cred.status_reason,
        "user_token_expires_at": cred.user_token_expires_at.isoformat(),
        "page": _page_view(cred),
        "publishing_blocked": _readiness(cred, now),
    }


@router.post("/reels", status_code=status.HTTP_202_ACCEPTED)
async def publish_reel(brand_id: int, payload: ReelIn, session: AsyncSession = SessionDep) -> dict[str, Any]:
    """Queue a Reel. If the credential or the platform setup is not usable, nothing is queued and the reasons are returned."""
    cred = await _credential_or_404(session, brand_id)
    now = datetime.now(timezone.utc)
    readiness = _readiness(cred, now)
    blocked = {p.value: readiness[p.value] for p in payload.platforms if readiness[p.value]}
    if blocked:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "Publishing is blocked", "reasons": blocked})

    jobs = await enqueue(
        session,
        client_id=brand_id,
        platforms=payload.platforms,
        video_url=payload.video_url,
        caption=payload.caption,
        now=now,
    )
    return {
        "jobs": [{"id": job.id, "platform": job.platform.value, "status": job.status.value} for job in jobs],
    }


@jobs_router.get("/jobs/{job_id}")
async def get_job(job_id: int, session: AsyncSession = SessionDep) -> dict[str, Any]:
    job = await session.get(PublishJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return {
        "id": job.id,
        "client_id": job.client_id,
        "platform": job.platform.value,
        "status": job.status.value,
        "attempts": job.attempts,
        "external_id": job.external_id,
        "blocked_reason": job.blocked_reason,
        "last_error": job.last_error,
        "published_at": job.published_at.isoformat() if job.published_at else None,
    }


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(brand_id: int, session: AsyncSession = SessionDep) -> None:
    """Revoke the app's grant with Meta, delete the stored tokens, and block the queue."""
    cred = await _credential_or_404(session, brand_id)
    graph = GraphClient()
    try:
        await graph.request("DELETE", f"{cred.fb_user_id}/permissions", token=cred.fb_user_access_token)
    except GraphError as error:
        # The local tokens are deleted either way, but the remote grant may still exist, so this is logged as an error.
        logger.error(
            "Remote permission revoke failed; local tokens will still be deleted",
            extra={"client_id": brand_id, "graph_code": error.code, "graph_subcode": error.subcode, "fbtrace_id": error.fbtrace_id},
        )
    finally:
        await graph.aclose()
    await block_client_queue(session, brand_id, "Facebook account disconnected by the operator.")
    await record_event(session, brand_id, CredentialEventType.DISCONNECTED, {})
    await session.delete(cred)
    await session.commit()
