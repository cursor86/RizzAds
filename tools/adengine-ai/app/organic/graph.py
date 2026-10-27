"""Graph API client for organic publishing.

Errors are split into three groups, because each one needs a different response:

* TokenInvalidError / PermissionRevokedError: the client's credential is no longer usable. The queue is blocked
  for that client until they reconnect.
* RateLimitedError / transient failures: retried with backoff, respecting Retry-After.
* Everything else: a permanent error for this one request. The job fails and the credential is left alone.

Every error is logged with the Graph error code, subcode, and fbtrace_id, so support can trace it. Tokens never
appear in logs or exception messages.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from app.config import get_settings
from app.integrations.retry import call_with_backoff

logger = logging.getLogger("adengine.organic.graph")

GRAPH_HOST = "https://graph.facebook.com"
RUPLOAD_HOST = "https://rupload.facebook.com"

# OAuthException 190: token expired, invalid, password changed, or the app was de-authorized.
TOKEN_INVALID_CODES = frozenset({190})
# Permission failures: the client removed a permission, or the app lost access to the Page or account.
PERMISSION_CODES = frozenset({10, 200, 299})
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80007, 80008, 80009, 80014})
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
RATE_LIMIT_WAIT_SECONDS = 60.0


class GraphError(Exception):
    def __init__(
        self,
        *,
        status: int,
        code: int | None,
        subcode: int | None,
        message: str,
        fbtrace_id: str | None,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Graph error status={status} code={code} subcode={subcode} fbtrace_id={fbtrace_id}: {message}")
        self.status = status
        self.code = code
        self.subcode = subcode
        self.message = message
        self.fbtrace_id = fbtrace_id
        self.transient = transient
        self.retry_after = retry_after


class TokenInvalidError(GraphError):
    """The credential has expired or was invalidated. Stop publishing for this client."""


class PermissionRevokedError(GraphError):
    """The client revoked a permission or the app lost access. Stop publishing for this client."""


class RateLimitedError(GraphError):
    """Too many calls. Retry after the wait."""


class PublishRejectedError(GraphError):
    """The platform rejected the content itself (for example, an unsupported video). Do not retry."""


def classify(
    *, status: int, code: int | None, subcode: int | None, message: str, fbtrace_id: str | None,
    transient: bool, retry_after: float | None,
) -> GraphError:
    args = dict(status=status, code=code, subcode=subcode, message=message, fbtrace_id=fbtrace_id)
    if code in TOKEN_INVALID_CODES:
        return TokenInvalidError(**args)
    if code in PERMISSION_CODES:
        return PermissionRevokedError(**args)
    if status == 429 or code in RATE_LIMIT_CODES:
        return RateLimitedError(**args, transient=True, retry_after=retry_after or RATE_LIMIT_WAIT_SECONDS)
    if transient or status in TRANSIENT_STATUSES:
        return GraphError(**args, transient=True, retry_after=retry_after)
    return GraphError(**args)


def retry_hint(exc: BaseException) -> float | None:
    if isinstance(exc, (TokenInvalidError, PermissionRevokedError, PublishRejectedError)):
        return None
    if isinstance(exc, RateLimitedError):
        return exc.retry_after or RATE_LIMIT_WAIT_SECONDS
    if isinstance(exc, GraphError) and exc.transient:
        return exc.retry_after or 0.0
    if isinstance(exc, httpx.TransportError):
        return 0.0
    return None


def appsecret_proof(user_token: str, app_secret: str) -> str:
    """Meta's appsecret_proof: HMAC-SHA256 of the token keyed with the app secret. Blocks stolen-token replay."""
    return hmac.new(app_secret.encode(), user_token.encode(), hashlib.sha256).hexdigest()


def _parse(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_success and "error" not in payload:
        return payload
    error = payload.get("error", {})
    retry_after_header = response.headers.get("Retry-After")
    try:
        retry_after = float(retry_after_header) if retry_after_header else None
    except ValueError:
        retry_after = None
    raise classify(
        status=response.status_code,
        code=error.get("code"),
        subcode=error.get("error_subcode"),
        message=error.get("message") or response.text[:300],
        fbtrace_id=error.get("fbtrace_id"),
        transient=bool(error.get("is_transient")),
        retry_after=retry_after,
    )


class GraphClient:
    def __init__(self, http: httpx.AsyncClient | None = None, api_version: str | None = None) -> None:
        settings = get_settings()
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._version = api_version or settings.organic_api_version
        self._app_secret = settings.meta_app_secret

    async def aclose(self) -> None:
        await self._http.aclose()

    def _params_with_proof(self, token: str | None, params: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(params or {})
        if token and self._app_secret:
            merged["appsecret_proof"] = appsecret_proof(token, self._app_secret)
        return merged

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        host: str = GRAPH_HOST,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{host}/{self._version}/{path.lstrip('/')}"
        headers = dict(extra_headers or {})
        if token:
            headers.setdefault("Authorization", f"Bearer {token}")
        merged_params = self._params_with_proof(token, params)

        async def attempt() -> dict[str, Any]:
            response = await self._http.request(method, url, params=merged_params, data=data, headers=headers)
            try:
                return _parse(response)
            except GraphError as error:
                logger.warning(
                    "Graph call failed",
                    extra={
                        "path": path,
                        "status": error.status,
                        "code": error.code,
                        "subcode": error.subcode,
                        "fbtrace_id": error.fbtrace_id,
                    },
                )
                raise

        return await call_with_backoff(attempt, retry_hint=retry_hint)

    async def paginate(self, path: str, *, token: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        payload = await self.request("GET", path, token=token, params=params)
        while True:
            rows.extend(payload.get("data", []))
            next_url = payload.get("paging", {}).get("next")
            if not next_url:
                return rows
            payload = await self.request("GET", next_url, token=token)

    async def upload_reel_from_url(self, video_id: str, file_url: str, page_token: str) -> None:
        """Hosted-URL upload: Meta fetches the file itself, so no binary chunks pass through this server."""
        await self.request(
            "POST",
            f"{RUPLOAD_HOST}/video-upload/{self._version}/{video_id}",
            token=None,
            extra_headers={"Authorization": f"OAuth {page_token}", "file_url": file_url},
        )
