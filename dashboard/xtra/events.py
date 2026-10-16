"""In-process pub/sub that feeds the browser's Server-Sent Events stream."""
import asyncio
import json
import time

from aiohttp import web


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._last_sent: dict[str, float] = {}

    def has_subscribers(self):
        return bool(self._subscribers)

    def publish(self, kind, data, throttle_key=None, min_interval=0.0):
        """Send an event to every connected browser.

        With throttle_key, events for the same key are dropped if they arrive
        within min_interval seconds of the previous one (used for progress).
        """
        if throttle_key is not None:
            now = time.monotonic()
            if now - self._last_sent.get(throttle_key, 0.0) < min_interval:
                return
            self._last_sent[throttle_key] = now
        msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        for q in list(self._subscribers):
            if q.qsize() < 500:  # a stalled client must not grow memory forever
                q.put_nowait(msg)

    async def stream(self, request, snapshot):
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        try:
            await resp.write(f"event: snapshot\ndata: {json.dumps(await snapshot(), default=str)}\n\n".encode())
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    msg = ": keep-alive\n\n"
                await resp.write(msg.encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.discard(q)
        return resp


bus = EventBus()
