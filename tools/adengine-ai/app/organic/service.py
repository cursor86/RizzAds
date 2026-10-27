"""Queue processing and credential lifecycle for organic publishing.

Guarantees:
* A publish never starts while the client's credential is blocked (see gates.py). Queued jobs are marked BLOCKED
  with the reason, so the dashboard shows why nothing went out.
* When Meta reports an invalid token or a revoked permission, the credential is marked, every queued job for that
  client is blocked in the same transaction, and a CredentialEvent records the code and fbtrace_id. The token is
  never logged.
* Each job is claimed with SELECT ... FOR UPDATE SKIP LOCKED, so several workers can run without double-publishing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.organic.gates import credential_block_reason, job_retry_delay, platform_block_reason
from app.organic.graph import (
    GraphClient,
    GraphError,
    PermissionRevokedError,
    PublishRejectedError,
    RateLimitedError,
    TokenInvalidError,
)
from app.organic.models import (
    ClientPlatformCredential,
    CredentialEvent,
    CredentialEventType,
    CredentialStatus,
    PublishJob,
    PublishPlatform,
    PublishStatus,
)
from app.organic.oauth import inspect_token, needs_refresh, exchange_for_long_lived
from app.organic.publishers import FacebookReelPublisher, InstagramPublisher, PublishTimeoutError

logger = logging.getLogger("adengine.organic")

# Meta subcode 463: the session has expired. Anything else on code 190 means the token was invalidated.
SUBCODE_SESSION_EXPIRED = 463


def _margin(settings: Settings) -> timedelta:
    return timedelta(hours=settings.token_expiry_margin_hours)


async def record_event(
    session: AsyncSession, client_id: int, event: CredentialEventType, detail: dict | None = None
) -> None:
    session.add(CredentialEvent(client_id=client_id, event=event, detail=detail or {}))


def _log_failure(event: str, *, client_id: int, job_id: int | None, error: GraphError) -> None:
    logger.error(
        event,
        extra={
            "client_id": client_id,
            "job_id": job_id,
            "graph_status": error.status,
            "graph_code": error.code,
            "graph_subcode": error.subcode,
            "fbtrace_id": error.fbtrace_id,
        },
    )


async def enqueue(
    session: AsyncSession,
    *,
    client_id: int,
    platforms: list[PublishPlatform],
    video_url: str,
    caption: str,
    now: datetime | None = None,
) -> list[PublishJob]:
    now = now or datetime.now(timezone.utc)
    jobs = [
        PublishJob(
            client_id=client_id,
            platform=platform,
            video_url=video_url,
            caption=caption,
            status=PublishStatus.QUEUED,
            next_attempt_at=now,
        )
        for platform in platforms
    ]
    session.add_all(jobs)
    await session.commit()
    return jobs


async def block_client_queue(session: AsyncSession, client_id: int, reason: str) -> None:
    await session.execute(
        update(PublishJob)
        .where(
            PublishJob.client_id == client_id,
            PublishJob.status.in_([PublishStatus.QUEUED, PublishStatus.RUNNING]),
        )
        .values(status=PublishStatus.BLOCKED, blocked_reason=reason)
    )


async def requeue_blocked(session: AsyncSession, client_id: int, now: datetime | None = None) -> int:
    """Call after a successful reconnect. Only jobs blocked by the credential return to the queue."""
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(PublishJob)
        .where(PublishJob.client_id == client_id, PublishJob.status == PublishStatus.BLOCKED)
        .values(status=PublishStatus.QUEUED, blocked_reason=None, next_attempt_at=now)
    )
    await session.commit()
    return result.rowcount or 0


async def _mark_credential_unusable(
    session: AsyncSession,
    cred: ClientPlatformCredential,
    *,
    status: CredentialStatus,
    reason: str,
    event: CredentialEventType,
    error: GraphError,
    job_id: int | None,
) -> None:
    cred.status = status
    cred.status_reason = reason
    cred.last_checked_at = datetime.now(timezone.utc)
    await record_event(
        session,
        cred.client_id,
        event,
        {"graph_code": error.code, "graph_subcode": error.subcode, "fbtrace_id": error.fbtrace_id, "job_id": job_id},
    )
    await block_client_queue(session, cred.client_id, reason)
    _log_failure("Credential unusable; publishing blocked for client", client_id=cred.client_id, job_id=job_id, error=error)


def _status_for_token_error(error: GraphError) -> CredentialStatus:
    if isinstance(error, TokenInvalidError) and error.subcode == SUBCODE_SESSION_EXPIRED:
        return CredentialStatus.EXPIRED
    return CredentialStatus.REVOKED


async def process_job(
    session: AsyncSession,
    job: PublishJob,
    graph: GraphClient,
    now: datetime,
    settings: Settings,
) -> None:
    cred = await session.scalar(
        select(ClientPlatformCredential).where(ClientPlatformCredential.client_id == job.client_id)
    )
    if cred is None:
        job.status = PublishStatus.BLOCKED
        job.blocked_reason = "This client has not connected a Facebook account."
        await session.commit()
        return

    block = credential_block_reason(
        status=cred.status,
        user_token_expires_at=cred.user_token_expires_at,
        now=now,
        margin=_margin(settings),
    ) or platform_block_reason(
        platform=job.platform,
        page_id=cred.fb_page_id,
        page_token=cred.fb_page_access_token,
        ig_account_id=cred.instagram_business_account_id,
    )
    if block:
        job.status = PublishStatus.BLOCKED
        job.blocked_reason = block
        await session.commit()
        logger.warning("Publish blocked", extra={"client_id": job.client_id, "job_id": job.id, "reason": block})
        return

    job.status = PublishStatus.RUNNING
    job.attempts += 1
    await session.commit()

    page_token = cred.fb_page_access_token or ""
    try:
        if job.platform == PublishPlatform.INSTAGRAM:
            publisher = InstagramPublisher(graph, cred.instagram_business_account_id or "", page_token)

            def remember_container(creation_id: str) -> None:
                job.ig_container_id = creation_id

            _, media_id = await publisher.publish_reel(job.video_url, job.caption, on_container=remember_container)
            job.external_id = media_id
        else:
            publisher = FacebookReelPublisher(graph, cred.fb_page_id or "", page_token)
            job.external_id = await publisher.publish_reel(job.video_url, job.caption)

        job.status = PublishStatus.PUBLISHED
        job.published_at = now
        job.last_error = None
        job.blocked_reason = None
        await session.commit()
        logger.info("Publish succeeded", extra={"client_id": job.client_id, "job_id": job.id, "platform": job.platform.value})

    except (TokenInvalidError, PermissionRevokedError) as error:
        status = _status_for_token_error(error) if isinstance(error, TokenInvalidError) else CredentialStatus.REVOKED
        event = (
            CredentialEventType.PERMISSION_REVOKED
            if isinstance(error, PermissionRevokedError)
            else CredentialEventType.TOKEN_INVALID
        )
        reason = (
            "Client removed a required permission. Reconnect the account."
            if isinstance(error, PermissionRevokedError)
            else "Access token is invalid or expired. Reconnect the account."
        )
        job.status = PublishStatus.BLOCKED
        job.blocked_reason = reason
        job.last_error = error.message[:2000]
        await _mark_credential_unusable(
            session, cred, status=status, reason=reason, event=event, error=error, job_id=job.id
        )
        await session.commit()

    except PublishRejectedError as error:
        job.status = PublishStatus.FAILED
        job.last_error = error.message[:2000]
        _log_failure("Platform rejected the content", client_id=job.client_id, job_id=job.id, error=error)
        await session.commit()

    except (RateLimitedError, GraphError, PublishTimeoutError, httpx.TransportError) as error:
        retryable = isinstance(error, (RateLimitedError, PublishTimeoutError, httpx.TransportError)) or (
            isinstance(error, GraphError) and error.transient
        )
        job.last_error = str(error)[:2000]
        if retryable and job.attempts < settings.organic_max_attempts:
            job.status = PublishStatus.QUEUED
            job.next_attempt_at = now + job_retry_delay(job.attempts)
            logger.warning("Publish will retry", extra={"job_id": job.id, "attempt": job.attempts})
        else:
            job.status = PublishStatus.FAILED
            if isinstance(error, GraphError):
                _log_failure("Publish failed permanently", client_id=job.client_id, job_id=job.id, error=error)
            else:
                logger.error("Publish failed permanently", extra={"job_id": job.id, "error": str(error)})
        await session.commit()


async def process_due_jobs(session: AsyncSession, graph: GraphClient, now: datetime | None = None, limit: int = 10) -> int:
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    jobs = (
        await session.scalars(
            select(PublishJob)
            .where(PublishJob.status == PublishStatus.QUEUED, PublishJob.next_attempt_at <= now)
            .order_by(PublishJob.next_attempt_at, PublishJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for job in jobs:
        await process_job(session, job, graph, now, settings)
    return len(jobs)


async def check_credentials(session: AsyncSession, graph: GraphClient, now: datetime | None = None) -> dict[str, int]:
    """Daily sweep. Verifies each active token with Meta and refreshes any that are close to expiry."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    creds = (
        await session.scalars(
            select(ClientPlatformCredential).where(ClientPlatformCredential.status == CredentialStatus.ACTIVE)
        )
    ).all()
    counts = {"checked": 0, "invalid": 0, "refreshed": 0}
    for cred in creds:
        counts["checked"] += 1
        try:
            inspection = await inspect_token(graph, cred.fb_user_access_token)
        except GraphError as error:
            _log_failure("Token inspection failed", client_id=cred.client_id, job_id=None, error=error)
            continue

        cred.last_checked_at = now
        await record_event(session, cred.client_id, CredentialEventType.TOKEN_CHECKED, {"valid": inspection.is_valid})
        if not inspection.is_valid:
            counts["invalid"] += 1
            reason = "Meta reports the access token is no longer valid. Reconnect the account."
            cred.status = CredentialStatus.EXPIRED
            cred.status_reason = reason
            await record_event(session, cred.client_id, CredentialEventType.TOKEN_INVALID, {})
            await block_client_queue(session, cred.client_id, reason)
            continue

        if inspection.expires_at is not None:
            cred.user_token_expires_at = inspection.expires_at
        if needs_refresh(cred.user_token_expires_at, now, settings.token_refresh_days_before_expiry):
            fresh = await exchange_for_long_lived(graph, cred.fb_user_access_token, now)
            cred.fb_user_access_token = fresh.access_token
            cred.user_token_expires_at = fresh.expires_at
            counts["refreshed"] += 1
            await record_event(session, cred.client_id, CredentialEventType.TOKEN_REFRESHED, {})
    await session.commit()
    return counts
