import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Returns None when the exception must not be retried, 0.0 to use exponential backoff,
# or a positive number of seconds to wait at least that long (for example a Retry-After value).
RetryHint = Callable[[BaseException], float | None]


async def call_with_backoff(
    operation: Callable[[], Awaitable[T]],
    *,
    retry_hint: RetryHint,
    max_attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 300.0,
) -> T:
    """Run `operation`, retrying transient failures with exponential backoff and jitter."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            hint = retry_hint(exc)
            if hint is None or attempt >= max_attempts:
                raise
            backoff = base_delay * (2 ** (attempt - 1))
            delay = min(max_delay, hint if hint > 0 else backoff)
            delay += random.uniform(0, delay * 0.1)
            logger.warning(
                "Transient API failure (attempt %d/%d). Retrying in %.1fs. %s",
                attempt,
                max_attempts,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
