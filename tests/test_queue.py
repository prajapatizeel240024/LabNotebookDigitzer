import threading
import time

import pytest
import redis as redis_lib

from lnd.queue import JobQueue

from .conftest import TEST_REDIS


@pytest.fixture
def q(redis_service):
    client = redis_lib.Redis.from_url(TEST_REDIS, decode_responses=True)
    queue = JobQueue(
        client, prefix="lnd:test:q", max_attempts=3, backoff_base_s=0.05, backoff_cap_s=0.2
    )
    queue.clear()
    yield queue
    queue.clear()


def job(n: int) -> dict:
    return {"page_id": f"p{n}", "document_id": "d1", "page_no": n, "tenant": "t"}


def test_fifo_and_ack(q):
    q.enqueue_many([job(1), job(2), job(3)])
    got = [q.pop(30) for _ in range(3)]
    assert [g.data["page_no"] for g in got] == [1, 2, 3]
    assert q.pop(30) is None
    assert q.stats()["inflight"] == 3
    for g in got:
        q.ack(g)
    assert q.stats() == {"ready": 0, "delayed": 0, "inflight": 0, "dead": 0}


def test_failed_job_waits_out_backoff_then_runs_again(q):
    q.enqueue(job(1))
    j = q.pop(30)
    assert q.fail(j, "boom") == "retry"
    assert q.stats()["delayed"] == 1
    assert q.pop(30) is None  # still backing off
    time.sleep(0.4)
    q.reap()
    again = q.pop(30)
    assert again is not None
    assert again.attempts == 1
    assert again.data["last_error"] == "boom"


def test_three_failures_go_to_the_dead_letter_list(q):
    q.enqueue(job(7))
    outcomes = []
    for _ in range(3):
        deadline = time.time() + 3
        j = None
        while j is None and time.time() < deadline:
            q.reap()
            j = q.pop(30)
            time.sleep(0.02)
        assert j is not None
        outcomes.append(q.fail(j, "still broken"))
    assert outcomes == ["retry", "retry", "dead"]
    assert q.stats() == {"ready": 0, "delayed": 0, "inflight": 0, "dead": 1}
    dead = q.dead_jobs()[0]
    assert dead["page_id"] == "p7"
    assert dead["attempts"] == 3
    assert dead["last_error"] == "still broken"


def test_backoff_grows_and_is_capped(q):
    q.backoff_base_s, q.backoff_cap_s = 1.0, 5.0
    first, second, fifth = q.backoff_s(1), q.backoff_s(2), q.backoff_s(5)
    assert 0.8 <= first <= 1.2
    assert 1.6 <= second <= 2.4
    assert fifth <= 5.0 * 1.2


def test_expired_lease_returns_job_and_counts_an_attempt(q):
    q.enqueue(job(1))
    q.pop(0.05)  # a worker takes it, then "crashes"
    time.sleep(0.15)
    assert q.reap() == []
    assert q.stats()["ready"] == 1
    j = q.pop(30)
    assert j.attempts == 1
    assert "lease expired" in j.data["last_error"]


def test_expired_lease_on_last_attempt_goes_dead_and_is_reported(q):
    q.enqueue(job(1))
    for _ in range(3):
        j = q.pop(0.05)
        assert j is not None
        time.sleep(0.12)
        dead = q.reap()
        if dead:
            break
    assert len(dead) == 1 and dead[0]["page_id"] == "p1"
    assert q.stats()["dead"] == 1


def test_late_failure_after_lease_loss_does_not_double_schedule(q):
    q.enqueue(job(1))
    j = q.pop(0.05)
    time.sleep(0.12)
    q.reap()  # lease expired, job is back on ready
    q.fail(j, "late")  # the original worker reports in after all
    assert q.stats()["delayed"] == 0
    assert q.stats()["ready"] == 1


def test_many_workers_never_get_the_same_job(q):
    total = 400
    q.enqueue_many([job(i) for i in range(total)])
    taken: list[str] = []
    lock = threading.Lock()

    def drain() -> None:
        while True:
            j = q.pop(60)
            if j is None:
                return
            with lock:
                taken.append(j.data["page_id"])
            q.ack(j)

    threads = [threading.Thread(target=drain) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(taken) == total
    assert len(set(taken)) == total


def test_purge_dead_removes_only_that_page(q):
    for n in (1, 2):
        q.enqueue(job(n))
    for _ in range(2):
        j = q.pop(30)
        q.max_attempts = 1
        q.fail(j, "x")
    assert q.stats()["dead"] == 2
    assert q.purge_dead("p1") == 1
    assert [d["page_id"] for d in q.dead_jobs()] == ["p2"]
