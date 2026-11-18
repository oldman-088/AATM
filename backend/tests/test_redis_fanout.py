"""The Redis event subscriber survives a lost connection without a traceback."""

from __future__ import annotations

import asyncio
import json
import logging

from app.coordination.redis_backend import RedisCoordinator


class _PubSub:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    async def subscribe(self, _channel: str) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def listen(self):
        if self.fail:
            raise TimeoutError("Timeout reading from redis")
        yield {"type": "message", "data": json.dumps({"origin": "other", "event": {"n": 1}})}
        await asyncio.sleep(30)


async def test_subscriber_reconnects_and_keeps_delivering(monkeypatch, caplog):
    real_sleep = asyncio.sleep
    monkeypatch.setattr("app.coordination.redis_backend.asyncio.sleep",
                        lambda delay: real_sleep(0 if delay < 30 else delay))
    coordinator = RedisCoordinator.__new__(RedisCoordinator)
    coordinator.instance_id = "me"
    sources = iter([_PubSub(True), _PubSub(False)])
    coordinator._subscriber = type("Client", (), {"pubsub": lambda _self: next(sources)})()
    received: asyncio.Queue = asyncio.Queue()

    async def handler(event):
        await received.put(event)

    with caplog.at_level(logging.WARNING):
        task = await coordinator.subscribe_events(handler)
        assert await asyncio.wait_for(received.get(), 2) == {"n": 1}
        task.cancel()
    assert "lost its connection" in caplog.text and "Traceback" not in caplog.text
