"""Background worker: `python -m app.worker` (loop) or `python -m app.worker --once`.

Run as many copies as you like; the queue's compare-and-set claim keeps them from doubling up.
"""

import argparse
import logging
import os
import socket
import time

from app.clock import utcnow
from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.jobs import RunStats, run_due
from app.reminders import HANDLERS

log = logging.getLogger("permitpilot.worker")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def run_once() -> RunStats:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    return run_due(
        factory,
        HANDLERS,
        worker_id(),
        utcnow(),
        lease_seconds=settings.job_lease_seconds,
        backoff_base=settings.job_backoff_base_seconds,
        backoff_cap=settings.job_backoff_cap_seconds,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="PermitPilot reminder worker")
    parser.add_argument("--once", action="store_true", help="process due jobs once and exit")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between polls")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        stats = run_once()
        if stats.claimed:
            log.info(
                "claimed=%d done=%d retried=%d dead=%d",
                stats.claimed,
                stats.done,
                stats.retried,
                stats.dead,
            )
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
