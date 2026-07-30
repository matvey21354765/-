from __future__ import annotations

import json
import logging
import secrets
import time
import uuid

import aiohttp
from aiohttp import web

from avito_adspower.adspower.client import AdsPowerClient
from avito_adspower.api import AvitoParser, SearchRequest, WorkerConfig
from avito_adspower.exceptions import (
    AdsPowerProfileNotFoundError,
    AdsPowerUnavailableError,
    AvitoAuthenticationRequiredError,
    AvitoCaptchaError,
    BrowserConnectionError,
    ConfigurationError,
    ParserChangedError,
    ProfileBusyError,
    SearchDeadlineExceededError,
)

LOGGER = logging.getLogger("avito_worker")


def _error_status(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, ProfileBusyError):
        return 409, "profile_busy"
    if isinstance(exc, AvitoAuthenticationRequiredError):
        return 401, "auth_required"
    if isinstance(exc, AvitoCaptchaError):
        return 429, "captcha"
    if isinstance(exc, AdsPowerProfileNotFoundError):
        return 404, "profile_not_found"
    if isinstance(exc, AdsPowerUnavailableError):
        return 503, "adspower_unavailable"
    if isinstance(exc, BrowserConnectionError):
        return 503, "browser_error"
    if isinstance(exc, ParserChangedError):
        return 502, "parser_changed"
    if isinstance(exc, SearchDeadlineExceededError):
        return 504, "timeout"
    if isinstance(exc, ConfigurationError):
        return 500, "configuration"
    return 500, "internal_error"


@web.middleware
async def auth_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    if request.path == "/health":
        return await handler(request)
    config: WorkerConfig = request.app["config"]
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {config.worker_token}"
    if not config.worker_token or not secrets.compare_digest(supplied, expected):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "unauthorized"}),
            content_type="application/json",
        )
    peer = request.remote or "unknown"
    now = time.monotonic()
    rate_state = request.app["rate_state"]
    recent = [
        timestamp
        for timestamp in rate_state.get(peer, [])
        if now - timestamp < 60
    ]
    if len(recent) >= 30:
        raise web.HTTPTooManyRequests(
            text=json.dumps({"error": "rate_limited"}),
            content_type="application/json",
        )
    recent.append(now)
    rate_state[peer] = recent
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    config: WorkerConfig = request.app["config"]
    payload = {
        "status": "degraded",
        "adspower_reachable": False,
        "profile_configured": bool(config.adspower_profile_id),
        "profile_running": False,
        "browser_connected": False,
    }
    try:
        async with aiohttp.ClientSession() as session:
            state = await AdsPowerClient(config, session).status()
        payload.update({
            "status": "ok" if state.active and state.cdp_endpoint else "degraded",
            "adspower_reachable": True,
            "profile_running": state.active,
            "browser_connected": bool(state.cdp_endpoint),
        })
    except Exception:
        pass
    return web.json_response(payload)


async def search(request: web.Request) -> web.Response:
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    try:
        body = await request.json()
        query = SearchRequest.from_dict(body)
        result = await AvitoParser(request.app["config"]).search(query)
        return web.json_response({
            "request_id": request_id,
            "search_id": str(uuid.uuid4()),
            **result.to_dict(),
        })
    except Exception as exc:
        status, error_type = _error_status(exc)
        LOGGER.warning(
            "search_failed request_id=%s error_type=%s",
            request_id,
            error_type,
        )
        return web.json_response(
            {
                "request_id": request_id,
                "error_type": error_type,
                "error_message_safe": type(exc).__name__,
            },
            status=status,
        )


async def search_stream(request: web.Request) -> web.StreamResponse:
    try:
        query = SearchRequest.from_dict(await request.json())
    except Exception:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid_request"}),
            content_type="application/json",
        )
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "application/x-ndjson"},
    )
    await response.prepare(request)
    try:
        async for item in AvitoParser(request.app["config"]).stream(query):
            await response.write(
                (json.dumps(item.to_dict(), ensure_ascii=False) + "\n").encode()
            )
    except Exception as exc:
        _, error_type = _error_status(exc)
        await response.write(
            (json.dumps({"error_type": error_type}) + "\n").encode()
        )
    await response.write_eof()
    return response


def create_app(config: WorkerConfig | None = None) -> web.Application:
    app = web.Application(
        middlewares=[auth_middleware],
        client_max_size=256 * 1024,
    )
    app["config"] = config or WorkerConfig.from_env()
    app["rate_state"] = {}
    app.router.add_get("/health", health)
    app.router.add_post("/v1/avito/search", search)
    app.router.add_post("/v1/avito/search/stream", search_stream)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = WorkerConfig.from_env()
    web.run_app(
        create_app(config),
        host=config.worker_host,
        port=config.worker_port,
    )


if __name__ == "__main__":
    main()
