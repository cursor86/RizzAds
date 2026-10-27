"""HomeAdEngine: a single-file local utility that connects a client's Facebook Page and linked Instagram
Business account, then publishes Reels in the background.

Run locally:
    export HOMEAD_API_KEY="<long random string>"
    export META_APP_ID="<app id>"
    export META_APP_SECRET="<app secret>"
    python app.py

The server binds to 127.0.0.1 only. Every endpoint requires the X-API-Key header.

Security notes:
- Tokens are stored in the local SQLite file in plaintext. Protect that file with operating system permissions.
- Access tokens are sent in the Authorization header, never in URLs, and each call includes appsecret_proof.
- Tokens are never written to logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import field_validator
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ---------------------------------------------------------------------------
# Section 1: environment, structured logging, and the SQLite engine
# ---------------------------------------------------------------------------

DATABASE_URL = "sqlite:///home_ad_engine.db"
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v19.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
DEFAULT_TOKEN_LIFETIME_SECONDS = 60 * 24 * 60 * 60  # 60 days, Meta's long-lived token lifetime
CAPTION_MAX_LENGTH = 2200

# Background polling policy: 15 checks, starting at 10 seconds, times 1.5 per failed check, capped at 45 seconds.
MAX_POLL_ATTEMPTS = 15
INITIAL_POLL_DELAY_SECONDS = 10.0
POLL_BACKOFF_FACTOR = 1.5
POLL_DELAY_CAP_SECONDS = 45.0

API_KEY = os.getenv("HOMEAD_API_KEY", "")
META_APP_ID = os.getenv("META_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per line. Keys passed via `extra=` become top-level fields."""

    _STANDARD_ATTRIBUTES = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._STANDARD_ATTRIBUTES and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO. Graph URLs can carry secrets, so keep those libraries at WARNING.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("homeadengine")


logger = configure_logging()

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. SQLModel refuses to store naive datetimes."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """SQLite returns datetimes without a timezone. Those values are UTC, so attach the zone before comparing."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class ClientCredential(SQLModel, table=True):
    __tablename__ = "client_credentials"

    id: int | None = Field(default=None, primary_key=True)
    fb_user_id: str = Field(index=True, unique=True)
    long_lived_user_token: str
    fb_page_id: str | None = None
    fb_page_name: str | None = None
    fb_page_access_token: str | None = None
    instagram_business_account_id: str | None = None
    instagram_username: str | None = None
    token_expires_at: datetime
    updated_at: datetime = Field(default_factory=utc_now)


UPSERTABLE_COLUMNS = (
    "long_lived_user_token",
    "fb_page_id",
    "fb_page_name",
    "fb_page_access_token",
    "instagram_business_account_id",
    "instagram_username",
    "token_expires_at",
    "updated_at",
)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One session per unit of work. Commits on success, rolls back on error, always closes.

    Background tasks open their own scope, so they never share a transaction with a request.
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AuthCallbackRequest(SQLModel):
    short_lived_token: str = Field(min_length=20, max_length=4096)
    page_id: str | None = None

    @field_validator("page_id")
    @classmethod
    def _numeric_page_id(cls, value: str | None) -> str | None:
        if value is not None and not (value.isdigit() and 5 <= len(value) <= 25):
            raise ValueError("page_id must be a numeric Facebook Page id")
        return value


class ReelPublishRequest(SQLModel):
    client_id: int = Field(gt=0)
    video_url: str = Field(max_length=2000)
    caption: str = Field(default="", max_length=CAPTION_MAX_LENGTH)

    @field_validator("video_url")
    @classmethod
    def _require_public_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("video_url must be a public https URL")
        return value


# ---------------------------------------------------------------------------
# Errors and the Graph API boundary
# ---------------------------------------------------------------------------


