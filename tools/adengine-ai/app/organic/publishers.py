"""Instagram and Facebook Reel publishing pipelines.

Instagram (container model):
  1. POST /{ig-user-id}/media            media_type=REELS, video_url, caption  -> creation_id
  2. GET  /{creation_id}?fields=status_code  until FINISHED (exponential backoff, capped total wait)
  3. POST /{ig-user-id}/media_publish    creation_id                           -> live media id

Facebook (Reels upload model, hosted URL):
  1. POST /{page-id}/video_reels  upload_phase=start                          -> video_id
  2. POST rupload /video-upload/{v}/{video_id}  with file_url header          (Meta fetches the file)
  3. POST /{page-id}/video_reels  upload_phase=finish, video_state=PUBLISHED  -> success
  4. GET  /{video_id}?fields=status  until publishing is complete

The video URL must be publicly reachable over https. Meta fetches it, so private storage links will fail.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Iterator

from app.config import get_settings
from app.organic.graph import GraphClient, PublishRejectedError

logger = logging.getLogger("adengine.organic.publish")

BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 60.0
IG_FINISHED = "FINISHED"
IG_FAILED_STATES = frozenset({"ERROR", "EXPIRED"})


class PublishTimeoutError(Exception):
    """Processing did not finish within the polling budget. The job is retried later."""


def backoff_delays(base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS) -> Iterator[float]:
    """Yields base, 2*base, 4*base, ... capped at `cap`, with +/-10% jitter to avoid synchronized polling."""
    delay = base
    while True:
        jitter = random.uniform(0.9, 1.1)
        yield min(cap, delay) * jitter
        delay = min(cap, delay * 2)


class InstagramPublisher:
    def __init__(self, graph: GraphClient, ig_user_id: str, token: str, max_wait_seconds: float | None = None) -> None:
        self._graph = graph
        self._ig_user_id = ig_user_id
        self._token = token
        self._max_wait = max_wait_seconds or get_settings().organic_poll_max_seconds

    async def create_reel_container(self, video_url: str, caption: str) -> str:
        payload = await self._graph.request(
            "POST",
            f"{self._ig_user_id}/media",
            token=self._token,
            data={"media_type": "REELS", "video_url": video_url, "caption": caption},
        )
        return str(payload["id"])

    async def wait_until_finished(self, creation_id: str, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        deadline = clock() + self._max_wait
        delays = backoff_delays()
        while True:
            payload = await self._graph.request(
                "GET", creation_id, token=self._token, params={"fields": "status_code,status"}
            )
            code = payload.get("status_code")
            if code == IG_FINISHED:
                return
            if code in IG_FAILED_STATES:
                detail = payload.get("status") or code
                raise PublishRejectedError(
                    status=200, code=None, subcode=None,
                    message=f"Instagram could not process the video: {detail}", fbtrace_id=None,
                )
            if clock() >= deadline:
                raise PublishTimeoutError(f"Container {creation_id} still {code} after {self._max_wait:.0f}s")
            await sleep(next(delays))

    async def publish_container(self, creation_id: str) -> str:
        payload = await self._graph.request(
            "POST", f"{self._ig_user_id}/media_publish", token=self._token, data={"creation_id": creation_id}
        )
        return str(payload["id"])

    async def publish_reel(self, video_url: str, caption: str, on_container=None) -> tuple[str, str]:
        """Runs all three steps. `on_container` is called with the creation id so the job can record it."""
        creation_id = await self.create_reel_container(video_url, caption)
        if on_container is not None:
            on_container(creation_id)
        await self.wait_until_finished(creation_id)
        media_id = await self.publish_container(creation_id)
        logger.info("Instagram Reel published", extra={"ig_user_id": self._ig_user_id, "media_id": media_id})
        return creation_id, media_id


class FacebookReelPublisher:
    def __init__(self, graph: GraphClient, page_id: str, page_token: str, max_wait_seconds: float | None = None) -> None:
        self._graph = graph
        self._page_id = page_id
        self._token = page_token
        self._max_wait = max_wait_seconds or get_settings().organic_poll_max_seconds

    async def start(self) -> str:
        payload = await self._graph.request(
            "POST", f"{self._page_id}/video_reels", token=self._token, data={"upload_phase": "start"}
        )
        return str(payload["video_id"])

    async def finish(self, video_id: str, description: str) -> None:
        payload = await self._graph.request(
            "POST",
            f"{self._page_id}/video_reels",
            token=self._token,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": description,
            },
        )
        if not payload.get("success", False):
            raise PublishRejectedError(
                status=200, code=None, subcode=None, message="Facebook did not confirm the Reel", fbtrace_id=None
            )

    async def wait_until_published(self, video_id: str, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        deadline = clock() + self._max_wait
        delays = backoff_delays()
        while True:
            payload = await self._graph.request("GET", video_id, token=self._token, params={"fields": "status"})
            status = payload.get("status", {})
            publishing = status.get("publishing_phase", {}).get("status")
            if status.get("video_status") == "error" or status.get("processing_phase", {}).get("status") == "error":
                raise PublishRejectedError(
                    status=200, code=None, subcode=None,
                    message=f"Facebook could not process the Reel: {status}", fbtrace_id=None,
                )
            if publishing == "complete":
                return
            if clock() >= deadline:
                raise PublishTimeoutError(f"Facebook Reel {video_id} not published after {self._max_wait:.0f}s")
            await sleep(next(delays))

    async def publish_reel(self, video_url: str, description: str, on_video=None) -> str:
        video_id = await self.start()
        if on_video is not None:
            on_video(video_id)
        await self._graph.upload_reel_from_url(video_id, video_url, self._token)
        await self.finish(video_id, description)
        await self.wait_until_published(video_id)
        logger.info("Facebook Reel published", extra={"page_id": self._page_id, "video_id": video_id})
        return video_id
