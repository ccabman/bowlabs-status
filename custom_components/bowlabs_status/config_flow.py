"""Normal HA administrator setup, pairing, re-pairing and revocation."""

import re

import probatio
from homeassistant.core import callback

from homeassistant import config_entries

from .const import DOMAIN
from .manager import get_manager
from .protocol import Refused


def reserve(hass, code):
    if type(code) is not str or re.fullmatch(r"[0-9]{8}", code) is None:
        raise Refused()
    return get_manager(hass).reserve(code)


class BowLabsFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        entries = self._async_current_entries()
        if not any(entry.data.get("kind") == "receiver" for entry in entries):
            if user_input is not None:
                await self.async_set_unique_id("receiver")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="BowLabs Status receiver", data={"kind": "receiver"}
                )
            return self.async_show_form(
                step_id="enable", data_schema=probatio.Schema({})
            )
        if any(entry.data.get("kind") == "agent" for entry in entries):
            return self.async_abort(reason="already_paired")
        return await self.async_step_pair(user_input)

    async def async_step_enable(self, user_input=None):
        return await self.async_step_user(user_input)

    async def async_step_pair(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                claim_hash = reserve(self.hass, user_input.get("code"))
                await self.async_set_unique_id("mac_status_agent")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="BowLabs Mac status",
                    data={"kind": "agent", "claim_hash": claim_hash},
                )
            except Refused:
                errors["base"] = "pairing_refused"
        return self.async_show_form(
            step_id="pair",
            data_schema=probatio.Schema({probatio.Required("code"): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BowLabsOptions()


class BowLabsOptions(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if self.config_entry.data.get("kind") != "agent":
            return self.async_abort(reason="receiver_options")
        if user_input is not None:
            if user_input["action"] == "revoke":
                get_manager(self.hass).revoke_entry(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required("action", default="revoke"): probatio.In(
                        ["revoke", "pair"]
                    )
                }
            ),
        )

    async def async_step_pair(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                claim_hash = reserve(self.hass, user_input.get("code"))
                manager = get_manager(self.hass)
                pending = manager.link.pending
                manager.revoke_entry(self.config_entry.entry_id)
                manager.link.pending = pending
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data={"kind": "agent", "claim_hash": claim_hash}
                )
                await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                return self.async_create_entry(title="", data={})
            except Refused:
                errors["base"] = "pairing_refused"
        return self.async_show_form(
            step_id="pair",
            data_schema=probatio.Schema({probatio.Required("code"): str}),
            errors=errors,
        )
