"""Organic publishing worker.

Two loops run here:
  * Every 30 seconds: process due publish jobs (claimed with SKIP LOCKED, so several workers can run).
  * Every 24 hours: verify each client's token with Meta, and refresh any token close to its 60-day expiry.

Usage:
  python -m app.organic.worker --once   # one pass of each loop, then exit (for cron)
  python -m app.organic.worker          # run continuously
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from datetime import datetime, timezone

from app.db import SessionLocal, engine
from app.models import Base
from app.config import get_settings
from app.organic.graph import GraphClient
from app.organic.service import check_credentials, process_due_jobs

logger = logging.getLogger("adengine.organic.worker")

JOB_POLL_SECONDS = 30
CREDENTIAL_SWEEP_SECONDS = 24 * 60 * 60


async def run_jobs_once() -> int:
    graph = GraphClient()
    try:
        async with SessionLocal() as session:
            return await process_due_jobs(session, graph, now=datetime.now(timezone.utc))
    finally:
        await graph.aclose()


async def run_credential_sweep_once() -> dict[str, int]:
    graph = GraphClient()
    try:
        async with SessionLocal() as session:
            counts = await check_credentials(session, graph, now=datetime.now(timezone.utc))
            logger.info("Credential sweep finished", extra=counts)
            return counts
    finally:
        await graph.aclose()


async def _run_forever() -> None:
    last_sweep = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            await run_jobs_once()
        except Exception:
            logger.exception("Job pass crashed; continuing on the next tick")
        if loop.time() - last_sweep >= CREDENTIAL_SWEEP_SECONDS:
            try:
                await run_credential_sweep_once()
            except Exception:
                logger.exception("Credential sweep crashed; it will run again on schedule")
            last_sweep = loop.time()
        await asyncio.sleep(JOB_POLL_SECONDS + random.uniform(0, 5))


async def _main(once: bool) -> None:
    if get_settings().auto_create_schema:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    try:
        if once:
            await run_jobs_once()
            await run_credential_sweep_once()
        else:
            await _run_forever()
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="AdEngine-AI organic publishing worker")
    parser.add_argument("--once", action="store_true", help="run one pass of each loop and exit")
    args = parser.parse_args()
    asyncio.run(_main(once=args.once))


if __name__ == "__main__":
    main()
