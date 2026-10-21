"""Native HA HTTPS views: no HA token is handed to the Mac."""

import asyncio

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .manager import get_manager
from .protocol import Refused, decode


class StatusView(HomeAssistantView):
    url = "/api/bowlabs_status/{action}"
    name = "api:bowlabs_status"
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    async def post(self, request, action):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            if action not in {"start", "status", "confirm", "report", "cancel"}:
                raise Refused(404)
            if not request.secure or "Origin" in request.headers:
                raise Refused(403)
            manager = get_manager(self.hass)
            if not manager.enabled:
                raise Refused(410)
            if (
                request.content_type != "application/json"
                or "Content-Encoding" in request.headers
            ):
                raise Refused(415)
            if request.content_length is not None and request.content_length > 4096:
                raise Refused(413)
            secret = request.headers.get("X-BowLabs-Status-Key", "")
            if action != "start" and (
                len(secret) != 48
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in secret)
            ):
                raise Refused(401)
            if action == "report":
                manager.link.authorized(secret)
            raw = bytearray()
            async with asyncio.timeout(2):
                async for chunk in request.content.iter_chunked(1024):
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise Refused(413)
            value = decode(raw)
            # Config entry removal may run while the request body is arriving.
            if not manager.enabled:
                raise Refused(410)
            if action not in {"start", "report"} and value != {}:
                raise Refused()
            if action == "start":
                result = manager.link.start(value)
            elif action == "status":
                result = manager.link.status(secret)
            elif action == "confirm":
                result = manager.link.confirm(secret)
            elif action == "report":
                result = manager.link.report(secret, value)
            else:
                result = manager.link.cancel(secret)
            return web.json_response(result, headers=headers)
        except Refused as error:
            result = web.json_response(
                {"error": "refused"}, status=error.status, headers=headers
            )
        except TimeoutError:
            result = web.json_response(
                {"error": "timeout"}, status=408, headers=headers
            )
        result.force_close()
        return result
