"""Two-process restart proof using normal HA APIs, never internal storage files."""

import asyncio
import json
import ssl
import sys
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "work/installable-status.nosync"
POLICY = {
    "protocol": 1,
    "role": "mac_status_lab",
    "capabilities": ["status.report.synthetic"],
}


async def main():
    context = ssl.create_default_context(
        cafile=str(ROOT / "work/status-link.nosync/tls/root.pem")
    )
    async with aiohttp.ClientSession(trust_env=False) as session:
        headers = {"Authorization": "Bearer " + (LAB / "admin-token").read_text()}

        async def api(path, data=None, method="POST"):
            async with session.request(
                method, "http://127.0.0.1:18131" + path, json=data, headers=headers
            ) as r:
                assert r.status == 200, (path, r.status)
                return await r.json()

        async def post(action, data, key=None, status=200):
            async with session.post(
                "https://127.0.0.1:18129/api/bowlabs_status/" + action,
                json=data,
                headers={"X-BowLabs-Status-Key": key} if key else {},
                ssl=context,
            ) as r:
                assert r.status == status, (action, r.status)
                return await r.json()

        if sys.argv[1] == "prepare":
            flow = await api(
                "/api/config/config_entries/flow", {"handler": "bowlabs_status"}
            )
            receiver = await api(
                "/api/config/config_entries/flow/" + flow["flow_id"], {}
            )
            await asyncio.sleep(0.3)
            started = await post("start", POLICY)
            flow = await api(
                "/api/config/config_entries/flow", {"handler": "bowlabs_status"}
            )
            agent = await api(
                "/api/config/config_entries/flow/" + flow["flow_id"],
                {"code": started["code"]},
            )
            await asyncio.sleep(0.3)
            await post("confirm", {}, started["secret"])
            path = LAB / "restart-private.json"
            path.touch(mode=0o600, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "secret": started["secret"],
                        "agent": agent["result"]["entry_id"],
                        "receiver": receiver["result"]["entry_id"],
                    }
                )
            )
            print("Prepared active session for process restart")
        else:
            saved = json.loads((LAB / "restart-private.json").read_text())
            await post("report", {}, saved["secret"], 401)
            rows = await api("/api/states", method="GET")
            states = [
                x
                for x in rows
                if "BowLabs Mac status" in x["attributes"].get("friendly_name", "")
            ]
            assert len(states) == 5 and all(
                x["state"] == "not_checked"
                for x in states
                if not x["entity_id"].endswith("_connection")
            )
            for entry in [saved["agent"], saved["receiver"]]:
                await api("/api/config/config_entries/entry/" + entry, method="DELETE")
            (LAB / "restart-private.json").unlink()
            (LAB / "restart-results.json").write_text(
                json.dumps(
                    {
                        "entry_restored": True,
                        "five_sensors_restored_without_trusted_results": True,
                        "old_credential_refused_after_actual_process_restart": True,
                        "clean_removal_after_restart": True,
                    },
                    indent=2,
                )
                + "\n"
            )
            print("PASS: restart restored entries and sensors; old credentials refused")


if __name__ == "__main__":
    asyncio.run(main())
