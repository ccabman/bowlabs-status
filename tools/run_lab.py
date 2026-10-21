"""Fresh HA instance; native HTTPS integration plus a loopback-only UI site."""

import asyncio
import logging
import shutil
import signal
import socket
from pathlib import Path

from aiohttp import web
from homeassistant.auth.providers.homeassistant import InvalidUsername
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from homeassistant import bootstrap, loader

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "work/installable-status.nosync"


async def main():
    for port in (18129, 18131):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    if LAB.is_symlink() or (LAB / "config").is_symlink():
        raise RuntimeError("Owned lab directories must not be symlinks")
    LAB.mkdir(mode=0o700, parents=True, exist_ok=True)
    LAB.chmod(0o700)
    config_dir = LAB / "config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    # This is an isolated installation copy; never write installed code live.
    shutil.copytree(
        ROOT / "HomeAssistant/custom_components/bowlabs_status",
        config_dir / "custom_components/bowlabs_status",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (config_dir / "custom_components/__init__.py").touch()
    (LAB / "ready").unlink(missing_ok=True)
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    try:
        from homeassistant.components import http
        from homeassistant.components.http.config import (
            HTTP_STORAGE_SCHEMA,
            async_get_and_load_store,
        )

        make = http.make_server

        def confined(instance, conf, supervisor_unix_socket_path=None):
            if (
                conf.get("server_host") != ["127.0.0.1"]
                or conf.get("server_port") != 18129
                or not conf.get("ssl_certificate")
            ):
                raise RuntimeError("Refused unexpected listener before binding")
            return make(instance, conf, supervisor_unix_socket_path)

        http.make_server = confined
        tls = ROOT / "work/status-link.nosync/tls"
        store = await async_get_and_load_store(hass)
        await store.async_set_pending(
            HTTP_STORAGE_SCHEMA(
                {
                    "server_host": ["127.0.0.1"],
                    "server_port": 18129,
                    "ssl_certificate": str(tls / "localhost.pem"),
                    "ssl_key": str(tls / "localhost.key"),
                }
            )
        )
        if store.pending is not None:
            await store.async_promote_pending()
        await store.async_mark_yaml_migration_done()
        config = {
            "homeassistant": {
                "name": "BowLabs installable integration lab",
                "latitude": 0,
                "longitude": 0,
                "elevation": 0,
                "time_zone": "America/Chicago",
                "unit_system": "metric",
            },
            "http": {},
            "api": {},
            "websocket_api": {},
        }
        assert await bootstrap.async_from_config_dict(config, hass)
        users = await hass.auth.async_get_users()
        owner = next((u for u in users if u.is_owner), None)
        if owner is None:
            owner = await hass.auth.async_create_user(
                "BowLabs test administrator",
                group_ids=["system-admin"],
                local_only=True,
            )
        viewer = next((u for u in users if u.name == "BowLabs test viewer"), None)
        if viewer is None:
            viewer = await hass.auth.async_create_user(
                "BowLabs test viewer", group_ids=["system-read-only"], local_only=True
            )
        provider = hass.auth.get_auth_provider("homeassistant", None)
        for user, username, password in [
            (owner, "bowlabs-admin", "bowlabs-isolated-admin"),
            (viewer, "bowlabs-viewer", "bowlabs-isolated-viewer"),
        ]:
            try:
                await provider.async_add_auth(username, password)
            except InvalidUsername:
                await provider.async_validate_login(username, password)
            credentials = await provider.async_get_or_create_credentials(
                {"username": username}
            )
            if not any(item.id == credentials.id for item in user.credentials):
                await hass.auth.async_link_user(user, credentials)
        assert await async_setup_component(hass, "frontend", {**config, "frontend": {}})
        if onboarding := hass.data.get("onboarding"):
            onboarding.onboarded = True
        await hass.async_start()
        assert all(
            sock.getsockname()[0] == "127.0.0.1" for sock in hass.http._server.sockets
        )
        # Test-only second site uses HA's existing runner. HTTP reports are
        # refused by the integration; only native TLS 18129 accepts reporting.
        ui = web.TCPSite(hass.http.runner, "127.0.0.1", 18131)
        await ui.start()
        for name, user in [("admin", owner), ("viewer", viewer)]:
            refresh = await hass.auth.async_create_refresh_token(
                user, client_id="http://127.0.0.1:18131/"
            )
            path = LAB / (name + "-token")
            path.touch(mode=0o600, exist_ok=True)
            path.chmod(0o600)
            path.write_text(hass.auth.async_create_access_token(refresh))
        (LAB / "ready").write_text("Native integration lab: TLS 18129, HTTP UI 18131\n")
        stopped = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
        await stopped.wait()
    finally:
        (LAB / "ready").unlink(missing_ok=True)
        for name in ["admin-token", "viewer-token"]:
            (LAB / name).unlink(missing_ok=True)
        await hass.async_stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
