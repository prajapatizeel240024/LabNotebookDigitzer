"""Worker process: takes page jobs from Redis and runs them. Scale with N copies.

python -m lnd.worker
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from .config import get_settings
from .db import close_pool, init_schema
from .gateway import LLMGateway
from .pipeline import finalize_dead, process_job
from .queue import JobQueue, connect
from .sandbox_client import SandboxClient

log = logging.getLogger("lnd.worker")


def make_queue() -> JobQueue:
    s = get_settings()
    return JobQueue(
        connect(s.redis_url),
        max_attempts=s.max_attempts,
        backoff_base_s=s.backoff_base_s,
        backoff_cap_s=s.backoff_cap_s,
    )


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.queue = make_queue()
        self.gateway = LLMGateway()
        self.sandbox = SandboxClient()
        self.name = f"{socket.gethostname()}-{os.getpid()}"
        self._stop = False
        self._last_reap = 0.0

    def stop(self, *_: object) -> None:
        self._stop = True

    def _reap(self) -> None:
        if time.monotonic() - self._last_reap < 1.0:
            return
        self._last_reap = time.monotonic()
        dead = self.queue.reap()
        if dead:
            finalize_dead(dead)

    def run(self) -> None:
        log.info("worker %s started", self.name)
        # Compose starts workers once the model server is healthy, but Docker's restart policy
        # (after a Docker or host restart) brings every container back at once. Jobs taken while
        # the model is still loading would use up their attempts on 503s within seconds.
        if not self.gateway.model_ready():
            log.info("waiting for the model server to be ready")
            while not self._stop and not self.gateway.model_ready():
                time.sleep(2.0)
        idle = 0.02
        while not self._stop:
            self._reap()
            job = self.queue.pop(self.settings.lease_s)
            if job is None:
                time.sleep(idle)
                idle = min(idle * 2, 0.25)
                continue
            idle = 0.02
            process_job(job, self.queue, self.gateway, self.sandbox, self.settings)
        log.info("worker %s stopped", self.name)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_schema()
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    try:
        worker.run()
    finally:
        close_pool()


if __name__ == "__main__":
    main()
