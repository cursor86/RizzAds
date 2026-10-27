"""Turns an IterationRequest into reviewable copy variations for a winning angle.

Generated assets are stored with approved=False. Nothing generated here can reach a platform until a human
approves it through the API, so the engine never spends on copy nobody has read.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.errors import AdEngineError, LLMOutputError
from app.integrations.retry import call_with_backoff
from app.models import (
    AngleTag,
    AssetKind,
    Brand,
    CreativeAsset,
    IterationRequest,
    IterationStatus,
)

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 529})
VARIATIONS_PER_REQUEST = 3

ANGLE_BRIEFS = {
    AngleTag.PAIN_POINT: "Open on a specific frustration the buyer already feels, then show the relief.",
    AngleTag.BENEFIT: "Lead with a concrete, measurable result the buyer gets.",
    AngleTag.SOCIAL_PROOF: "Lead with a real-sounding customer experience and what they noticed.",
}


class _TransientLLMError(AdEngineError):
    def __init__(self, status: int, retry_after: float | None) -> None:
        super().__init__(f"LLM API transient status {status}")
        self.retry_after = retry_after


def _retry_hint(exc: BaseException) -> float | None:
    if isinstance(exc, _TransientLLMError):
        return exc.retry_after or 0.0
    if isinstance(exc, httpx.TransportError):
        return 0.0
    return None


def _build_prompt(angle: AngleTag, examples: list[str], brief: str) -> str:
    example_block = "\n".join(f"- {text}" for text in examples[:10]) or "- (no approved examples yet)"
    return (
        f"Write {VARIATIONS_PER_REQUEST} new ad variations in the '{angle.value}' angle.\n"
        f"Angle guidance: {ANGLE_BRIEFS[angle]}\n\n"
        f"Brand brief:\n{brief or '(none provided)'}\n\n"
        f"Approved copy that already performs for this angle:\n{example_block}\n\n"
        "Return ONLY a JSON array of exactly "
        f"{VARIATIONS_PER_REQUEST} objects with keys: \"hook\" (under 12 words, first 3 seconds of a video), "
        "\"body\" (under 90 characters, a single sentence), \"headline\" (under 40 characters). "
        "No prose, no markdown fences."
    )


def parse_variations(raw_text: str, expected: int) -> list[dict[str, str]]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or len(data) != expected:
        raise LLMOutputError(f"Expected a JSON array of {expected} items")

    variations: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise LLMOutputError("Each variation must be a JSON object")
        cleaned_item: dict[str, str] = {}
        for key in ("hook", "body", "headline"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                raise LLMOutputError(f"Variation is missing a non-empty '{key}'")
            cleaned_item[key] = value.strip()
        variations.append(cleaned_item)
    return variations


async def generate_angle_variations(
    http: httpx.AsyncClient,
    settings: Settings,
    angle: AngleTag,
    examples: list[str],
    brief: str,
) -> list[dict[str, str]]:
    if not settings.anthropic_api_key:
        raise AdEngineError("ANTHROPIC_API_KEY is not configured")

    body = {
        "model": settings.anthropic_model,
        "max_tokens": 1200,
        "system": "You write short-form paid social ad copy. You output only the JSON you are asked for.",
        "messages": [{"role": "user", "content": _build_prompt(angle, examples, brief)}],
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    async def attempt() -> dict[str, Any]:
        response = await http.post(ANTHROPIC_URL, headers=headers, json=body)
        if response.status_code in TRANSIENT_STATUSES:
            retry_after = response.headers.get("Retry-After")
            raise _TransientLLMError(response.status_code, float(retry_after) if retry_after else None)
        if not response.is_success:
            raise AdEngineError(f"LLM API error {response.status_code}: {response.text[:300]}")
        return response.json()

    payload = await call_with_backoff(attempt, retry_hint=_retry_hint)
    text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
    return parse_variations(text, VARIATIONS_PER_REQUEST)


async def process_pending_iterations(session: AsyncSession, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    pending = (
        await session.scalars(
            select(IterationRequest)
            .where(IterationRequest.status == IterationStatus.PENDING)
            .order_by(IterationRequest.id)
        )
    ).all()
    completed = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as http:
        for request in pending:
            brand = await session.get(Brand, request.brand_id)
            if brand is None:
                request.status = IterationStatus.FAILED
                request.reason = f"{request.reason} | brand missing"
                continue
            try:
                examples = list(
                    (
                        await session.scalars(
                            select(CreativeAsset.text).where(
                                CreativeAsset.brand_id == brand.id,
                                CreativeAsset.angle == request.angle,
                                CreativeAsset.approved.is_(True),
                                CreativeAsset.kind.in_([AssetKind.BODY, AssetKind.HOOK]),
                            )
                        )
                    ).all()
                )
                variations = await generate_angle_variations(
                    http, settings, request.angle, [e for e in examples if e], brand.brief
                )
                for variation in variations:
                    session.add(
                        CreativeAsset(
                            brand_id=brand.id,
                            angle=request.angle,
                            kind=AssetKind.HOOK,
                            text=variation["hook"],
                            approved=False,
                            source="llm",
                        )
                    )
                    session.add(
                        CreativeAsset(
                            brand_id=brand.id,
                            angle=request.angle,
                            kind=AssetKind.BODY,
                            text=variation["body"],
                            approved=False,
                            source="llm",
                        )
                    )
                    session.add(
                        CreativeAsset(
                            brand_id=brand.id,
                            angle=request.angle,
                            kind=AssetKind.HEADLINE,
                            text=variation["headline"],
                            approved=False,
                            source="llm",
                        )
                    )
                request.status = IterationStatus.DONE
                request.completed_at = datetime.now(timezone.utc)
                completed += 1
            except Exception as exc:
                logger.exception("Iteration %s failed", request.id)
                request.status = IterationStatus.FAILED
                request.reason = f"{request.reason} | failed: {exc}"[:2000]

    await session.commit()
    return completed
