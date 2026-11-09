"""Background worker tasks (demo).

The deployed worker imports the optional redis dependency lazily; this demo
keeps the same layout so examples/demo.log resolves back to this file.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

RETRY_DELAY_SECONDS = 30


def handle_job(job: dict) -> None:
    logger.info("processing job %s", job.get("id"))
    time.sleep(0.01)


def run(job: dict) -> None:
    try:
        import redis

        client = redis.Redis.from_url("redis://cache:6379/0")
        client.set(job["id"], "done")
    except ImportError:
        logger.exception("job failed")
        raise
