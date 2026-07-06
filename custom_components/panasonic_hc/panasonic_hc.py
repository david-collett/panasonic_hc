"""Handle communication with supported Panasonic H&C devices."""

import asyncio
from collections.abc import Callable
import logging
import time

from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .panasonic_hc_proto import (
    FANSPEED,
    MODE,
    PanasonicBLEAlarmHistoryReq,
    PanasonicBLEEnergySaving,
    PanasonicBLEFanMode,
    PanasonicBLEMode,
    PanasonicBLEParcel,
    PanasonicBLEPower,
    PanasonicBLEPowerReq,
    PanasonicBLEPowerReqHour,
    PanasonicBLEStatusReq,
    PanasonicBLETemp,
)
from .panasonic_hc_diag import (
    BLE_CHAR_DIAG_NOTIFY,
    BLE_CHAR_DIAG_WRITE,
    CMD_FIRMWARE,
    CMD_REALTIME_START,
    CMD_REALTIME_STOP,
    DiagnosticsParser,
    DiagnosticsState,
    diag_encode,
)

MIN_TEMP = 16
MAX_TEMP = 32  # FIXME: check these

BLE_CHAR_WRITE = "4d200002-eff3-4362-b090-a04cab3f1da0"
BLE_CHAR_NOTIFY = "4d200003-eff3-4362-b090-a04cab3f1da0"
CONSUMPTION_INTERVAL = 300

