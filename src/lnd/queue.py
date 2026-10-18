"""A small reliable job queue on Redis.

Keys (all under one prefix):
    ready     list    jobs waiting to run (LPUSH to add, RPOP to take, so FIFO)
    delayed   zset    jobs waiting out a retry backoff, scored by the time they are due
    inflight  zset    jobs a worker has taken, scored by the time its lease runs out
    dead      list    jobs that failed max_attempts times (the dead-letter list)

A worker takes a job and gets a lease. It acks the job when done. If it crashes, the lease
runs out and any worker's reaper puts the job back on ready (counted as a failed attempt),
or on dead once attempts run out. Every state change is one Lua script, so a job is never
in two places at once and is never lost between steps. Time comes from Redis (TIME), so
workers with skewed clocks agree.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from typing import Any

import redis

_POP = """
local j = redis.call('RPOP', KEYS[1])
if not j then return false end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[1]), j)
return j
"""

_RETRY = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then return 0 end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[3]), ARGV[2])
return 1
"""

_DEAD = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[2])
return 1
"""

_REAP = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local limit = tonumber(ARGV[2])
local due = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now, 'LIMIT', 0, limit)
for _, j in ipairs(due) do
  redis.call('ZREM', KEYS[2], j)
  redis.call('LPUSH', KEYS[1], j)
end
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, limit)
local dead = {}
for _, j in ipairs(expired) do
  if redis.call('ZREM', KEYS[3], j) == 1 then
    local job = cjson.decode(j)
    job.attempts = job.attempts + 1
    job.last_error = 'lease expired (worker crashed or stalled)'
    local nj = cjson.encode(job)
    if job.attempts >= tonumber(ARGV[1]) then
      redis.call('LPUSH', KEYS[4], nj)
      table.insert(dead, nj)
    else
      redis.call('LPUSH', KEYS[1], nj)
    end
  end
end
return dead
"""


@dataclass
class Job:
    """A job as taken from the queue. `raw` is the exact string stored in Redis."""

    raw: str
    data: dict[str, Any]

    @property
    def attempts(self) -> int:
        return int(self.data.get("attempts", 0))


class JobQueue:
    def __init__(
        self,
        client: redis.Redis,
        *,
        prefix: str = "lnd:q",
        max_attempts: int = 3,
        backoff_base_s: float = 2.0,
        backoff_cap_s: float = 60.0,
    ) -> None:
        self.r = client
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.k_ready = f"{prefix}:ready"
        self.k_delayed = f"{prefix}:delayed"
        self.k_inflight = f"{prefix}:inflight"
        self.k_dead = f"{prefix}:dead"
        self._pop = client.register_script(_POP)
        self._retry = client.register_script(_RETRY)
        self._dead = client.register_script(_DEAD)
        self._reap = client.register_script(_REAP)

    # ---- producing -------------------------------------------------------------------------
    @staticmethod
    def _new(job: dict[str, Any]) -> str:
        body = {**job, "attempts": 0, "uid": uuid.uuid4().hex}
        return json.dumps(body, separators=(",", ":"), sort_keys=True)

    def enqueue(self, job: dict[str, Any]) -> None:
        self.r.lpush(self.k_ready, self._new(job))

    def enqueue_many(self, jobs: list[dict[str, Any]]) -> None:
        if not jobs:
            return
        pipe = self.r.pipeline()
        for chunk_start in range(0, len(jobs), 500):
            pipe.lpush(self.k_ready, *[self._new(j) for j in jobs[chunk_start : chunk_start + 500]])
        pipe.execute()

    # ---- consuming -------------------------------------------------------------------------
    def pop(self, lease_s: float) -> Job | None:
        raw = self._pop(keys=[self.k_ready, self.k_inflight], args=[lease_s])
        if not raw:
            return None
        return Job(raw=raw, data=json.loads(raw))

    def ack(self, job: Job) -> None:
        self.r.zrem(self.k_inflight, job.raw)

    def backoff_s(self, failures: int) -> float:
        base = min(self.backoff_cap_s, self.backoff_base_s * (2 ** max(0, failures - 1)))
        return base * random.uniform(0.8, 1.2)

    def fail(self, job: Job, error: str) -> str:
        """Record a failed attempt. Returns 'retry' or 'dead'."""
        failures = job.attempts + 1
        updated = {**job.data, "attempts": failures, "last_error": error[:500]}
        new_raw = json.dumps(updated, separators=(",", ":"), sort_keys=True)
        if failures >= self.max_attempts:
            self._dead(keys=[self.k_inflight, self.k_dead], args=[job.raw, new_raw])
            return "dead"
        self._retry(
            keys=[self.k_inflight, self.k_delayed],
            args=[job.raw, new_raw, self.backoff_s(failures)],
        )
        return "retry"

    def reap(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Move due retries to ready and expired leases back. Returns jobs that went dead."""
        dead = self._reap(
            keys=[self.k_ready, self.k_delayed, self.k_inflight, self.k_dead],
            args=[self.max_attempts, limit],
        )
        return [json.loads(d) for d in dead or []]

    # ---- inspection ------------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        pipe = self.r.pipeline()
        pipe.llen(self.k_ready)
        pipe.zcard(self.k_delayed)
        pipe.zcard(self.k_inflight)
        pipe.llen(self.k_dead)
        ready, delayed, inflight, dead = pipe.execute()
        return {"ready": ready, "delayed": delayed, "inflight": inflight, "dead": dead}

    def dead_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [json.loads(x) for x in self.r.lrange(self.k_dead, 0, limit - 1)]

    def purge_dead(self, page_id: str) -> int:
        """Drop dead-letter entries for one page (used when the page is retried by hand)."""
        removed = 0
        for raw in self.r.lrange(self.k_dead, 0, -1):
            if json.loads(raw).get("page_id") == page_id:
                removed += int(self.r.lrem(self.k_dead, 0, raw))
        return removed

    def clear(self) -> None:
        self.r.delete(self.k_ready, self.k_delayed, self.k_inflight, self.k_dead)


def connect(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True)
