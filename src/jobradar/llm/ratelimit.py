"""A requests-per-minute limiter shared by the threads that call an LLM.

Providers enforce rate limits and answer HTTP 429 ("too many requests") when we exceed them. The
SDKs retry those, but each retry wastes time; spacing calls evenly avoids hitting the limit at all.
"""

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """Hands out evenly spaced time slots: at most `requests_per_minute` acquire() calls a minute.

    Thread-safe: the enrich pipeline calls acquire() from several worker threads, and the lock makes
    "read the next free slot, then move it forward" one indivisible step, so two threads can never
    take the same slot.
    """

    def __init__(
        self,
        requests_per_minute: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # clock and sleep are injectable so tests run instantly with a fake clock. monotonic() never
        # goes backwards (unlike the wall clock, which can jump when the system time is adjusted).
        self._interval = 60.0 / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = clock()

    def acquire(self) -> None:
        """Block until this caller's slot comes up."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        # Sleep outside the lock, so other threads can reserve the following slots meanwhile.
        wait = slot - now
        if wait > 0:
            self._sleep(wait)
