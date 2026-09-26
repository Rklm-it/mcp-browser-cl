import httpx
import pytest

from mcp_browser import server


@pytest.fixture
def client(settings):
    app = server.build_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")


async def _get(client, path, **kw):
    async with client:
        return await client.get(path, **kw)


def test_paths(client):
    import asyncio
    assert asyncio.run(_get(client, "/healthz")).json()["service"] == "mcp-browser"


@pytest.mark.parametrize("path,code", [("/", 404), ("/mcp", 401), ("/mcp/wrong", 404), ("/browser/mcp/wrong", 404),
                                       ("/browser/healthz", 200), ("/secret", 404)])
def test_denied(client, path, code):
    import asyncio
    assert asyncio.run(_get(client, path)).status_code == code


def test_short_secret_refuses_to_start(settings):
    settings.secret = "short"
    with pytest.raises(SystemExit):
        server.build_app()
