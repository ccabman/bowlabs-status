"""Exercise the installed integration through real HA admin/viewer APIs."""

import asyncio
import json
import ssl
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "work/installable-status.nosync"
UI = "http://127.0.0.1:18131"
TLS = "https://127.0.0.1:18129/api/bowlabs_status/"
POLICY = {
    "protocol": 1,
    "role": "mac_status_lab",
    "capabilities": ["status.report.synthetic"],
}


async def main():
    context = ssl.create_default_context(
        cafile=str(ROOT / "work/status-link.nosync/tls/root.pem")
    )
    admin = (LAB / "admin-token").read_text()
    viewer = (LAB / "viewer-token").read_text()
    results = {}
    async with aiohttp.ClientSession(
        trust_env=False,
        connector=aiohttp.TCPConnector(force_close=True),
        timeout=aiohttp.ClientTimeout(total=10),
    ) as session:

        async def api(path, body=None, method="POST", token=admin, status=200):
            async with session.request(
                method,
                UI + path,
                json=body,
                headers={"Authorization": "Bearer " + token},
            ) as response:
                text = await response.text()
                assert response.status == status, (path, response.status, text[:200])
                return (
                    json.loads(text)
                    if text and response.content_type == "application/json"
                    else None
                )

        async def post(action, body=None, secret=None, status=200):
            headers = {"X-BowLabs-Status-Key": secret} if secret else {}
            async with session.post(
                TLS + action,
                json={} if body is None else body,
                headers=headers,
                ssl=context,
            ) as response:
                assert response.status == status, (action, response.status, status)
                return await response.json()

        async def flow():
            return await api(
                "/api/config/config_entries/flow",
                {"handler": "bowlabs_status", "show_advanced_options": False},
            )

        async def submit(value, body):
            return await api(
                "/api/config/config_entries/flow/" + value["flow_id"], body
            )

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
                    "/api/config/config_entries/options/flow/" + value["flow_id"],
                    {"code": code},
                )
            return value

        async def wait_loaded(entry):
            for _ in range(100):
                entries = await api("/api/config/config_entries/entry", method="GET")
                row = next((x for x in entries if x["entry_id"] == entry), None)
                if row and row["state"] == "loaded":
                    return
                await asyncio.sleep(0.1)
            raise RuntimeError("Entry did not load")

        async def states():
            return [
                x
                for x in await api("/api/states", method="GET")
                if "BowLabs Mac status" in x["attributes"].get("friendly_name", "")
            ]

        async def disable(entry, value):
            async with session.ws_connect(UI + "/api/websocket") as ws:
                await ws.receive_json()
                await ws.send_json({"type": "auth", "access_token": admin})
                assert (await ws.receive_json())["type"] == "auth_ok"
                await ws.send_json(
                    {
                        "id": 1,
                        "type": "config_entries/disable",
                        "entry_id": entry,
                        "disabled_by": value,
                    }
                )
                assert (await ws.receive_json())["success"]

        await api(
            "/api/config/config_entries/flow",
            {"handler": "bowlabs_status"},
            token=viewer,
            status=401,
        )
        value = await flow()
        assert value["step_id"] == "enable"
        created = await submit(value, {})
        receiver = created["result"]["entry_id"]
        await wait_loaded(receiver)
        results["receiver_installed_via_normal_flow"] = True
        run = LAB / "probe"
        run.mkdir(mode=0o700, exist_ok=True)
        for name in ["pair.json", "sent", "revoked", "native-results.json"]:
            (run / name).unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            sys.argv[1],
            str(run),
            str(ROOT / "work/status-link.nosync/tls/root.der"),
            str(ROOT / "work/mac-lab.nosync/localhost.der"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:

            async def wait_file(name):
                for _ in range(600):
                    if (run / name).exists():
                        return
                    if proc.returncode is not None:
                        raise RuntimeError("Native probe exited early")
                    await asyncio.sleep(0.1)
                raise RuntimeError("Native probe timed out")

            await wait_file("pair.json")
            pair = json.loads((run / "pair.json").read_text())
            value = await flow()
            assert value["step_id"] == "pair"
            wrong = await submit(value, {"code": "wrong"})
            assert wrong["errors"]["base"] == "pairing_refused"
            created = await submit(value, {"code": pair["code"]})
            agent = created["result"]["entry_id"]
            await wait_loaded(agent)
            await wait_file("sent")
            rows = await states()
            assert len(rows) == 5, rows
            assert any(
                x["state"] == "partial" and x["attributes"].get("loaded") == 1
                for x in rows
            )
            results["five_real_sensors_from_native_https_report"] = True
            await api(
                "/api/config/config_entries/entry/" + agent,
                method="DELETE",
                token=viewer,
                status=401,
            )
            value = await options(agent, "revoke")
            assert value["type"] == "create_entry"
            await post("report", {}, pair["secret"], 401)
            assert all(
                x["attributes"].get("last_check") is None for x in await states()
            )
            (run / "revoked").write_text("revoked")
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
            (LAB / "probe.log").write_bytes(stdout + stderr)
            assert proc.returncode == 0
            results["native_probe"] = json.loads(
                (run / "native-results.json").read_text()
            )
            results["viewer_cannot_install_or_remove"] = True
            results["options_revocation_clears_and_refuses"] = True
            # Re-pair the SAME entry with a new secret through normal options.
            started = await post("start", POLICY)
            value = await options(agent, "pair", started["code"])
            assert value["type"] == "create_entry"
            await post("confirm", {}, started["secret"])
            await post("status", {}, pair["secret"], 410)
            results["options_repair_reuses_entry"] = True
            await disable(agent, "user")
            await post("report", {}, started["secret"], 401)
            await disable(agent, None)
            await wait_loaded(agent)
            await post("report", {}, started["secret"], 401)
            results["disable_and_reload_do_not_restore_credential"] = True
            started = await post("start", POLICY)
            await options(agent, "pair", started["code"])
            await post("confirm", {}, started["secret"])
            await disable(receiver, "user")
            await post("report", {}, started["secret"], 410)
            await disable(receiver, None)
            await wait_loaded(receiver)
            await post("report", {}, started["secret"], 401)
            results["receiver_disable_revokes_active_session"] = True
            started = await post("start", POLICY)
            await options(agent, "pair", started["code"])
            await post("confirm", {}, started["secret"])
            # Entry removal while an authenticated upload is paused.
            uploaded = asyncio.Event()
            release = asyncio.Event()

            async def body():
                yield b" "
                uploaded.set()
                await release.wait()
                yield b"{}"

            async def pending_report():
                async with session.post(
                    TLS + "report",
                    data=body(),
                    headers={
                        "Content-Type": "application/json",
                        "X-BowLabs-Status-Key": started["secret"],
                    },
                    ssl=context,
                ) as response:
                    assert response.status == 401, response.status

            pending = asyncio.create_task(pending_report())
            await uploaded.wait()
            await api("/api/config/config_entries/entry/" + agent, method="DELETE")
            release.set()
            await pending
            await post("report", {}, started["secret"], 401)
            assert len(await states()) == 0
            results["removal_during_upload_revokes_and_removes_sensors"] = True
            started = await post("start", POLICY)
            value = await flow()
            created = await submit(value, {"code": started["code"]})
            agent = created["result"]["entry_id"]
            await wait_loaded(agent)
            await post("confirm", {}, started["secret"])
            await api("/api/config/config_entries/entry/" + receiver, method="DELETE")
            await post("start", POLICY, status=410)
            await post("report", {}, started["secret"], 410)
            assert all(
                x["attributes"].get("last_check") is None for x in await states()
            )
            await api("/api/config/config_entries/entry/" + agent, method="DELETE")
            results["receiver_removal_disables_endpoint"] = True
            assert not [
                e
                for e in await api("/api/config/config_entries/entry", method="GET")
                if e["domain"] == "bowlabs_status"
            ]
            results["clean_uninstall"] = True
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            (run / "pair.json").unlink(missing_ok=True)
    (LAB / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
