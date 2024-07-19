"""Handle communication with supported Panasonic H&C devices."""

import asyncio
from collections.abc import Callable
import logging

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .panasonic_hc_proto import (
    FANSPEED,
    MODE,
    PanasonicBLEEnergySaving,
    PanasonicBLEFanMode,
    PanasonicBLEMode,
    PanasonicBLEParcel,
    PanasonicBLEPower,
    PanasonicBLEStatusReq,
    PanasonicBLETemp,
)

MIN_TEMP = 16
MAX_TEMP = 32  # FIXME: check these

BLE_CHAR_WRITE = "4d200002-eff3-4362-b090-a04cab3f1da0"
BLE_CHAR_NOTIFY = "4d200003-eff3-4362-b090-a04cab3f1da0"

_LOGGER = logging.getLogger(__name__)


class PanasonicHCException(Exception):
    """PanasonicHC Exception."""


class Status:
    """Class representing current HVAC status."""

    def __init__(
        self,
        power: bool,
        mode: str,
        powersave: bool,
        curtemp: float,
        settemp: float,
        fanspeed: str,
    ) -> None:
        """Initialise Status."""

        self.power = power
        self.mode = mode
        self.powersave = powersave
        self.curtemp = curtemp
        self.settemp = settemp
        self.fanspeed = fanspeed


class PanasonicHC:
    """Class representing the Panasonic Controller."""

    def __init__(self, ble_device: BLEDevice, mac_address: str) -> None:
        """Initialise Panasonic H&C Controller."""

        self.device = ble_device
        self.mac_address = mac_address
        self._on_update_callbacks: list[Callable] = []
        self._conn: BleakClient = BleakClient(ble_device)
        self._lock = asyncio.Lock()
        self.status = None

    @property
    def is_connected(self) -> bool:
        """Return trie if connected to thermostat."""

        return self._conn.is_connected

    def register_update_callback(self, on_update: Callable) -> None:
        """Register a callback to be called on updated data."""

        self._on_update_callbacks.append(on_update)

    def unregister_update_callback(self, on_update: Callable) -> None:
        """Unregister update callback."""

        if on_update in self._on_update_callbacks:
            self._on_update_callbacks.remove(on_update)

    async def async_connect(self) -> None:
        """Connect to thermostat."""

        try:
            await self._conn.connect()
            await self._conn.start_notify(BLE_CHAR_NOTIFY, self.on_notification)
            await self.async_get_status()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not connect to Thermostat") from e

    async def async_disconnect(self) -> None:
        """Shutdown thermostat connection."""

        try:
            await self._conn.disconnect()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not disconnect from Thermostat") from e

    async def async_get_status(self) -> None:
        """Query current status."""

        await self._async_write_command(PanasonicBLEStatusReq())

    async def _async_write_command(self, command: PanasonicBLEParcel):
        """Write a command to the write characteristic."""

        if not self.is_connected:
            raise PanasonicHCException("Not Connected")

        data = command.encode()

        async with self._lock:
            try:
                await self._conn.write_gatt_char(BLE_CHAR_WRITE, data)
            except (BleakError, TimeoutError) as e:
                raise PanasonicHCException("Error during write") from e

    def on_notification(self, handle: BleakGATTCharacteristic, data: bytes) -> None:
        """Handle data from BLE GATT Notifications."""

        _LOGGER.info("Received BLE packet")
        try:
            parcel = PanasonicBLEParcel.parse(data=data)
            for packet in parcel:
                if isinstance(packet, PanasonicBLEParcel.PanasonicBLEPacketStatus):
                    self.status = Status(
                        packet.power,
                        packet.mode.name,
                        packet.powersave,
                        packet.curtemp,
                        packet.temp,
                        packet.fanspeed.name,
                    )
                    for callback in self._on_update_callbacks:
                        callback()
        except Exception as e:
            _LOGGER.error("Error parsing packet: %s", e)

    async def async_set_power(self, state: bool) -> None:
        """Set power state."""

        await self._async_write_command(PanasonicBLEPower(1 if state else 0))

    async def async_set_temperature(self, temp: float) -> None:
        """Set target temperature."""

        await self._async_write_command(PanasonicBLETemp(temp))

    async def async_set_mode(self, mode: str):
        """Set thermostat mode."""

        await self._async_write_command(PanasonicBLEMode(MODE[mode].value))

    async def async_set_fanmode(self, mode: str):
        """Set thermostat mode."""

        await self._async_write_command(PanasonicBLEFanMode(FANSPEED[mode].value))

    async def async_set_energysaving(self, state: bool):
        """Toggle EnergySaving mode."""

        await self._async_write_command(PanasonicBLEEnergySaving(state))