class GraphAPIError(Exception):
    """Any failure talking to Meta. status_code is None for network failures."""

    def __init__(self, message: str, status_code: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CredentialExpiredError(Exception):
    """The stored user token has passed its expiry time. The client must reconnect."""


def appsecret_proof(token: str) -> str:
    return hmac.new(META_APP_SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


async def graph_call(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single entry point for every Meta call. Converts network and API failures into GraphAPIError."""
    url = f"{GRAPH_BASE_URL}/{path.lstrip('/')}"
    query: dict[str, Any] = dict(params or {})
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        query["appsecret_proof"] = appsecret_proof(token)

    try:
        response = await http.request(method, url, params=query, data=data, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(
            "Network failure contacting Meta",
            extra={"graph_path": path, "error_type": type(exc).__name__},
        )
        raise GraphAPIError(f"Network failure contacting Meta: {type(exc).__name__}") from exc

    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        payload = {}

    if response.is_error or "error" in payload:
        error = payload.get("error", {})
        message = str(error.get("message") or f"HTTP {response.status_code}")
        code = error.get("code")
        logger.error(
            "Meta API returned an error",
            extra={
                "graph_path": path,
                "http_status": response.status_code,
                "graph_code": code,
                "graph_subcode": error.get("error_subcode"),
                "fbtrace_id": error.get("fbtrace_id"),
                "graph_message": message,
            },
        )
        raise GraphAPIError(message, status_code=response.status_code, code=code)
    return payload


# ---------------------------------------------------------------------------
# Section 2: background Reels worker
# ---------------------------------------------------------------------------


def ensure_token_not_expired(expires_at: datetime) -> None:
    if as_utc(expires_at) <= utc_now():
        raise CredentialExpiredError(f"Access token expired at {expires_at.isoformat()}. Reconnect the client.")


async def create_reel_container(http: httpx.AsyncClient, ig_account_id: str, page_token: str, video_url: str, caption: str) -> str:
    payload = await graph_call(
        http,
        "POST",
        f"{ig_account_id}/media",
        token=page_token,
        data={"media_type": "REELS", "video_url": video_url, "caption": caption},
    )
    container_id = payload.get("id")
    if not container_id:
        raise GraphAPIError("Meta did not return a container id")
    return str(container_id)


async def wait_until_container_finished(http: httpx.AsyncClient, container_id: str, page_token: str) -> None:
    """Polls the container until Meta reports FINISHED, backing off between checks.

    Transient network failures count as a used attempt and do not abort the job early.
    """
    delay = INITIAL_POLL_DELAY_SECONDS
    last_state = "UNKNOWN"
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        await asyncio.sleep(delay)
        try:
            payload = await graph_call(
                http, "GET", container_id, token=page_token, params={"fields": "status_code,status"}
            )
        except GraphAPIError as exc:
            if exc.status_code is not None:
                raise  # The platform rejected the request. Retrying would not help.
            logger.warning(
                "Transient failure while checking container; will retry",
                extra={"container_id": container_id, "attempt": attempt},
            )
            delay = min(delay * POLL_BACKOFF_FACTOR, POLL_DELAY_CAP_SECONDS)
            continue

        last_state = str(payload.get("status_code") or "UNKNOWN")
        logger.info(
            "Container status checked",
            extra={"container_id": container_id, "attempt": attempt, "status_code": last_state},
        )
        if last_state == "FINISHED":
            return
        if last_state in {"ERROR", "EXPIRED"}:
            detail = payload.get("status") or last_state
            raise GraphAPIError(f"Instagram could not process the video: {detail}")
        delay = min(delay * POLL_BACKOFF_FACTOR, POLL_DELAY_CAP_SECONDS)

    raise GraphAPIError(
        f"Container {container_id} did not reach FINISHED after {MAX_POLL_ATTEMPTS} checks (last state: {last_state})"
    )


async def publish_container(http: httpx.AsyncClient, ig_account_id: str, page_token: str, container_id: str) -> str:
    payload = await graph_call(
        http, "POST", f"{ig_account_id}/media_publish", token=page_token, data={"creation_id": container_id}
    )
    media_id = payload.get("id")
    if not media_id:
        raise GraphAPIError("Meta did not return a published media id")
    return str(media_id)


async def process_and_publish_ig_reel(credential_id: int, video_url: str, caption: str) -> None:
    """Runs in the background. Every failure is logged with context and never escapes the task."""
    log_context = {"credential_id": credential_id}
    try:
        with session_scope() as session:
            credential = session.get(ClientCredential, credential_id)
            if credential is None:
                logger.error("Publish skipped: credential no longer exists", extra=log_context)
                return
            ig_account_id = credential.instagram_business_account_id
            page_token = credential.fb_page_access_token
            expires_at = credential.token_expires_at

        if not ig_account_id or not page_token:
            logger.error("Publish skipped: Instagram account or Page token is missing", extra=log_context)
            return

        ensure_token_not_expired(expires_at)

        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            container_id = await create_reel_container(http, ig_account_id, page_token, video_url, caption)
            logger.info("Reel container created", extra={**log_context, "container_id": container_id})

            await wait_until_container_finished(http, container_id, page_token)

            media_id = await publish_container(http, ig_account_id, page_token, container_id)
            logger.info("Reel published", extra={**log_context, "media_id": media_id})

    except CredentialExpiredError as exc:
        logger.error("Publish blocked: token expired", extra={**log_context, "reason": str(exc)})
    except GraphAPIError as exc:
        logger.error(
            "Reel publish failed",
            extra={**log_context, "reason": exc.message, "graph_code": exc.code, "http_status": exc.status_code},
        )
    except Exception:
        logger.exception("Unexpected failure while publishing Reel", extra=log_context)


# ---------------------------------------------------------------------------
# Section 3: FastAPI application and controllers
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY or not hmac.compare_digest((x_api_key or "").encode(), API_KEY.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def validate_configuration() -> None:
    missing = [name for name, value in (("HOMEAD_API_KEY", API_KEY), ("META_APP_ID", META_APP_ID), ("META_APP_SECRET", META_APP_SECRET)) if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_configuration()
    SQLModel.metadata.create_all(engine)
    logger.info("HomeAdEngine started", extra={"database": DATABASE_URL, "graph_version": GRAPH_API_VERSION})
    yield
    engine.dispose()


app = FastAPI(title="HomeAdEngine", version="1.0.0", lifespan=lifespan)


@app.post("/v1/auth/callback", dependencies=[Depends(require_api_key)])
async def auth_callback(payload: AuthCallbackRequest) -> dict[str, Any]:
    """Exchanges the short-lived login token for a 60-day token, finds the Page and Instagram account,
    and stores everything with an upsert keyed on the Facebook user id."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        try:
            # POST keeps the app secret and the login token out of the request URL.
            exchanged = await graph_call(
                http,
                "POST",
                "oauth/access_token",
                data={
                    "grant_type": "fb_exchange_token",
                    "client_id": META_APP_ID,
                    "client_secret": META_APP_SECRET,
                    "fb_exchange_token": payload.short_lived_token,
                },
            )
            long_lived_token = str(exchanged["access_token"])
            expires_in = int(exchanged.get("expires_in") or DEFAULT_TOKEN_LIFETIME_SECONDS)

            # The user id comes from Meta, never from the client, so a caller cannot claim someone else's account.
            me = await graph_call(http, "GET", "me", token=long_lived_token, params={"fields": "id"})
            fb_user_id = str(me["id"])

            pages_payload = await graph_call(
                http,
                "GET",
                "me/accounts",
                token=long_lived_token,
                params={
                    "fields": "id,name,access_token,instagram_business_account{id,username}",
                    "limit": 100,
                },
            )
        except GraphAPIError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Facebook error: {exc.message}") from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Unexpected response from Facebook: missing {exc}") from exc

    pages = [page for page in pages_payload.get("data", []) if page.get("access_token")]
    if not pages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No Facebook Pages with publishing access were found for this login")

    if payload.page_id:
        chosen = next((page for page in pages if str(page["id"]) == payload.page_id), None)
        if chosen is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That Page is not available to this login")
    elif len(pages) == 1:
        chosen = pages[0]
    else:
        # Never guess which Page a client wants to publish to.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "This login manages several Pages. Send page_id to choose one.",
                "pages": [{"page_id": str(p["id"]), "name": p.get("name", "")} for p in pages],
            },
        )

    ig_account = chosen.get("instagram_business_account") or {}
    values: dict[str, Any] = {
        "fb_user_id": fb_user_id,
        "long_lived_user_token": long_lived_token,
        "fb_page_id": str(chosen["id"]),
        "fb_page_name": chosen.get("name"),
        "fb_page_access_token": chosen["access_token"],
        "instagram_business_account_id": str(ig_account["id"]) if ig_account.get("id") else None,
        "instagram_username": ig_account.get("username"),
        "token_expires_at": utc_now() + timedelta(seconds=expires_in),
        "updated_at": utc_now(),
    }

    with session_scope() as session:
        # INSERT ... ON CONFLICT DO UPDATE: one atomic statement, safe against concurrent callbacks.
        statement = sqlite_insert(ClientCredential.__table__).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["fb_user_id"],
            set_={column: statement.excluded[column] for column in UPSERTABLE_COLUMNS},
        )
        session.execute(statement)
        credential = session.exec(select(ClientCredential).where(ClientCredential.fb_user_id == fb_user_id)).one()
        result = {
            "status": "connected",
            "client_id": credential.id,
            "page_id": credential.fb_page_id,
            "page_name": credential.fb_page_name,
            "has_instagram": credential.instagram_business_account_id is not None,
            "instagram_username": credential.instagram_username,
            "token_expires_at": as_utc(credential.token_expires_at).isoformat(),
        }

    logger.info("Client connected", extra={"client_id": result["client_id"], "has_instagram": result["has_instagram"]})
    return result


@app.post("/v1/publish/instagram-reel", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
async def queue_instagram_reel(payload: ReelPublishRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Checks the client is ready, then hands the publish to a background task and returns immediately.
    A 202 means the Reel was accepted for publishing, not that it has been published."""
    with session_scope() as session:
        credential = session.get(ClientCredential, payload.client_id)
        if credential is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
        if not credential.instagram_business_account_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has no linked Instagram Business account")
        if not credential.fb_page_access_token:
            raise HTTPException(status.HTTP_409_CONFLICT, "This client has no Page selected")
        if as_utc(credential.token_expires_at) <= utc_now():
            raise HTTPException(status.HTTP_409_CONFLICT, "Access token has expired. Reconnect the client.")
        credential_id = credential.id

    if credential_id is None:  # pragma: no cover - a stored row always has an id
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Credential has no id")

    background_tasks.add_task(process_and_publish_ig_reel, credential_id, payload.video_url, payload.caption)
    logger.info("Reel queued", extra={"client_id": payload.client_id})
    return {"status": "queued", "client_id": payload.client_id}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
