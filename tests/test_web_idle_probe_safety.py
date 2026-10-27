import httpx
import pytest

from agentdeck import web_idle


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="runtime is unhealthy"),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"active": "unknown"}),
        httpx.Response(
            200,
            json={
                "active": False,
                "live_worker_processes": "unknown",
                "unfinished_jobs": 0,
            },
        ),
    ],
)
async def test_unknown_runtime_activity_keeps_the_web_worker_alive(response):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response),
        base_url="http://runtime",
    ) as client:
        assert await web_idle.runtime_is_busy(client) is True


async def test_runtime_read_failure_keeps_the_web_worker_alive():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("runtime stopped answering", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail),
        base_url="http://runtime",
    ) as client:
        assert await web_idle.runtime_is_busy(client) is True