# How long to let the diagnosis real-time stream run to collect a full set of
# subtype packets before stopping it again.
DIAG_STREAM_SECONDS = 3.0

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

    def __init__(
        self, ble_device: BLEDevice, mac_address: str, device_type: str = "PAC"
    ) -> None:
        """Initialise Panasonic H&C Controller."""

        self.last_update = 0
        self.device = ble_device
        self.mac_address = mac_address
        self._on_update_callbacks: list[Callable] = []
        self._conn = None
        self._lock = asyncio.Lock()
        self.status = None
        self.curhour = None
        self.curindex = None
        self.consumption = [0] * 48

        # Control-channel alarm state (works on hardware where the diagnosis
        # channel does not). alarm_occurred comes from the status packet's
        # data[7]; alarm_history is read from the indoor unit (cm 39), most
        # recent slot first.
        self.alarm_occurred = False
        self.alarm_history: list[str | None] = []
        self._alarm_hist_buf: list[str | None] = []

        # Diagnosis channel (error codes + live operating data). Available only
        # when the controller exposes the diagnosis characteristics; probed on
        # connect and left disabled otherwise.
        self._diag_parser = DiagnosticsParser(device_type=device_type)
        self.diag_available = False
        # Whether the controller supports the real-time data stream. Some
        # controllers expose the diagnosis channel but answer data commands
        # with "NOTSUPPORT" (only the firmware query works).
        self.diag_realtime_supported = False

    @property
    def is_connected(self) -> bool:
        """Return true if connected to thermostat."""

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
            self._conn = await establish_connection(
                BleakClientWithServiceCache,
                self.device,
                name=self.device.name or self.device.address
            )
            await asyncio.sleep(0.5)
            await self._conn.start_notify(BLE_CHAR_NOTIFY, self.on_notification)
            await asyncio.sleep(0.5)
            await self._async_start_diag_notify()
            await self._async_detect_diag_capabilities()
            await self.async_get_status()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not connect to Thermostat") from e

    async def _async_start_diag_notify(self) -> None:
        """Subscribe to the diagnosis notify characteristic if present.

        The diagnosis channel is optional; controllers that do not expose it
        simply run without error-code / live-data sensors.
        """

        try:
            await self._conn.start_notify(
                BLE_CHAR_DIAG_NOTIFY, self.on_diag_notification
            )
        except (BleakError, TimeoutError, KeyError) as e:
            _LOGGER.info(
                "[%s] Diagnosis channel unavailable (%s); "
                "error-code / live-data sensors disabled",
                self.mac_address,
                e,
            )
            self.diag_available = False
            return

        self.diag_available = True
        await asyncio.sleep(0.5)

    async def async_disconnect(self) -> None:
        """Shutdown thermostat connection."""

        try:
            await self._conn.disconnect()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not disconnect from Thermostat") from e

    async def async_get_status(self) -> None:
        """Query current status."""

        # always update status (carries the alarm-active flag in data[7])
        await self._async_write_command(PanasonicBLEStatusReq())

        # update consumption if interval has passed
        now = time.time()
        if now > self.last_update + CONSUMPTION_INTERVAL:
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReq())
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReqHour())
            self.last_update = now

    # Number of history slots to read (1 = most recent).
    ALARM_HISTORY_SLOTS = 4

    async def async_update_alarm_history(self) -> None:
        """Read the indoor unit's alarm history (cm 39).

        Reads the most-recent slots into :attr:`alarm_history` (most recent
        first). Fires update callbacks when done.
        """

        self._alarm_hist_buf = []
        for idx in range(1, self.ALARM_HISTORY_SLOTS + 1):
            await self._async_write_command(PanasonicBLEAlarmHistoryReq(idx))
            await asyncio.sleep(0.3)

        # Let the last response land before finalising.
        await asyncio.sleep(0.3)
        self.alarm_history = list(self._alarm_hist_buf)

        for callback in self._on_update_callbacks:
            callback()

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

        try:
            do_callback = False
            parcel = PanasonicBLEParcel.parse(data=data)
            _LOGGER.debug("Received packet data: %s", parcel)
            for packet in parcel:
                if isinstance(packet, PanasonicBLEParcel.PanasonicBLEPacketStatus):
                    # Some packets do not have curtemp at this index, I'm not sure how to
                    # identify them, so we will do some sanity filtering
                    if self.status:
                        if not packet.curtemp or abs(packet.curtemp - self.status.curtemp) > 20:
                            packet.curtemp = self.status.curtemp

                    self.status = Status(
                        packet.power,
                        packet.mode.name,
                        packet.powersave,
                        packet.curtemp,
                        packet.temp,
                        packet.fanspeed.name,
                    )
                    self.alarm_occurred = packet.alarm_occurred
                    do_callback = True
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketAlarmHistory
                ):
                    # Collect the sweep's history slots in request order.
                    self._alarm_hist_buf.append(packet.alarm_code)
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketConsumption
                ):
                    if packet.hour is not None:
                        self.curhour = packet.hour
                    if packet.index is not None:
                        self.curindex = packet.index
                    if packet.values is not None:
                        for i, value in enumerate(packet.values):
                            offset = self.curhour + 24 - 1 - self.curindex
                            if offset < 0:
                                offset += 48
                            idx = offset + packet.pos + i
                            if idx >= 48:
                                idx -= 48
                            _LOGGER.debug("Writing %s to index %s", value, idx)
                            self.consumption[idx] = value
                        do_callback = True

            _LOGGER.debug(
                "Consumption: %s, curindex: %s, curhour: %s",
                self.consumption,
                self.curindex,
                self.curhour,
            )
            if do_callback:
                for callback in self._on_update_callbacks:
                    callback()
        except Exception as e:
            _LOGGER.error("Error parsing packet: %s", e)

    # ------------------------------------------------------------------
    # Diagnosis channel
    # ------------------------------------------------------------------
    @property
    def diagnostics(self) -> DiagnosticsState:
        """Return the latest parsed diagnosis state."""

        return self._diag_parser.state

    def on_diag_notification(
        self, handle: BleakGATTCharacteristic, data: bytes
    ) -> None:
        """Handle a notification on the diagnosis channel."""

        try:
            self._diag_parser.feed(bytes(data))
        except Exception as e:  # noqa: BLE001 - never let a bad packet propagate
            _LOGGER.debug("Error parsing diagnosis packet: %s", e)

    async def _async_detect_diag_capabilities(self) -> None:
        """Query firmware and detect whether real-time data is supported.

        Runs once per connect. Sends the firmware query (widely supported),
        then briefly starts the real-time stream to see whether the
        controller streams data or answers "NOTSUPPORT". Controllers that do
        not support the data commands still get a firmware reading.
        """

        if not self.diag_available:
            return

        # Firmware query.
        try:
            await self._async_write_diag(CMD_FIRMWARE)
            await asyncio.sleep(1.5)
        except PanasonicHCException:
            _LOGGER.debug("[%s] Firmware query failed", self.mac_address)

        # Probe real-time support.
        self._diag_parser.realtime_lines = 0
        try:
            await self._async_write_diag(CMD_REALTIME_START)
            await asyncio.sleep(DIAG_STREAM_SECONDS)
            await self._async_write_diag(CMD_REALTIME_STOP)
        except PanasonicHCException:
            _LOGGER.debug("[%s] Real-time probe failed", self.mac_address)

        self.diag_realtime_supported = self._diag_parser.realtime_lines > 0
        _LOGGER.info(
            "[%s] Diagnosis channel: firmware=%s, real-time data %s",
            self.mac_address,
            self._diag_parser.state.firmware,
            "supported" if self.diag_realtime_supported else "not supported",
        )

    async def _async_write_diag(self, cmd: str) -> None:
        """Write an ASCII diagnosis command to the diagnosis write char."""

        if not self.is_connected:
            raise PanasonicHCException("Not Connected")

        data = diag_encode(cmd)
        async with self._lock:
            try:
                await self._conn.write_gatt_char(BLE_CHAR_DIAG_WRITE, data)
            except (BleakError, TimeoutError) as e:
                raise PanasonicHCException("Error during diagnosis write") from e

    async def async_update_diagnostics(self) -> None:
        """Refresh error codes and live operating data.

        Briefly runs the real-time stream to capture a full set of subtype
        packets, then stops it. Parsed values land in :attr:`diagnostics` via
        the notification handler. Fires update callbacks when done.
        """

        if not (self.diag_available and self.diag_realtime_supported):
            return

        try:
            await self._async_write_diag(CMD_REALTIME_START)
            await asyncio.sleep(DIAG_STREAM_SECONDS)
        finally:
            # Always try to stop the stream, even if the wait was interrupted.
            try:
                await self._async_write_diag(CMD_REALTIME_STOP)
            except PanasonicHCException:
                _LOGGER.debug("[%s] Failed to stop diag stream", self.mac_address)

        for callback in self._on_update_callbacks:
            callback()

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
