"""Platform for Panasonic H&C climate entities."""

import logging
from typing import Any

from homeassistant.components.climate import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SIGNAL_THERMOSTAT_CONNECTED,
    SIGNAL_THERMOSTAT_DISCONNECTED,
)
from .panasonic_hc import MAX_TEMP, MIN_TEMP, PanasonicHC, PanasonicHCException

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Handle config entry setup."""

    thermostat: PanasonicHC = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [PanasonicHCClimate(thermostat)],
    )


class PanasonicHCClimate(ClimateEntity):
    """Climate entity to represent a eQ-3 thermostat."""

    _attr_name = None
    _attr_has_entity_name = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_precision = PRECISION_HALVES
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]
    _attr_fan_modes = [FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH]
    _attr_preset_modes = [PRESET_ECO, PRESET_NONE]
    _attr_should_poll = False
    _attr_available = False
    _attr_fan_mode = None
    _attr_hvac_mode: HVACMode | None = None
    _attr_hvac_action: HVACAction | None = None
    _attr_preset_mode: str | None = None
    _target_temperature: float | None = None

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the climate entity."""

        self._thermostat = thermostat
        self._attr_unique_id = dr.format_mac(thermostat.mac_address)
        self._attr_device_info = DeviceInfo(
            name=f"{MODEL}_{thermostat.mac_address[-8:].replace(':','')}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            connections={(CONNECTION_BLUETOOTH, thermostat.mac_address)},
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""

        self._thermostat.register_update_callback(self._async_on_updated)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_DISCONNECTED}_{self._thermostat.mac_address}",
                self._async_on_disconnected,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_CONNECTED}_{self._thermostat.mac_address}",
                self._async_on_connected,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""

        self._thermostat.unregister_update_callback(self._async_on_updated)

    @callback
    def _async_on_disconnected(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _async_on_connected(self) -> None:
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _async_on_updated(self) -> None:
        """Handle updated data from the thermostat."""

        if self._thermostat.status is not None:
            self._async_on_status_updated()

        self.async_write_ha_state()

    @callback
    def _async_on_status_updated(self) -> None:
        """Handle updated status from the thermostat."""

        self._attr_hvac_mode = (
            self._thermostat.status.mode
            if self._thermostat.status.power
            else HVACMode.OFF
        )
        self._attr_fan_mode = self._thermostat.status.fanspeed
        self._attr_current_temperature = self._thermostat.status.curtemp
        self._attr_target_temperature = self._thermostat.status.settemp
        self._attr_preset_mode = (
            PRESET_ECO if self._thermostat.status.powersave else PRESET_NONE
        )
        # self._attr_hvac_action = self._get_current_hvac_action()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""

        temperature: float | None
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        previous_temperature = self._target_temperature
        self._target_temperature = temperature

        self.async_write_ha_state()

        try:
            await self._thermostat.async_set_temperature(self._target_temperature)
        except PanasonicHCException:
            _LOGGER.error(
                "[%s] Failed setting temperature", self._thermostat.mac_address
            )
            self._target_temperature = previous_temperature
            self.async_write_ha_state()
        except ValueError as ex:
            raise ServiceValidationError("Invalid temperature") from ex

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""

        try:
            if hvac_mode is HVACMode.OFF:
                await self._thermostat.async_set_power(False)
            else:
                await self._thermostat.async_set_power(True)
                await self._thermostat.async_set_mode(hvac_mode.value)
        except PanasonicHCException:
            _LOGGER.error("[%s] Failed setting HVAC mode", self._thermostat.mac_address)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""

        try:
            await self._thermostat.async_set_energysaving(preset_mode == PRESET_ECO)
        except PanasonicHCException:
            _LOGGER.warning("[%s] Failed to set preset", self._thermostat.mac_address)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan speed."""

        try:
            await self._thermostat.async_set_fanmode(fan_mode)
        except PanasonicHCException:
            _LOGGER.warning("[%s] Failed to set fan mode", self._thermostat.mac_address)
