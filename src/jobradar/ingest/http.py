"""HTTP access for the extractors: one polite, retrying client shared by every source.

Concepts:
- **Transient vs permanent errors.** A 429 (rate limited), a 5xx (server trouble) or a dropped
  connection may well succeed a few seconds later, so those are retried. A 4xx such as 404 or 400
  means the request itself is wrong; retrying would only repeat the same answer, so it raises now.
- **Exponential backoff with jitter.** Wait 1 s, 2 s, 4 s… between attempts, so a struggling server
  gets more room each time, plus a small random extra ("jitter") so many clients that failed
  together don't all retry at the same instant.
- **Retry-After.** A server that rate-limits often says how long to wait; that beats our own guess.
- **Politeness.** At least `min_interval` seconds between consecutive requests, and a user agent
  that says who we are and where to find us. Public APIs are free because clients behave.
"""

import logging
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Identifies the client to the API owners, with a link in case they need to contact us.
USER_AGENT = "jobradar/0.1 (+https://github.com/nachhh/jobradar)"

# 429 Too Many Requests, plus the 5xx codes that usually mean "try again later".
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Upper bound on any single wait, so a server asking for "Retry-After: 3600" fails the source
# instead of stalling the run for an hour.
MAX_RETRY_DELAY = 60.0

# Parameter values may be lists: httpx repeats the key, e.g. {"expand[]": ["a", "b"]} becomes
# ?expand[]=a&expand[]=b, the form GetOnBoard expects.
Params = Mapping[str, str | int | list[str]]


def make_client(timeout: float = 30.0) -> httpx.Client:
    """An httpx client with our user agent and a timeout.

    httpx's default timeout is 5 s, short for pages close to 1 MB (GetOnBoard's are); `timeout`
    still makes a stuck request fail instead of hanging the whole run. The client keeps
    connections open between requests (connection pooling), which matters when paging.
    """
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
        follow_redirects=True,
    )


class Fetcher:
    """GETs JSON with retries, backoff and a minimum interval between requests.

    `sleep` and `clock` are parameters so tests can pass fakes and run instantly; production code
    uses the defaults (real time). This is dependency injection at its smallest.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        min_interval: float = 1.0,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self._sleep = sleep
        # monotonic(), not time(): it never jumps backwards when the system clock is adjusted,
        # so it's the right clock for measuring intervals.
        self._clock = clock
        self._last_request_at: float | None = None
        self.requests_made = 0  # every attempt, retries included; handy in logs and tests

    def get_json(self, url: str, params: Params | None = None) -> Any:
        """GET `url` and return the decoded JSON body. Raises once retries are exhausted."""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_turn()
            try:
                response = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                # TransportError covers timeouts, refused connections and dropped connections:
                # the request never got a response, so trying again is reasonable.
                if attempt == self.max_attempts:
                    raise
                delay = self._backoff(attempt)
                logger.warning("%s failed (%s); retry %d in %.1fs", url, exc, attempt, delay)
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    # raise_for_status() raises httpx.HTTPStatusError for any 4xx/5xx left over
                    # (e.g. 404), and does nothing for 2xx.
                    response.raise_for_status()
                    return response.json()
                if attempt == self.max_attempts:
                    response.raise_for_status()
                delay = min(self._retry_after(response) or self._backoff(attempt), MAX_RETRY_DELAY)
                logger.warning("%s returned %d; retry %d in %.1fs", url, response.status_code, attempt, delay)
            self._sleep(delay)
        # Unreachable: the last attempt either returns or raises. Pyright can't prove that.
        raise AssertionError("unreachable")

    def _wait_for_turn(self) -> None:
        """Sleep just long enough that requests are at least `min_interval` seconds apart."""
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.min_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now
        self.requests_made += 1

    def _backoff(self, attempt: int) -> float:
        # attempt 1 → ~1 s, 2 → ~2 s, 3 → ~4 s, plus up to 10% jitter.
        # random() is fine here: jitter needs spread, not unpredictability (hence the noqa for
        # Ruff's S311 rule, which warns about using `random` for security purposes).
        base = self.backoff_base * 2 ** (attempt - 1)
        return base + random.random() * base * 0.1  # noqa: S311

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        """Seconds from a Retry-After header, when the server sent one in that form.

        The header can also carry an HTTP date; that form is rare on APIs and is ignored here
        (the caller falls back to backoff).
        """
        value = response.headers.get("Retry-After")
        if value is not None and value.isdigit():
            return float(value)
        return None
