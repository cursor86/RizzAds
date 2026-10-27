from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.config import get_settings
from app.organic import crypto
from app.organic.gates import credential_block_reason, job_retry_delay, platform_block_reason
from app.organic.graph import (
    PermissionRevokedError,
    PublishRejectedError,
    RateLimitedError,
    TokenInvalidError,
    appsecret_proof,
    classify,
    retry_hint,
)
from app.organic.models import CredentialStatus, PublishPlatform
from app.organic.publishers import (
    FacebookReelPublisher,
    InstagramPublisher,
    PublishTimeoutError,
    backoff_delays,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
MARGIN = timedelta(hours=1)


def _err(status: int = 400, code: int | None = None, subcode: int | None = None, transient: bool = False, retry_after=None):
    return classify(
        status=status, code=code, subcode=subcode, message="m", fbtrace_id="t", transient=transient, retry_after=retry_after
    )


# --- Error classification ---------------------------------------------------------------------


def test_code_190_is_token_invalid_and_not_retried():
    error = _err(status=400, code=190, subcode=463)
    assert isinstance(error, TokenInvalidError)
    assert retry_hint(error) is None


@pytest.mark.parametrize("code", [10, 200, 299])
def test_permission_codes_are_revocations(code):
    error = _err(code=code)
    assert isinstance(error, PermissionRevokedError)
    assert retry_hint(error) is None


def test_rate_limit_waits_at_least_a_minute_by_default():
    error = _err(status=429, code=4)
    assert isinstance(error, RateLimitedError)
    assert retry_hint(error) == 60.0


def test_retry_after_header_is_honoured_for_rate_limits():
    error = _err(status=429, code=4, retry_after=120)
    assert retry_hint(error) == 120.0


def test_transient_server_error_uses_backoff():
    error = _err(status=503, transient=True)
    assert retry_hint(error) == 0.0


def test_plain_client_error_is_permanent():
    assert retry_hint(_err(status=400, code=100)) is None


# --- Request signing --------------------------------------------------------------------------


def test_appsecret_proof_is_hmac_sha256_of_token():
    import hashlib
    import hmac

    expected = hmac.new(b"secret-xyz", b"token-abc", hashlib.sha256).hexdigest()
    assert appsecret_proof("token-abc", "secret-xyz") == expected


# --- Gates ------------------------------------------------------------------------------------


def test_revoked_credential_blocks():
    reason = credential_block_reason(
        status=CredentialStatus.REVOKED, user_token_expires_at=NOW + timedelta(days=30), now=NOW, margin=MARGIN
    )
    assert reason and "revoked" in reason.lower()


def test_expired_status_blocks_even_with_future_expiry():
    reason = credential_block_reason(
        status=CredentialStatus.EXPIRED, user_token_expires_at=NOW + timedelta(days=30), now=NOW, margin=MARGIN
    )
    assert reason and "expired" in reason.lower()


def test_token_inside_safety_margin_blocks():
    reason = credential_block_reason(
        status=CredentialStatus.ACTIVE, user_token_expires_at=NOW + timedelta(minutes=30), now=NOW, margin=MARGIN
    )
    assert reason is not None


def test_healthy_active_token_allows_publishing():
    assert (
        credential_block_reason(
            status=CredentialStatus.ACTIVE, user_token_expires_at=NOW + timedelta(days=30), now=NOW, margin=MARGIN
        )
        is None
    )


def test_missing_page_blocks_both_platforms():
    for platform in PublishPlatform:
        assert platform_block_reason(platform=platform, page_id=None, page_token=None, ig_account_id=None)


def test_instagram_needs_linked_account_but_facebook_does_not():
    assert platform_block_reason(
        platform=PublishPlatform.INSTAGRAM, page_id="1", page_token="t", ig_account_id=None
    ) is not None
    assert platform_block_reason(
        platform=PublishPlatform.FACEBOOK, page_id="1", page_token="t", ig_account_id=None
    ) is None


def test_job_retry_delay_doubles_and_caps():
    assert job_retry_delay(1).total_seconds() == 60
    assert job_retry_delay(2).total_seconds() == 120
    assert job_retry_delay(20).total_seconds() == 30 * 60


# --- Backoff ----------------------------------------------------------------------------------


def test_backoff_grows_and_respects_cap():
    delays = backoff_delays(base=5, cap=60)
    values = [next(delays) for _ in range(8)]
    assert values[0] < values[1] < values[2]
    assert max(values) <= 60 * 1.1  # cap plus at most 10% jitter


# --- Fake graph for publishing flows ----------------------------------------------------------


class FakeGraph:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []
        self.uploads: list[tuple[str, str]] = []

    async def request(self, method, path, *, token, params=None, data=None, **_: Any) -> dict[str, Any]:
        self.calls.append((method, path, data))
        return self._responses.pop(0)

    async def upload_reel_from_url(self, video_id: str, file_url: str, page_token: str) -> None:
        self.uploads.append((video_id, file_url))


class FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


async def _no_sleep(_: float) -> None:
    return None


async def test_instagram_polls_until_finished_then_publishes():
    graph = FakeGraph(
        [
            {"id": "C1"},  # create container
            {"status_code": "IN_PROGRESS"},
            {"status_code": "FINISHED"},
            {"id": "M1"},  # media_publish
        ]
    )
    publisher = InstagramPublisher(graph, "IG1", "page-token", max_wait_seconds=600)
    creation, media = await publisher.publish_reel("https://cdn.example.com/a.mp4", "caption")
    assert (creation, media) == ("C1", "M1")
    assert graph.calls[0][1] == "IG1/media"
    assert graph.calls[0][2]["media_type"] == "REELS"
    assert graph.calls[-1][1] == "IG1/media_publish"
    assert graph.calls[-1][2] == {"creation_id": "C1"}


async def test_instagram_error_status_is_permanent():
    graph = FakeGraph([{"status_code": "ERROR", "status": "unsupported format"}])
    publisher = InstagramPublisher(graph, "IG1", "page-token", max_wait_seconds=600)
    with pytest.raises(PublishRejectedError):
        await publisher.wait_until_finished("C1", sleep=_no_sleep)


async def test_instagram_times_out_when_never_finished():
    graph = FakeGraph([{"status_code": "IN_PROGRESS"}] * 50)
    publisher = InstagramPublisher(graph, "IG1", "page-token", max_wait_seconds=60)
    with pytest.raises(PublishTimeoutError):
        await publisher.wait_until_finished("C1", sleep=_no_sleep, clock=FakeClock(step=20))


async def test_facebook_reel_runs_start_upload_finish_then_waits():
    graph = FakeGraph(
        [
            {"video_id": "V1"},  # start
            {"success": True},  # finish
            {"status": {"publishing_phase": {"status": "complete"}}},
        ]
    )
    publisher = FacebookReelPublisher(graph, "PAGE1", "page-token", max_wait_seconds=600)
    video_id = await publisher.publish_reel("https://cdn.example.com/a.mp4", "desc")
    assert video_id == "V1"
    assert graph.uploads == [("V1", "https://cdn.example.com/a.mp4")]
    assert graph.calls[0][2] == {"upload_phase": "start"}
    assert graph.calls[1][2]["upload_phase"] == "finish"
    assert graph.calls[1][2]["video_state"] == "PUBLISHED"


async def test_facebook_processing_error_is_permanent():
    graph = FakeGraph([{"status": {"video_status": "error"}}])
    publisher = FacebookReelPublisher(graph, "PAGE1", "page-token", max_wait_seconds=600)
    with pytest.raises(PublishRejectedError):
        await publisher.wait_until_published("V1", sleep=_no_sleep)


# --- Credential encryption --------------------------------------------------------------------


@pytest.fixture
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    yield
    get_settings.cache_clear()
    crypto._fernet.cache_clear()


def test_encrypted_text_round_trips_and_is_not_plaintext(encryption_key):
    column = crypto.EncryptedText()
    stored = column.process_bind_param("EAAG-secret-token", dialect=None)
    assert "EAAG-secret-token" not in stored
    assert column.process_result_value(stored, dialect=None) == "EAAG-secret-token"


def test_wrong_key_fails_loudly(encryption_key, monkeypatch):
    from cryptography.fernet import Fernet

    column = crypto.EncryptedText()
    stored = column.process_bind_param("EAAG-secret-token", dialect=None)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    with pytest.raises(crypto.CredentialCorruptedError):
        column.process_result_value(stored, dialect=None)
