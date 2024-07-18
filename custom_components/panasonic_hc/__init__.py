"""The Panasonic H&C integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_THERMOSTAT_CONNECTED, SIGNAL_THERMOSTAT_DISCONNECTED
from .panasonic_hc import PanasonicHC, PanasonicHCException

PLATFORMS: list[Platform] = [Platform.CLIMATE]

type PanasonicHCConfigEntry = ConfigEntry[PanasonicHC]  # noqa: F821

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Panasonic H&C from a config entry."""

    mac_address: str | None = entry.unique_id

    device = bluetooth.async_ble_device_from_address(
        hass, mac_address.upper(), connectable=True
    )

    if device is None:
        raise ConfigEntryNotReady(f"[{mac_address}] Device could not be found")

    thermostat = PanasonicHC(ble_device=device, mac_address=mac_address)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = thermostat

    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_create_background_task(
        hass, _async_run_thermostat(hass, entry), entry.entry_id
    )

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        thermostat: PanasonicHC = hass.data[DOMAIN].pop(entry.entry_id)
        await thermostat.async_disconnect()

    return unload_ok

async def _async_run_thermostat(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run the thermostat."""

    thermostat = hass.data[DOMAIN][entry.entry_id]

    await _async_reconnect_thermostat(hass, entry)

    while True:
        try:
            await thermostat.async_get_status()
        except PanasonicHCException as e:
            if not thermostat.is_connected:
                _LOGGER.error("[%s] PanasonicHC device disconnected", thermostat.mac_address)

                async_dispatcher_send(hass, f"{SIGNAL_THERMOSTAT_DISCONNECTED}_{thermostat.mac_address}")
                await _async_reconnect_thermostat(hass, entry)
                continue

            _LOGGER.error("[%s] Error updating PanasonicHC device %s", thermostat.mac_address, e)

        await asyncio.sleep(10)


async def _async_reconnect_thermostat(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reconnect thermostat."""

    thermostat = hass.data[DOMAIN][entry.entry_id]

    while True:
        try:
            await thermostat.async_connect()
        except PanasonicHCException:
            await asyncio.sleep(10)
            continue

        _LOGGER.debug("[%s] PanasonicHC device connected", thermostat.mac_address)

        async_dispatcher_send(hass, f"{SIGNAL_THERMOSTAT_CONNECTED}_{thermostat.mac_address}")

        return
