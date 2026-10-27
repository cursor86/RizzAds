"""Background worker: runs the optimization cycle and iteration generation every 24 hours.

Usage:
  python -m app.worker --once     # run one cycle and exit (use this from an external scheduler such as cron)
  python -m app.worker            # run forever, one cycle every 24 hours
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from datetime import datetime, timezone

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Base
from app.services.iteration import process_pending_iterations
from app.services.optimization_loop import run_optimization_cycle

logger = logging.getLogger("adengine.worker")

CYCLE_INTERVAL_SECONDS = 24 * 60 * 60


async def run_once() -> dict[str, object]:
    settings = get_settings()
    started = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        summary = await run_optimization_cycle(session, settings, now=started)
    async with SessionLocal() as session:
        generated = await process_pending_iterations(session, settings)

    result: dict[str, object] = {
        "started_at": started.isoformat(),
        "paused": summary.paused,
        "scaled": summary.scaled,
        "iterations_requested": summary.iterations_requested,
        "iterations_generated": generated,
        "errors": summary.errors,
    }
    logger.info("Cycle finished: %s", result)
    return result


async def run_forever() -> None:
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("Cycle crashed; the next cycle runs on schedule")
        jitter = random.uniform(0, 600)  # spread load if several workers start together
        await asyncio.sleep(CYCLE_INTERVAL_SECONDS + jitter)


async def _main(once: bool) -> None:
    if get_settings().auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    if once:
        await run_once()
    else:
        await run_forever()
    await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="AdEngine-AI optimization worker")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()
    asyncio.run(_main(once=args.once))


if __name__ == "__main__":
    main()
