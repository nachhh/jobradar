"""Unit tests for the retrying HTTP fetcher. No network: respx intercepts httpx requests.

`respx_mock` is a pytest fixture from respx's plugin: any httpx request made during the test is
matched against the routes declared on it, and a request matching no route fails the test, so
nothing can reach the real internet by accident.
"""

import httpx
import pytest
import respx

from jobradar.ingest.http import MAX_RETRY_DELAY, USER_AGENT, Fetcher, make_client

URL = "https://api.example.com/jobs"


class FakeTime:
    """A clock that only moves when the code under test sleeps: tests run instantly, and the
    recorded sleeps show exactly how long the fetcher would have waited."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def fetcher(fake_time: FakeTime) -> Fetcher:
    return Fetcher(make_client(), min_interval=0.0, sleep=fake_time.sleep, clock=fake_time.clock)


def test_returns_json_and_sends_user_agent(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert fetcher.get_json(URL) == {"ok": True}
    assert route.calls.last.request.headers["User-Agent"] == USER_AGENT


def test_list_params_repeat_the_key(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json={}))
    fetcher.get_json(URL, params={"page": 2, "expand[]": ["company", "tags"]})
    query = route.calls.last.request.url.query.decode()
    assert query == "page=2&expand%5B%5D=company&expand%5B%5D=tags"  # %5B%5D is "[]" URL-encoded


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_transient_errors_with_backoff(
    respx_mock: respx.MockRouter, fetcher: Fetcher, fake_time: FakeTime, status: int
) -> None:
    # side_effect with a list: each call gets the next response in order.
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(status), httpx.Response(200, json=[1])]
    )
    assert fetcher.get_json(URL) == [1]
    # Two waits, ~1 s then ~2 s (each plus up to 10% jitter).
    assert len(fake_time.sleeps) == 2
    assert 1.0 <= fake_time.sleeps[0] <= 1.1
    assert 2.0 <= fake_time.sleeps[1] <= 2.2


def test_retries_network_errors(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    respx_mock.get(URL).mock(side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json={})])
    assert fetcher.get_json(URL) == {}
    assert fetcher.requests_made == 2


def test_honours_retry_after_but_caps_it(
    respx_mock: respx.MockRouter, fetcher: Fetcher, fake_time: FakeTime
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json={}),
        ]
    )
    fetcher.get_json(URL)
    assert fake_time.sleeps == [7.0, MAX_RETRY_DELAY]


def test_gives_up_after_max_attempts(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.get_json(URL)
    assert route.call_count == fetcher.max_attempts


def test_client_errors_are_not_retried(respx_mock: respx.MockRouter, fetcher: Fetcher) -> None:
    route = respx_mock.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.get_json(URL)
    assert route.call_count == 1


def test_waits_min_interval_between_requests(respx_mock: respx.MockRouter, fake_time: FakeTime) -> None:
    fetcher = Fetcher(make_client(), min_interval=1.5, sleep=fake_time.sleep, clock=fake_time.clock)
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json={}))

    fetcher.get_json(URL)  # first request: no wait
    fake_time.now += 0.5  # half a second of "work" before the next request
    fetcher.get_json(URL)

    assert fake_time.sleeps == [pytest.approx(1.0)]  # 1.5 s interval minus the 0.5 s already passed
