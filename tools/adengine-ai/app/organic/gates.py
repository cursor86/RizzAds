"""Pure rules that decide whether a client's queue may publish. No I/O, so every branch is unit-tested.

A publish is blocked, not attempted, when the credential is revoked or expired, when the user token is within the
safety margin of expiry, or when the account is not set up for the requested platform.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.organic.models import CredentialStatus, PublishPlatform

# Backoff for transient job failures: 1, 2, 4 ... minutes, capped at 30 minutes.
JOB_RETRY_BASE_SECONDS = 60
JOB_RETRY_CAP_SECONDS = 30 * 60


def credential_block_reason(
    *,
    status: CredentialStatus,
    user_token_expires_at: datetime,
    now: datetime,
    margin: timedelta,
) -> str | None:
    if status == CredentialStatus.REVOKED:
        return "Client revoked access or the app lost permission. Reconnect the account to resume publishing."
    if status == CredentialStatus.EXPIRED:
        return "Access token has expired. Reconnect the account to resume publishing."
    if user_token_expires_at - now <= margin:
        return f"Access token expires at {user_token_expires_at.isoformat()}. Reconnect before publishing."
    return None


def platform_block_reason(
    *,
    platform: PublishPlatform,
    page_id: str | None,
    page_token: str | None,
    ig_account_id: str | None,
) -> str | None:
    if not page_token or not page_id:
        return "No Facebook Page is selected for this client."
    if platform == PublishPlatform.INSTAGRAM and not ig_account_id:
        return "The selected Page has no linked Instagram Business account."
    return None


def job_retry_delay(attempts: int) -> timedelta:
    seconds = min(JOB_RETRY_CAP_SECONDS, JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    return timedelta(seconds=seconds)
