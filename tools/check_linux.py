"""Real Home Assistant lifecycle/HTTPS smoke test in a disposable Linux sandbox."""

import asyncio
import json
import logging
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import aiohttp
from homeassistant.components.http.config import (
    HTTP_STORAGE_SCHEMA,
    async_get_and_load_store,
)
from homeassistant.core import HomeAssistant

from homeassistant import bootstrap, loader


async def main():
    with tempfile.TemporaryDirectory(prefix="bowlabs-ci-") as directory:
        lab = Path(directory)
        config_dir = lab / "config"
        config_dir.mkdir()
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "custom_components",
            config_dir / "custom_components",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (config_dir / "custom_components/__init__.py").touch()
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(lab / "key.pem"),
                "-out",
                str(lab / "cert.pem"),
            ],
            check=True,
            capture_output=True,
        )
        hass = HomeAssistant(str(config_dir))
        loader.async_setup(hass)
        try:
            store = await async_get_and_load_store(hass)
            await store.async_set_pending(
                HTTP_STORAGE_SCHEMA(
                    {
                        "server_host": ["127.0.0.1"],
                        "server_port": 18139,
                        "ssl_certificate": str(lab / "cert.pem"),
                        "ssl_key": str(lab / "key.pem"),
                    }
                )
            )
            await store.async_promote_pending()
            await store.async_mark_yaml_migration_done()
            assert await bootstrap.async_from_config_dict(
                {
                    "homeassistant": {
                        "name": "Disposable BowLabs CI",
                        "latitude": 0,
                        "longitude": 0,
                        "elevation": 0,
                        "time_zone": "UTC",
                        "unit_system": "metric",
                    },
                    "http": {},
                    "api": {},
                    "websocket_api": {},
                },
                hass,
            )
            await hass.async_start()
            assert all(
                sock.getsockname()[0] == "127.0.0.1"
                for sock in hass.http._server.sockets
            )
            admin = await hass.auth.async_create_user(
                "CI admin", group_ids=["system-admin"], local_only=True
            )
            viewer = await hass.auth.async_create_user(
                "CI viewer", group_ids=["system-read-only"], local_only=True
            )

            async def token(user):
                refresh = await hass.auth.async_create_refresh_token(
                    user, client_id="https://127.0.0.1:18139/"
                )
                return hass.auth.async_create_access_token(refresh)

            admin_token, viewer_token = await token(admin), await token(viewer)
            context = ssl.create_default_context(cafile=str(lab / "cert.pem"))
            async with aiohttp.ClientSession(
                trust_env=False, connector=aiohttp.TCPConnector(ssl=context)
            ) as session:

                async def api(
                    path, body=None, method="POST", credential=admin_token, expected=200
                ):
                    async with session.request(
                        method,
                        "https://127.0.0.1:18139" + path,
                        json=body,
                        headers={"Authorization": "Bearer " + credential},
                    ) as response:
                        assert response.status == expected, (
                            path,
                            response.status,
                            await response.text(),
                        )
                        return (
                            await response.json()
                            if response.content_type == "application/json"
                            else None
                        )

                async def post(
                    action, body=None, secret=None, expected=200, extra=None
                ):
                    headers = {"X-BowLabs-Status-Key": secret} if secret else {}
                    headers.update(extra or {})
                    async with session.post(
                        "https://127.0.0.1:18139/api/bowlabs_status/" + action,
                        json={} if body is None else body,
                        headers=headers,
                    ) as response:
                        assert response.status == expected, (
                            action,
                            response.status,
                            expected,
                        )
                        return await response.json()

                async def create(code=None):
                    value = await api(
                        "/api/config/config_entries/flow", {"handler": "bowlabs_status"}
                    )
                    value = await api(
                        "/api/config/config_entries/flow/" + value["flow_id"],
                        {} if code is None else {"code": code},
                    )
                    assert value["type"] == "create_entry", value
                    entry = value["result"]["entry_id"]
                    for _ in range(100):
                        if (
                            hass.config_entries.async_get_entry(entry).state.value
                            == "loaded"
                        ):
                            return entry
                        await asyncio.sleep(0.1)
                    raise RuntimeError("Entry not loaded")

                async def options(entry, action, code=None):
                    value = await api(
                        "/api/config/config_entries/options/flow", {"handler": entry}
                    )
                    value = await api(
                        "/api/config/config_entries/options/flow/" + value["flow_id"],
                        {"action": action},
                    )
                    if code is not None:
                        value = await api(
                            "/api/config/config_entries/options/flow/"
                            + value["flow_id"],
                            {"code": code},
                        )
                    assert value["type"] == "create_entry", value

                def sensors():
                    return [
                        row
                        for row in hass.states.async_all("sensor")
                        if "BowLabs Mac status" in row.name
                    ]

                policy = {
                    "protocol": 1,
                    "role": "mac_status_lab",
                    "capabilities": ["status.report.synthetic"],
                }
                await api(
                    "/api/config/config_entries/flow",
                    {"handler": "bowlabs_status"},
                    credential=viewer_token,
                    expected=401,
                )
                receiver = await create()
                pair = await post("start", policy)
                await post("confirm", secret=pair["secret"], expected=403)
                agent = await create(pair["code"])
                await post("report", {}, pair["secret"], expected=401)
                await post("confirm", secret=pair["secret"])
                report = {
                    "protocol": 1,
                    "source": "synthetic",
                    "sequence": 1,
                    "reportedAt": int(time.time()),
                    "modules": {
                        "photos": {"state": "checked", "permission": "full"},
                        "calendar": {"state": "checked", "permission": "full"},
                        "music": {"state": "permission_only", "permission": "granted"},
                        "album": {
                            "state": "sample_loaded",
                            "permission": "full",
                            "loaded": 3,
                            "needs_download": 0,
                            "failed": 0,
                            "timed_out": 0,
                        },
                    },
                }
                await post("report", report, pair["secret"])
                assert len(sensors()) == 5
                assert any(row.state == "sample_loaded" for row in sensors())
                await post("report", report, pair["secret"], expected=409)
                await post(
                    "report",
                    report,
                    pair["secret"],
                    expected=403,
                    extra={"Origin": "https://example.com"},
                )
                await api(
                    "/api/config/config_entries/entry/" + agent,
                    method="DELETE",
                    credential=viewer_token,
                    expected=401,
                )
                await options(agent, "revoke")
                await post("report", report, pair["secret"], expected=401)
                assert all(
                    row.attributes.get("last_check") is None for row in sensors()
                )
                new = await post("start", policy)
                await options(agent, "pair", new["code"])
                await post("confirm", secret=new["secret"])
                await post("report", report, new["secret"])
                await hass.config_entries.async_reload(agent)
                await post("report", report, new["secret"], expected=401)
                assert all(
                    row.attributes.get("last_check") is None for row in sensors()
                )
                await api("/api/config/config_entries/entry/" + agent, method="DELETE")
                assert not sensors()
                await api(
                    "/api/config/config_entries/entry/" + receiver, method="DELETE"
                )
                await post("start", policy, expected=410)
            print(
                json.dumps(
                    {
                        "linux_native_ha_lifecycle": "passed",
                        "tls_verified": True,
                        "pairing_both_approvals": True,
                        "five_sensors": True,
                        "replay_and_origin_refused": True,
                        "viewer_refused": True,
                        "revoke_repair_reload_remove": True,
                        "sandbox": "temporary loopback only",
                    }
                )
            )
        finally:
            await hass.async_stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
