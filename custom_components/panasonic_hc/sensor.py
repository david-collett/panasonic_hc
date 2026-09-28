"""Sensors for Panasonic H&C."""

from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.device_registry as dr
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
from .panasonic_hc import PanasonicHC
from .panasonic_hc_diag import describe_error

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class DiagSensorDescription(SensorEntityDescription):
    """Describes a diagnosis live-data sensor and which field it reads."""

    diag_attr: str


# Live operating-data sensors sourced from the diagnosis channel. Keys match
# attributes on panasonic_hc_diag.DiagnosticsState.
DIAG_SENSORS: tuple[DiagSensorDescription, ...] = (
    DiagSensorDescription(
        key="indoor_suction_temp",
        diag_attr="indoor_suction_temp",
        name="Indoor suction temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="indoor_pipe_temp_1",
        diag_attr="indoor_pipe_temp_1",
        name="Indoor coil temperature (E1)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="indoor_pipe_temp_2",
        diag_attr="indoor_pipe_temp_2",
        name="Indoor coil temperature (E2)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="outdoor_temp",
        diag_attr="outdoor_temp",
        name="Outdoor air temperature (TO)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="outdoor_pipe_temp_1",
        diag_attr="outdoor_pipe_temp_1",
        name="Outdoor coil temperature (C1)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="outdoor_pipe_temp_2",
        diag_attr="outdoor_pipe_temp_2",
        name="Outdoor coil temperature (C2)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="discharge_temp",
        diag_attr="discharge_temp",
        name="Compressor discharge temperature (TD)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="suction_temp",
        diag_attr="suction_temp",
        name="Compressor suction temperature (TS)",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="compressor_freq",
        diag_attr="compressor_freq",
        name="Compressor frequency",
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    DiagSensorDescription(
        key="outdoor_total_current",
        diag_attr="outdoor_total_current",
        name="Outdoor unit current (CT1)",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialise sensor platform."""

    thermostat: PanasonicHC = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[SensorEntity] = [
        PanasonicHCEnergy(thermostat),
        PanasonicHCFirmware(thermostat),
        PanasonicHCErrorCode(thermostat),
        PanasonicHCAlarmHistory(thermostat),
    ]
    entities.extend(
        PanasonicHCDiagSensor(thermostat, description) for description in DIAG_SENSORS
    )

    async_add_entities(entities)


class PanasonicHCEnergy(SensorEntity):
    """Sensor entity to represent daily power usage."""

    _attr_name = "Daily Energy"
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the sensor entity."""

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

        if self._thermostat.curhour is not None:
            today = sum(
                self._thermostat.consumption[24 : 24 + self._thermostat.curhour]
            )
            self._attr_native_value = today
            self.async_write_ha_state()


class _PanasonicHCDiagEntity(SensorEntity):
    """Base for diagnosis-channel sensors (error code + live data).

    Shares device info, connection-based availability and update wiring. These
    entities are only meaningful when the controller exposes the diagnosis
    channel, so availability also tracks ``thermostat.diag_available``.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_available = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Entities that need the diagnosis channel / real-time data stream stay
    # unavailable on controllers that only support the firmware query. Control-
    # channel entities (e.g. the alarm code) set _requires_diag = False.
    _requires_diag = True
    _requires_realtime = True

    def __init__(self, thermostat: PanasonicHC, unique_suffix: str) -> None:
        self._thermostat = thermostat
        self._attr_unique_id = f"{dr.format_mac(thermostat.mac_address)}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            name=f"{MODEL}_{thermostat.mac_address[-8:].replace(':','')}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            connections={(CONNECTION_BLUETOOTH, thermostat.mac_address)},
        )

    async def async_added_to_hass(self) -> None:
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
        self._thermostat.unregister_update_callback(self._async_on_updated)

    @callback
    def _async_on_disconnected(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _async_on_connected(self) -> None:
        # Control-channel entities are available whenever connected. Diagnosis-
        # channel entities require the channel (and, for live data, real-time
        # support).
        if self._requires_diag:
            available = self._thermostat.diag_available
            if self._requires_realtime:
                available = available and self._thermostat.diag_realtime_supported
        else:
            available = True
        self._attr_available = available
        self.async_write_ha_state()

    @callback
    def _async_on_updated(self) -> None:
        """Refresh the value from the current diagnostics state."""

        self._update_value()
        self.async_write_ha_state()

    def _update_value(self) -> None:
        raise NotImplementedError


class PanasonicHCErrorCode(_PanasonicHCDiagEntity):
    """Active alarm/error code reported by the unit (control channel).

    State is the alarm code (e.g. ``"F04"``), ``"Fault"`` when the unit
    signals an alarm but the code has not been read yet, or ``"OK"`` when no
    fault is present. Read over the control channel, so available on any
    connected controller (does not need the diagnosis channel).
    """

    _attr_name = "Error code"
    _attr_icon = "mdi:alert-circle-outline"
    _requires_diag = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        super().__init__(thermostat, "error_code")

    def _update_value(self) -> None:
        occurred = self._thermostat.alarm_occurred
        history = self._thermostat.alarm_history
        # When a fault is active, the most recent history slot is the active
        # code. When there is no active fault, report OK.
        active_code = history[0] if (occurred and history) else None
        self._attr_native_value = active_code or ("Fault" if occurred else "OK")
        self._attr_extra_state_attributes = {
            "meaning": describe_error(active_code),
            "alarm_active": occurred,
        }


class PanasonicHCAlarmHistory(_PanasonicHCDiagEntity):
    """The indoor unit's stored alarm history (control channel).

    State is the most recent stored alarm code; the full list and their
    meanings are attributes.
    """

    _attr_name = "Alarm history"
    _attr_icon = "mdi:history"
    _requires_diag = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        super().__init__(thermostat, "alarm_history")

    def _update_value(self) -> None:
        history = [c for c in self._thermostat.alarm_history if c]
        self._attr_native_value = history[0] if history else "None"
        self._attr_extra_state_attributes = {
            "codes": history,
            "meanings": {c: describe_error(c) for c in history},
        }


class PanasonicHCFirmware(_PanasonicHCDiagEntity):
    """Controller firmware version(s) reported over the diagnosis channel.

    Supported by controllers that only expose the firmware query, so it does
    not require the real-time stream.
    """

    _attr_name = "Firmware"
    _attr_icon = "mdi:chip"
    _requires_realtime = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        super().__init__(thermostat, "firmware")

    def _update_value(self) -> None:
        diag = self._thermostat.diagnostics
        self._attr_native_value = diag.firmware
        self._attr_extra_state_attributes = {
            "main": diag.firmware_main,
            "wireless_module": diag.firmware_wireless,
        }


class PanasonicHCDiagSensor(_PanasonicHCDiagEntity):
    """A single live operating-data value from the diagnosis channel."""

    entity_description: DiagSensorDescription

    def __init__(
        self, thermostat: PanasonicHC, description: DiagSensorDescription
    ) -> None:
        super().__init__(thermostat, description.key)
        self.entity_description = description

    def _update_value(self) -> None:
        self._attr_native_value = getattr(
            self._thermostat.diagnostics, self.entity_description.diag_attr
        )
