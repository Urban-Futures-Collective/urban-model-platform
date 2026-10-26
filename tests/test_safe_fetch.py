"""AioHttpSafeFetch (SafeFetchPort adapter) against real local servers.

The local server listens on 127.0.0.1, i.e. exactly the kind of address the
adapter must refuse unless allowlisted. ``localhost`` goes through DNS
resolution, so it exercises the connection-time check (what defeats DNS
rebinding); ``127.0.0.1`` exercises the literal-address check.
"""

import asyncio

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from ump.adapters.safe_fetch_aiohttp import AioHttpSafeFetch
from ump.core.interfaces.safe_fetch import RemoteFetchError, UnsafeUrlError


async def _json(request):
    return web.json_response({"ok": True, "auth": request.headers.get("Authorization")})


async def _redirect(request):
    raise web.HTTPFound("http://169.254.169.254/latest/meta-data")


async def _big(request):
    return web.Response(
        body=b"[" + b"1," * 10_000 + b"1]", content_type="application/json"
    )


async def _html(request):
    return web.Response(text="<html/>", content_type="text/html")


async def _slow(request):
    await asyncio.sleep(2)
    return web.json_response({})


async def _missing(request):
    return web.Response(status=404)


@pytest_asyncio.fixture
async def server():
    app = web.Application()
    for path, handler in {
        "/json": _json,
        "/redirect": _redirect,
        "/big": _big,
        "/html": _html,
        "/slow": _slow,
        "/missing": _missing,
    }.items():
        app.router.add_get(path, handler)
    async with TestServer(app, host="127.0.0.1") as srv:
        yield srv


def _url(server, path, host="localhost"):
    return f"http://{host}:{server.port}{path}"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",  # http not allowed by default
        "ftp://example.org/",
        "https://user:pw@example.org/",
        "https:///nohost",
        "https://127.0.0.1/",
        "https://10.1.2.3/",
        "https://192.168.0.1/",
        "https://169.254.169.254/",  # cloud metadata
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://0.0.0.0/",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(UnsafeUrlError):
        AioHttpSafeFetch().check_url(url)


def test_check_url_accepts_public_and_allowlisted():
    AioHttpSafeFetch().check_url("https://models.example.org/api/")
    AioHttpSafeFetch().check_url("https://8.8.8.8/")
    AioHttpSafeFetch(allow_http=True).check_url("http://example.org/")
    fetch = AioHttpSafeFetch(allowed_private_hosts=["modelserver", "10.1.2.3"])
    fetch.check_url("https://modelserver:5000/")
    fetch.check_url("https://10.1.2.3/")


@pytest.mark.asyncio
async def test_hostname_resolving_to_loopback_is_blocked_at_connect(server):
    fetch = AioHttpSafeFetch(allow_http=True)
    with pytest.raises(UnsafeUrlError):
        await fetch.get_json(_url(server, "/json"))
    with pytest.raises(UnsafeUrlError):
        await fetch.get_json(_url(server, "/json", host="127.0.0.1"))


@pytest.mark.asyncio
async def test_allowlisted_host_is_fetched_with_headers(server):
    fetch = AioHttpSafeFetch(allow_http=True, allowed_private_hosts=["localhost"])
    body = await fetch.get_json(
        _url(server, "/json"), headers={"Authorization": "Basic x"}
    )
    assert body == {"ok": True, "auth": "Basic x"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/redirect", "/big", "/html", "/missing", "/slow"])
async def test_remote_errors(server, path):
    fetch = AioHttpSafeFetch(
        allow_http=True,
        allowed_private_hosts=["localhost"],
        timeout=0.5,
        max_bytes=1_000,
    )
    with pytest.raises(RemoteFetchError):
        await fetch.get_json(_url(server, path))


@pytest.mark.asyncio
async def test_unreachable_host_is_a_remote_error():
    fetch = AioHttpSafeFetch(allow_http=True, allowed_private_hosts=["localhost"])
    with pytest.raises(RemoteFetchError):
        await fetch.get_json("http://localhost:9/")  # discard port, nothing listens
