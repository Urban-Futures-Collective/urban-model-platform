import asyncio

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from ump.adapters.aiohttp_client_adapter import AioHttpClientAdapter
from ump.core.exceptions import OGCProcessException

"""
Tests for AioHttpClientAdapter behavior.

Each test verifies how the adapter maps various upstream responses and errors
into the domain-level OGCProcessException responses. Expected outcomes:
- Non-JSON responses should be treated as invalid upstream content and map to
    an OGCProcessException with status 502 (Bad Gateway / Invalid Response).
- HTTP error status codes from the provider (e.g. 500) are mapped to
    OGCProcessException with the same status to allow upstream error reporting.
- Network timeouts map to OGCProcessException with status 504 (Gateway Timeout).

These mappings ensure the web adapter can reliably translate provider issues
into appropriate OGC-style error responses for API clients.

The provider is a real local aiohttp server rather than a mocked session, so
the adapter is exercised against genuine aiohttp responses.
"""


async def _processes(request: web.Request) -> web.Response:
    return web.json_response({"processes": []})


async def _bad(request: web.Request) -> web.Response:
    return web.Response(text="<html>error</html>", content_type="text/html")


async def _execute_500(request: web.Request) -> web.Response:
    return web.Response(status=500, text="Server Error")


async def _execute_html(request: web.Request) -> web.Response:
    return web.Response(status=202, text="<html>ok</html>", content_type="text/html")


async def _slow(request: web.Request) -> web.Response:
    await asyncio.sleep(5)
    return web.json_response({})


@pytest_asyncio.fixture
async def provider():
    app = web.Application()
    app.router.add_get("/processes", _processes)
    app.router.add_get("/bad", _bad)
    app.router.add_get("/slow", _slow)
    app.router.add_post("/processes/echo/execution", _execute_500)
    app.router.add_post("/processes/html/execution", _execute_html)
    async with TestServer(app) as server:
        yield server


@pytest.mark.asyncio
async def test_get_json_response(provider):
    # Happy path: provider returns valid JSON. The adapter should parse the
    # JSON and return a Python dict. Expected: a dict with 'processes': [].
    async with AioHttpClientAdapter() as client:
        data = await client.get(str(provider.make_url("/processes")))
        assert isinstance(data, dict)
        assert data.get("processes") == []


@pytest.mark.asyncio
async def test_get_non_json_response_raises_ogc_exception(provider):
    # Non-JSON response: simulate the provider returning HTML/text instead of
    # valid JSON. The adapter must detect invalid JSON and raise an
    # OGCProcessException with status 502. Rationale: the provider violated the
    # expected API contract (JSON), which is treated as an upstream/bad-gateway
    # condition when surfaced to API clients.
    async with AioHttpClientAdapter() as client:
        with pytest.raises(OGCProcessException) as excinfo:
            await client.get(str(provider.make_url("/bad")))
        assert excinfo.value.response.status == 502


@pytest.mark.asyncio
async def test_post_handles_500(provider):
    # Upstream HTTP error: provider returns 500. post() does not raise for
    # status; it hands status and body back so the execution pipeline
    # (HandleProviderErrorStep) can mark the job failed and propagate it.
    url = str(provider.make_url("/processes/echo/execution"))
    async with AioHttpClientAdapter() as client:
        resp = await client.post(url, json={})
        assert resp.get("status") == 500
        assert resp.get("body") == "Server Error"


@pytest.mark.asyncio
async def test_timeout_maps_to_ogc_timeout(provider):
    # Timeout case: the provider answers slower than the request timeout. The
    # adapter should map the timeout to an OGCProcessException with status 504
    # (Gateway Timeout) indicating the upstream request timed out.
    async with AioHttpClientAdapter() as client:
        with pytest.raises(OGCProcessException) as excinfo:
            await client.get(str(provider.make_url("/slow")), timeout=0.1)
        assert excinfo.value.response.status == 504


@pytest.mark.asyncio
async def test_post_non_json_returns_text(provider):
    # When provider returns non-JSON content for a POST, adapter.post should
    # return a dict with 'body' containing the raw text.
    url = str(provider.make_url("/processes/html/execution"))
    async with AioHttpClientAdapter() as client:
        resp = await client.post(url, json={})
        assert isinstance(resp, dict)
        assert resp.get("status") == 202
        assert resp.get("body") == "<html>ok</html>"
