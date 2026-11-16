"""Item C optional integration tests against real RabbitMQ / Redis / Postgres.

Skipped unless the matching URL is set, e.g. (localhost-only throwaway containers):

    docker run -d --rm --name aatm-it-redis -p 127.0.0.1:57379:6379 redis:7-alpine
    docker run -d --rm --name aatm-it-rmq -p 127.0.0.1:57672:5672 rabbitmq:4-management-alpine
    docker run -d --rm --name aatm-it-pg -e POSTGRES_PASSWORD=<password> -p 127.0.0.1:57432:5432 postgres:16-alpine
    AATM_TEST_REDIS_URL=redis://127.0.0.1:57379/0 \\
    AATM_TEST_RABBITMQ_URL=amqp://<user>:<password>@127.0.0.1:57672/  (image default user: guest) \\
    AATM_TEST_POSTGRES_URL=postgresql://postgres:<password>@127.0.0.1:57432/postgres \\
    .venv/bin/pytest backend/tests/test_c_broker_integration.py
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

REDIS_URL = os.environ.get("AATM_TEST_REDIS_URL")
RABBITMQ_URL = os.environ.get("AATM_TEST_RABBITMQ_URL")
POSTGRES_URL = os.environ.get("AATM_TEST_POSTGRES_URL")


@pytest.mark.skipif(not REDIS_URL, reason="AATM_TEST_REDIS_URL not set (Redis is optional)")
async def test_redis_lock_rate_limit_and_cross_process_fanout():
    from app.coordination.redis_backend import LockNotAcquired, RedisCoordinator

    a, b = RedisCoordinator(REDIS_URL), RedisCoordinator(REDIS_URL)
    try:
        key = f"it-{uuid.uuid4().hex}"
        async with a.lock(key, ttl_seconds=5):
            with pytest.raises(LockNotAcquired):
                async with b.lock(key, ttl_seconds=5):
                    pass
        async with b.lock(key, ttl_seconds=5):
            pass
        results = [await a.hit(f"rl-{key}", limit=3, window_seconds=5) for _ in range(5)]
        assert [ok for ok, _ in results] == [True, True, True, False, False]

        received: list[dict] = []
        got = asyncio.Event()

        async def handler(event):
            received.append(event)
            got.set()

        sub = await b.subscribe_events(handler)
        await asyncio.sleep(0.2)
        await a.publish_event({"type": "it.ping", "payload": {"k": key}})
        await asyncio.wait_for(got.wait(), timeout=5)
        assert received[0]["payload"]["k"] == key
        sub.cancel()
        await asyncio.gather(sub, return_exceptions=True)
    finally:
        await a.close()
        await b.close()


@pytest.mark.skipif(not RABBITMQ_URL, reason="AATM_TEST_RABBITMQ_URL not set (RabbitMQ is optional)")
async def test_rabbitmq_durable_queue_ack_after_commit_redelivery_and_dead_letter():
    from app.queue.base import DEAD_LETTER_QUEUE
    from app.queue.rabbitmq import RabbitMQWakeBus

    device = f"it-{uuid.uuid4().hex[:8]}"
    queue = f"aatm.tasks.{device}"
    bus = RabbitMQWakeBus(RABBITMQ_URL)
    try:
        await bus.publish_task(device, "task-1")
        first = await bus.receive(queue, timeout=5)
        assert first is not None and first.payload["task_id"] == "task-1"
        # Consumer "crashes" before its claim committed: no ack -> message is redelivered.
        await bus.stop()
        bus = RabbitMQWakeBus(RABBITMQ_URL)
        again = await bus.receive(queue, timeout=5)
        assert again is not None and again.payload["task_id"] == "task-1"
        await again.ack()
        assert await bus.receive(queue, timeout=0.5) is None
        assert await bus.queue_depth(queue) == 0

        await bus.publish_task(device, "poison")
        poison = await bus.receive(queue, timeout=5)
        before = await bus.queue_depth(DEAD_LETTER_QUEUE)
        await poison.reject_to_dead_letter()
        await asyncio.sleep(0.3)
        assert await bus.queue_depth(DEAD_LETTER_QUEUE) == before + 1
    finally:
        await bus.stop()


@pytest.mark.skipif(not POSTGRES_URL, reason="AATM_TEST_POSTGRES_URL not set")
async def test_postgres_listen_notify_fallback_wakes_other_connection():
    from app.queue.postgres import PostgresNotifyWakeBus

    sender, listener = PostgresNotifyWakeBus(POSTGRES_URL), PostgresNotifyWakeBus(POSTGRES_URL)
    try:
        await listener.start()
        await sender.publish_task("device-local", "pg-task")
        msg = await listener.receive("aatm.tasks.device-local", timeout=5)
        assert msg is not None and msg.payload["task_id"] == "pg-task"
    finally:
        await sender.stop()
        await listener.stop()
