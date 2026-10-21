"""Entry-bound, revocable in-memory reporting sessions."""

import hmac
import logging
from datetime import datetime, timezone

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, KEYS
from .protocol import Link, Refused


def empty(state="disconnected"):
    return {
        key: {
            "state": state if key == "connection" else "not_checked",
            "source": "synthetic",
            "permission": "unknown",
            "last_check": None,
        }
        for key in KEYS
    }


class Manager:
    def __init__(self, hass):
        self.hass = hass
        self.enabled = False
        self.receiver_id = None
        self.owner = None
        self.coordinators = {}
        self.hashes = {}
        self.link = Link(self.changed)

    def coordinator(self, entry):
        coordinator = DataUpdateCoordinator(
            self.hass,
            logging.getLogger(__name__),
            name="BowLabs status",
            config_entry=entry,
            update_method=None,
        )
        coordinator.async_set_updated_data(empty())
        self.coordinators[entry.entry_id] = coordinator
        return coordinator

    def reserve(self, code):
        if not self.enabled:
            raise Refused(403)
        pending = self.link.pending
        if pending and pending.get("reserved"):
            raise Refused(409)
        # Validate/throttle using the existing protocol, but activation must wait
        # until the HA entry and sensors are loaded, followed by Mac approval.
        self.link.approve(code)
        pending = self.link.pending
        pending["approved"] = False
        pending["reserved"] = True
        return pending["hash"]

    def bind(self, entry):
        hashed = entry.data["claim_hash"]
        self.hashes[entry.entry_id] = hashed
        pending = self.link.pending
        if (
            self.enabled
            and pending
            and pending.get("reserved")
            and hmac.compare_digest(pending["hash"], hashed)
        ):
            self.owner = entry.entry_id
            pending["approved"] = True

    def changed(self, connection, report):
        coordinator = self.coordinators.get(self.owner)
        if coordinator is None:
            return
        values = empty(connection)
        if report:
            stamp = datetime.fromtimestamp(
                report["reportedAt"], timezone.utc
            ).isoformat()
            for name, row in report["modules"].items():
                values[name].update(row)
                values[name]["last_check"] = (
                    None if row["state"] == "not_checked" else stamp
                )
        coordinator.async_set_updated_data(values)

    def revoke_entry(self, entry_id):
        if self.owner == entry_id:
            self.link.revoke()
            self.owner = None
        self.hashes.pop(entry_id, None)
        if coordinator := self.coordinators.get(entry_id):
            coordinator.async_set_updated_data(empty("revoked"))

    def disable(self):
        self.enabled = False
        self.link.revoke()
        self.owner = None
        for coordinator in self.coordinators.values():
            coordinator.async_set_updated_data(empty("revoked"))


def get_manager(hass):
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = Manager(hass)
    return hass.data[DOMAIN]
