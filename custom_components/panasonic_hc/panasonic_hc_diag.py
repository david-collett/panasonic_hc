"""Diagnosis-channel protocol for Panasonic H&C controllers.

This is SEPARATE from the control protocol in ``panasonic_hc_proto.py``.

The "Panasonic H&C Diagnosis App" talks to the same controller over a
different pair of GATT characteristics using ASCII command mnemonics
(``"1"``, ``"M180D"``, ...). Commands are written to the diagnosis write
characteristic; responses arrive on the diagnosis notify characteristic as
XOR-obfuscated ASCII lines terminated by CRLF.

Reverse-engineered from the Diagnosis app (com.panasonic.hc.diagnosis).
Obfuscation, command set and field offsets were recovered from the
decompiled app; the error-byte decode and codec are unit-tested. Real-time
field offsets are annotated with their source and are confirmed against a
live capture during bring-up.

Channels on the CZ-RTC6BLW GATT server (same physical connection):
    control   : write 4d200002, notify 4d200003  (0x11 framing, XOR 105/202)
    diagnosis : write 4d200004, notify 4d200005  (ASCII mnemonics, XOR 90/249)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging

_LOGGER = logging.getLogger(__name__)

# GATT characteristics for the diagnosis channel (same UUID family as control).
BLE_CHAR_DIAG_WRITE = "4d200004-eff3-4362-b090-a04cab3f1da0"
BLE_CHAR_DIAG_NOTIFY = "4d200005-eff3-4362-b090-a04cab3f1da0"

# ASCII command mnemonics (written to BLE_CHAR_DIAG_WRITE, obfuscated).
CMD_FIRMWARE = "V"
CMD_REALTIME_START = "1"
CMD_REALTIME_STOP = "0"
CMD_HISTORY_INDOOR = "M180D"
CMD_HISTORY_OUTDOOR = "M280D"
CMD_DEVICEID_INDOOR = "M180I"
CMD_DEVICEID_OUTDOOR = "M280I"

# Obfuscation constants (C/a.java, wired-mode / f358v == false path).
_XOR_TAIL = 90
_XOR_SEED = 249
_FRAME_LEN = 16


# ---------------------------------------------------------------------------
# Obfuscation codec
# encode: pad ASCII cmd to 16 bytes, chain-XOR (seed 249), then XOR 90
# decode: XOR 90, then un-chain (seed 249). Inverts encode; unit-tested.
# ---------------------------------------------------------------------------
def diag_encode(cmd: str) -> bytes:
    """Encode an ASCII command mnemonic into a 16-byte obfuscated frame."""

    plain = list(cmd.encode("ascii"))
    if len(plain) > _FRAME_LEN:
        raise ValueError(f"Command too long: {cmd!r}")
    plain += [0] * (_FRAME_LEN - len(plain))

    chained = [0] * _FRAME_LEN
    chained[0] = plain[0] ^ _XOR_SEED
    for i in range(1, _FRAME_LEN):
        chained[i] = plain[i] ^ chained[i - 1]

    return bytes((chained[i] ^ _XOR_TAIL) & 0xFF for i in range(_FRAME_LEN))


def diag_decode_bytes(data: bytes) -> list[int]:
    """Reverse the obfuscation of one notification packet to raw bytes."""

    stage = [b ^ _XOR_TAIL for b in data]
    out = [0] * len(stage)
    if stage:
        out[0] = stage[0] ^ _XOR_SEED
        for i in range(1, len(stage)):
            out[i] = stage[i] ^ stage[i - 1]
    return out


def diag_decode_ascii(data: bytes) -> str:
    """Decode a notification packet to ASCII, dropping 0x00/0xFF filler.

    Mirrors the native ``j()`` string builder which skips 0x00 and 0xFF
    bytes (used as frame padding) and treats the rest as ASCII.
    """

    return "".join(chr(x) for x in diag_decode_bytes(data) if x not in (0x00, 0xFF))


def diag_decode_hex(data: bytes) -> str:
    """Decode a notification packet to an uppercase hex string.

    Mirrors the native ``k()`` builder used for the device-ID (``M180I``)
    responses, skipping 0x00/0xFF filler bytes.
    """

    return "".join(f"{x:02X}" for x in diag_decode_bytes(data) if x not in (0x00, 0xFF))


# ---------------------------------------------------------------------------
# Error-code decode
# ---------------------------------------------------------------------------
# PAC map (native f8c): 32-value blocks. code = letter + (byte - block_base).
_PAC_BLOCKS = {0x40: "E", 0x60: "F", 0x80: "H", 0xC0: "L", 0xE0: "P"}

# RAC outdoor map (native f9d): sparse.
_RAC_MAP = {
    0x94: "E06", 0x63: "F04", 0x66: "F05", 0x49: "F08",
    0x38: "H01", 0x92: "H02", 0x48: "H03", 0x3A: "L18",
    0x53: "P03", 0x56: "P04", 0x9A: "P05", 0x74: "P07",
    0xC0: "P13", 0x90: "P15", 0x75: "P16", 0x37: "P22", 0x36: "P29",
}

# Alternate code aliases shown by the app for certain raw codes.
_CODE_ALIASES = {
    "F90": "H02", "F91": "P15", "F93": "P29", "F95": "P04", "F96": "P07",
    "F97": "P03", "F98": "H01", "F99": "P16", "H33": "P05", "H96": "P13",
    "H97": "P22",
}

# No fault.
NO_ERROR = 0x00


def decode_error_byte(byte: int, device_type: str = "PAC") -> str | None:
    """Decode a raw error byte to an alarm code string (e.g. ``"F11"``).

    Returns ``None`` when the byte indicates no fault. ``device_type`` is
    ``"PAC"`` (commercial, grid map) or ``"RAC"`` (residential, sparse map).
    """

    if byte in (NO_ERROR, 0xFF):
        return None

    if device_type == "RAC":
        code = _RAC_MAP.get(byte)
    else:
        code = None
        for base, letter in _PAC_BLOCKS.items():
            if base <= byte <= base + 0x1F:
                code = f"{letter}{byte - base:02d}"
                break

    if code is None:
        return None
    return _CODE_ALIASES.get(code, code)


# Official alarm-code meanings, taken from the Panasonic H&C app's own English
# string table. Not every unit emits every code; a unit only ever reports its
# own subset, and undocumented codes fall through to describe_error's default.
ERROR_CODES: dict[str, str] = {
    # E** — Serial communication / mis-setting
    "E01": "Remote controller reception error",
    "E02": "Remote controller transmission error",
    "E03": "Error in indoor unit receiving signal from remote controller (central)",
    "E04": "Error in indoor unit receiving signal from the outdoor unit",
    "E06": "Outdoor unit failed to receive serial communication signals from indoor unit",
    "E07": "Outdoor unit transmission failure to indoor unit",
    "E08": "Duplicate indoor unit address settings error",
    "E09": "More than one remote controller set to main error",
    "E12": "Automatic address setting start is prohibited while auto-address setting in progress",
    "E14": "Main unit duplication in simultaneous-operation multi control (detected outdoor unit)",
    "E15": "Automatic address alarm (total capacity of indoor units is too low)",
    "E16": "Automatic address alarm (total indoor capacity too high or too many indoor units)",
    "E18": "Faulty communication in group control wiring",
    "E20": "Connection problem of indoor/outdoor units",
    "E24": "CCU reception failure from outdoor unit sub bus",
    "E29": "Outdoor unit reception failure from CCU (outdoor unit communication error)",
    "E31": "Communication error within the unit (control board microcomputer reception failure)",
    # F** — Sensor / nonvolatile memory faults
    "F01": "Indoor unit E1 sensor error",
    "F02": "Indoor unit E2 sensor error",
    "F04": "Compressor 1 discharge temperature sensor (TD) trouble",
    "F06": "Inlet temperature sensor (C1) in heat exchanger trouble",
    "F07": "Intermediate temperature sensor (C2) in heat exchanger trouble",
    "F08": "Outdoor air temperature sensor (TO) trouble",
    "F10": "Indoor unit body sensor error",
    "F11": "4-way valve operation failure",
    "F12": "Compressor inlet suction temperature sensor (TS) trouble",
    "F14": "Engine room temperature sensor error",
    "F29": "Indoor unit nonvolatile memory (EEPROM) error",
    "F31": "Outdoor unit nonvolatile memory (EEPROM) trouble",
    # H** — Current / power electronics protection
    "H01": "Primary (input) overcurrent detected",
    "H02": "PAM trouble",
    "H03": "Primary current CT sensor (current sensor) failure",
    "H11": "Outdoor unit failed to receive serial communication signals from indoor unit",
    "H15": "Compressor 1 discharge temperature sensor (TD) trouble",
    "H16": "Primary current CT sensor (current sensor) failure",
    "H27": "Outdoor air temperature sensor (TO) trouble",
    "H28": "Inlet temperature sensor (C1) in heat exchanger trouble",
    "H31": "HIC trouble",
    # L** — Setting / configuration errors
    "L02": "Inconsistency between indoor and outdoor unit type",
    "L03": "Setting of multiple main units by group control",
    "L04": "Outdoor unit address duplication",
    "L07": "Individual indoor unit with group wiring",
    "L08": "Indoor unit address unset or group unset",
    "L09": "Indoor unit capacity unset",
    "L10": "Outdoor unit capacity not set or invalid",
    "L13": "Indoor unit type setting error",
    "L18": "4-way valve operation failure",
    # P** — Protective device activation
    "P01": "Indoor unit fan error",
    "P03": "Compressor discharge temperature trouble",
    "P04": "High pressure trouble",
    "P05": "AC power supply trouble",
    "P07": "DC overcurrent / heat sink thermo",
    "P09": "Connection failure of indoor unit ceiling panel connector",
    "P10": "Indoor unit float switch activated",
    "P11": "Freezing alarm",
    "P12": "Connection failure of low water alarm cancellation connector",
    "P13": "Alarm valve open",
    "P14": "O2 sensor detect",
    "P15": "Insufficient gas level detected",
    "P16": "Compressor overcurrent trouble",
    "P19": "Four-way valve lock failure",
    "P20": "Refrigerant pressure too high (service valve opening failure)",
    "P22": "Outdoor unit fan motor trouble",
    "P23": "Interlock alarm",
    "P26": "Inverter compressor error (HIC circuit)",
    "P29": "INV compressor wiring/actuation failure (including locked) or DCCT failure",
    "P31": "Group control error",
}


def describe_error(code: str | None) -> str | None:
    """Return a human-readable meaning for an alarm code, or None."""

    if not code:
        return None
    return ERROR_CODES.get(code, f"Unknown alarm code {code}")


# ---------------------------------------------------------------------------
# Real-time data parsing
# ---------------------------------------------------------------------------
# Real-time lines are ASCII where each aircon data byte is two hex chars.
# The leading '<' (indoor) / '>' (outdoor) marker is stripped before parsing;
# a 3-byte (6 hex char) header precedes the payload. The subtype is the first
# two hex chars of the stripped line: indoor "10 40 50 83", outdoor "10 40 83".
#
# Field offsets below are from the decompiled native parser A/f.java. They are
# defensive: any parse failure yields None for that field rather than raising.


def _byte_at(line: str, char_off: int) -> int | None:
    """Read one byte (two hex chars) at a character offset, or None."""

    try:
        return int(line[char_off : char_off + 2], 16)
    except (ValueError, IndexError):
        return None


def _bits(line: str, byte_index: int, bit_start: int, bit_len: int) -> int | None:
    """Native ``c()``: byte at char (byte_index*2 + 6), MSB-first bit slice."""

    b = _byte_at(line, byte_index * 2 + 6)
    if b is None:
        return None
    binary = f"{b:08b}"
    return int(binary[bit_start : bit_start + bit_len], 2)


def _temp_half_offset(raw: int | None) -> float | None:
    """Native temp: raw/2 - 35.0 (Celsius) for the /2,-35 sensor encoding."""

    if raw is None:
        return None
    return raw / 2.0 - 35.0


@dataclass
class DiagnosticsState:
    """Parsed diagnosis data. Fields are None until first seen."""

    # Fault state (highest-value, highest-confidence).
    indoor_error_code: str | None = None
    outdoor_error_code: str | None = None

    # Setpoint / mode (indoor <50).
    setting_temp: float | None = None
    operation_mode_raw: int | None = None

    # Indoor temps (<83).
    indoor_suction_temp: float | None = None  # TA-ish suction air
    indoor_pipe_temp_1: float | None = None  # E1
    indoor_pipe_temp_2: float | None = None  # E2

    # Outdoor values (>83 for PAC).
    outdoor_temp: float | None = None  # TO
    outdoor_pipe_temp_1: float | None = None  # C1
    outdoor_pipe_temp_2: float | None = None  # C2
    discharge_temp: float | None = None  # TD
    suction_temp: float | None = None  # TS
    compressor_freq: float | None = None  # Hz
    outdoor_total_current: float | None = None  # A (CT1)

    # Bookkeeping.
    firmware: str | None = None
    firmware_main: str | None = None
    firmware_wireless: str | None = None
    raw_lines: list[str] = field(default_factory=list)

    @property
    def error_code(self) -> str | None:
        """The active alarm code, preferring outdoor then indoor."""

        return self.outdoor_error_code or self.indoor_error_code


def _signed16_by_markers(line: str, hi: str, lo: str, div: float = 10.0) -> float | None:
    """Native f(): signed 16-bit from two TLV markers, divided by ``div``.

    Marker ``hi``/``lo`` are two-hex-char tokens; the value byte follows the
    marker token in the line. Returns None if either marker is absent.
    """

    try:
        i_hi = line.index(hi)
        i_lo = line.index(lo)
    except ValueError:
        return None
    b_hi = _byte_at(line, i_hi + 2)
    b_lo = _byte_at(line, i_lo + 2)
    if b_hi is None or b_lo is None:
        return None
    val = (b_hi << 8) | b_lo
    if val & 0x8000:
        val -= 0x10000
    return val / div


class DiagnosticsParser:
    """Accumulates diagnosis notifications and parses complete lines.

    Feed raw notification ``bytes`` to :meth:`feed`; completed CRLF lines are
    parsed into :attr:`state`. ``device_type`` selects the error map.
    """

    def __init__(self, device_type: str = "PAC") -> None:
        self.device_type = device_type
        self.state = DiagnosticsState()
        self._ascii_buf = ""
        # Number of real-time (subtype) lines parsed; used to detect whether a
        # controller actually supports the real-time stream vs "NOTSUPPORT".
        self.realtime_lines = 0
        # True if the controller answered a command with "NOTSUPPORT".
        self.last_not_supported = False

    def feed(self, data: bytes) -> None:
        """Decode a notification packet and parse any completed lines."""

        self._ascii_buf += diag_decode_ascii(data)
        while "\r\n" in self._ascii_buf:
            line, self._ascii_buf = self._ascii_buf.split("\r\n", 1)
            if line:
                self._parse_line(line.strip())

    def _parse_line(self, line: str) -> None:
        try:
            self.state.raw_lines = (self.state.raw_lines + [line])[-32:]

            # Controller rejected the command.
            if line == "NOTSUPPORT":
                self.last_not_supported = True
                return

            # Firmware version lines: "F Ver x.x.x" (main) / "W Ver x.x.x"
            # (wireless module). Keep both in a combined string.
            if line.startswith("F Ver"):
                self.state.firmware_main = line[len("F Ver") :].strip()
                self._update_firmware()
                return
            if line.startswith("W Ver"):
                self.state.firmware_wireless = line[len("W Ver") :].strip()
                self._update_firmware()
                return

            marker = line[:1]
            if marker not in ("<", ">"):
                return
            self.realtime_lines += 1
            body = line[1:]  # strip </> marker
            subtype = body[:2]

            if marker == "<":
                self._parse_indoor(body, subtype)
            else:
                self._parse_outdoor(body, subtype)
        except Exception as exc:  # never let a bad line kill the stream
            _LOGGER.debug("Diag line parse error (%r): %s", line, exc)

    def _update_firmware(self) -> None:
        parts = []
        if self.state.firmware_main:
            parts.append(f"F {self.state.firmware_main}")
        if self.state.firmware_wireless:
            parts.append(f"W {self.state.firmware_wireless}")
        self.state.firmware = " / ".join(parts) or None

    def _parse_indoor(self, body: str, subtype: str) -> None:
        if subtype == "10":
            # error byte at char 8 (A/f.java <10 case)
            b = _byte_at(body, 8)
            if b is not None:
                self.state.indoor_error_code = decode_error_byte(b, self.device_type)
        elif subtype == "50":
            # setpoint at chars 8:10, /2 if < 127; mode nibble at char 6
            raw = _byte_at(body, 8)
            if raw is not None and raw < 127:
                self.state.setting_temp = raw / 2.0
            try:
                self.state.operation_mode_raw = int(body[6:7], 16)
            except (ValueError, IndexError):
                pass
        elif subtype == "83":
            # temps via native c() bit reads: raw/2 - 35.0
            self.state.indoor_suction_temp = _temp_half_offset(_bits(body, 3, 0, 8))
            self.state.indoor_pipe_temp_1 = _temp_half_offset(_bits(body, 7, 0, 8))
            self.state.indoor_pipe_temp_2 = _temp_half_offset(_bits(body, 8, 0, 8))
            b = _byte_at(body, 26)
            if b is not None:
                code = decode_error_byte(b, self.device_type)
                if code:
                    self.state.indoor_error_code = code

    def _parse_outdoor(self, body: str, subtype: str) -> None:
        if subtype == "10":
            # RAC outdoor error byte at char 8
            b = _byte_at(body, 8)
            if b is not None:
                self.state.outdoor_error_code = decode_error_byte(b, self.device_type)
        elif subtype == "83":
            # PAC outdoor: signed16-by-marker fields (A/f.java >83 case)
            self.state.outdoor_temp = _signed16_by_markers(body, "09", "0A")
            self.state.outdoor_pipe_temp_1 = _signed16_by_markers(body, "07", "08")
            self.state.outdoor_pipe_temp_2 = _signed16_by_markers(body, "05", "06")
            self.state.discharge_temp = _signed16_by_markers(body, "03", "04")
            self.state.suction_temp = _signed16_by_markers(body, "01", "02")
            self.state.compressor_freq = _signed16_by_markers(body, "0F", "10")
            self.state.outdoor_total_current = _signed16_by_markers(body, "11", "12")
            b = _byte_at(body, 18)
            if b is not None:
                code = decode_error_byte(b, self.device_type)
                if code:
                    self.state.outdoor_error_code = code
