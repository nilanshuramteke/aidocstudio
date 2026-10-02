"""In-process pub/sub. Publishers may be any thread; subscribers are asyncio queues."""
import asyncio
import threading


class EventBus:
    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(loop, s) for (loop, s) in self._subs if s is not q]

    def publish(self, event: str, data: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            def _put(q=q):
                if q.full():
                    q.get_nowait()
                q.put_nowait((event, data))
            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:  # loop closed
                pass
