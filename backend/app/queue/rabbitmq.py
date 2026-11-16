"""RabbitMQ wake bus (aio-pika): durable queues, ack-after-commit, dead-letter exchange."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.queue.base import DEAD_LETTER_QUEUE, EVENTS_QUEUE, MANAGER_QUEUE, WakeBus, WakeMessage, encode

logger = logging.getLogger(__name__)

EXCHANGE = "aatm"
DLX = "aatm.dlx"


class RabbitMQWakeBus(WakeBus):
    backend = "rabbitmq"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self._connection = None
        self._channel = None
        self._exchange = None
        self._declared: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        import aio_pika

        async with self._lock:
            if self._connection is not None and not self._connection.is_closed:
                return
            self._connection = await aio_pika.connect_robust(self.url, timeout=10)
            self._channel = await self._connection.channel(publisher_confirms=True)
            await self._channel.set_qos(prefetch_count=16)
            self._exchange = await self._channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
            dlx = await self._channel.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
            dlq = await self._channel.declare_queue(DEAD_LETTER_QUEUE, durable=True)
            await dlq.bind(dlx)
            self._declared[DEAD_LETTER_QUEUE] = dlq
            for name in (MANAGER_QUEUE, EVENTS_QUEUE):
                await self._declare(name)

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = self._channel = self._exchange = None
        self._declared.clear()

    async def _declare(self, name: str):
        if name in self._declared:
            return self._declared[name]
        queue = await self._channel.declare_queue(
            name, durable=True, arguments={"x-dead-letter-exchange": DLX}
        )
        await queue.bind(self._exchange, routing_key=name)
        self._declared[name] = queue
        return queue

    async def publish(self, queue: str, payload: dict[str, Any]) -> None:
        import aio_pika

        await self.start()
        if queue == DEAD_LETTER_QUEUE:
            dlx = await self._channel.get_exchange(DLX)
            await dlx.publish(aio_pika.Message(encode(payload), delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key="")
        else:
            await self._declare(queue)
            await self._exchange.publish(
                aio_pika.Message(
                    encode(payload),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                ),
                routing_key=queue,
            )
        self.published += 1

    async def receive(self, queue: str, timeout: float) -> WakeMessage | None:
        await self.start()
        q = await self._declare(queue)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            message = await q.get(no_ack=False, fail=False)
            if message is not None:
                try:
                    payload = json.loads(message.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await message.reject(requeue=False)  # → dead-letter exchange
                    continue
                return WakeMessage(
                    queue=queue,
                    payload=payload,
                    _ack=message.ack,
                    _nack=lambda m=message: m.reject(requeue=False),
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.05, remaining))

    async def queue_depth(self, queue: str) -> int:
        """Ready (un-delivered) messages. Uses a fresh channel: a reused robust channel can report a stale count."""
        await self.start()
        channel = await self._connection.channel()
        try:
            declared = await channel.declare_queue(queue, durable=True, passive=True)
            return int(declared.declaration_result.message_count or 0)
        finally:
            await channel.close()
