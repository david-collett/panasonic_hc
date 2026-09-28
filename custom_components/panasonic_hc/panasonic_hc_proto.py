"""Data structures for Panasonic H&C Bluetooth controllers."""

from enum import Enum
import io
import logging


_LOGGER = logging.getLogger(__name__)


def _cksum(data):
    cksum = 0
    for i in data:
        cksum = (cksum + i) & 255
    return cksum


def _decode(data):
    data = list(data)
    for x in range(len(data)):
        data[x] = data[x] ^ 105

    for x in range(len(data) - 1, 0, -1):
        data[x] = data[x] ^ data[x - 1]

    data[0] = data[0] ^ 202

    if _cksum(data[1:-1]) != data[-1]:
        raise ValueError("Bad Checksum")

    return bytes(data)


def _encode(data):
    data = list(data)
    data[-1] = _cksum(data[1:-1])

    data[0] = data[0] ^ 202
    for x in range(1, len(data)):
        data[x] = data[x] ^ data[x - 1]

    for x in range(len(data)):
        data[x] = data[x] ^ 105

    return bytes(data)


def _bytes_to_floats(data):
    arr = [data[x] << 8 + data[x + 1] for x in range(0, len(data), 2)]


class MODE(Enum):
    heat = 1
    cool = 2
    fan_only = 3
    dry = 4
    auto = 5


class FANSPEED(Enum):
    auto = 2
    high = 3
    medium = 4
    low = 5


# Alarm/error class encoded in the top 3 bits of an alarm byte (from the H&C
# Control app's AlarmCodeData). Content (number) is the low 5 bits. Class "A"
# with content 0 means "no error".
_ALARM_CLASS = {0: "A", 1: "C", 2: "E", 3: "F", 4: "H", 5: "J", 6: "L", 7: "P"}


def decode_alarm_byte(b: int) -> str | None:
    """Decode a control-channel alarm byte to a code (e.g. "E04"), or None.

    Matches the Control app: class = (b >> 5) & 7, number = b & 0x1F,
    formatted "%s%02d". No-fault ("A00") returns None.
    """

    cls = _ALARM_CLASS.get((b >> 5) & 7)
    num = b & 0x1F
    if cls is None or (cls == "A" and num == 0):
        return None
    return f"{cls}{num:02d}"


class PanasonicBLEParcel:
    """A BLE Parcel."""

    idx = 0

    class PanasonicBLEPacket:
        """A BLE Parcel Packet."""

        class PACKET_TYPE(Enum):
            SET_TEMP = 76
            SET_POWER = 65
            SET_MODE = 66
            SET_POWERSAVE = 84

        def __init__(self, ptype, pdata):
            self.ptype = ptype
            self.pdata = pdata

        @staticmethod
        def parse(fd: io.BytesIO):
            """Construct Packet from fd."""

            ptype = fd.read(1)[0]
            plen = fd.read(1)[0]
            # 0xFF (Busy) and 0xFE (NAck) are sentinel lengths with no payload;
            # treating them as a real length reads bogus/short data and derails
            # parsing of the rest of the parcel.
            pdata = b"" if plen in (0xFE, 0xFF) else fd.read(plen)

            if ptype == 129:
                return PanasonicBLEParcel.PanasonicBLEPacketStatus(ptype, pdata)
            if ptype == 33:
                return PanasonicBLEParcel.PanasonicBLEPacketOutdoorTemp(ptype, pdata)

            if ptype == 105 and pdata[0:3] == bytes([2, 0, 19]):
                return PanasonicBLEParcel.PanasonicBLEPacketConsumption(ptype, pdata)

            # Active alarm code: cm 105 generic read, fn=27 (0x1B), dn=5.
            if ptype == 105 and len(pdata) >= 5 and pdata[2] == 27 and pdata[3] == 5:
                return PanasonicBLEParcel.PanasonicBLEPacketAlarm(ptype, pdata)

            # Alarm history: cm 39, alarm byte at pdata[2].
            if ptype == 39 and len(pdata) >= 3:
                return PanasonicBLEParcel.PanasonicBLEPacketAlarmHistory(ptype, pdata)

            return PanasonicBLEParcel.PanasonicBLEPacket(ptype, pdata)

        def encode(self):
            return self.ptype.to_bytes() + len(self.pdata).to_bytes() + self.pdata

        def __str__(self):
            return f"{self.ptype}, {list(self.pdata)}"

    class PanasonicBLEPacketStatus(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            self.curtemp = 0
            self.power = self.pdata[0] & 1
            self.mode = MODE((self.pdata[0] >> 5) & 7)
            self.temp = (self.pdata[4] - 70) / 2
            self.fanspeed = FANSPEED((self.pdata[1] >> 5) & 7)
            self.powersave = self.pdata[8]
            # Alarm-active flag (data[7] != 0), per the Control app.
            self.alarm_occurred = bool(self.pdata[7]) if len(self.pdata) > 7 else False

            if len(self.pdata) >= 6:
                self.curtemp = (self.pdata[5] - 70) / 2

        def __str__(self):
            s = super().__str__()
            s += f"\nTemp: {self.temp}"
            if self.curtemp:
                s += f" ({self.curtemp})"
            s += f'\nPower: { "on" if self.power else "off" }'
            s += f"\nMode: {self.mode.name}"
            s += f"\nFan: {self.fanspeed.name}"
            s += f'\nPowersave: { "on" if self.powersave else "off" }'
            return s

    class PanasonicBLEPacketAlarm(PanasonicBLEPacket):
        """Active alarm code response (cm 105, fn=27, dn=5).

        The alarm byte is the read payload at pdata[4].
        """

        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            self.alarm_code = decode_alarm_byte(pdata[4]) if len(pdata) >= 5 else None

    class PanasonicBLEPacketAlarmHistory(PanasonicBLEPacket):
        """Alarm-history response (cm 39). Alarm byte at pdata[2]."""

        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            self.alarm_code = decode_alarm_byte(pdata[2]) if len(pdata) >= 3 else None

    class PanasonicBLEPacketOutdoorTemp(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            self.temp = self.pdata[1] / 10 if len(self.pdata) >= 2 else None

        def __str__(self):
            s = super().__str__()
            s += f"\nOutdoor Temp: {self.temp}"
            return s

    class PanasonicBLEPacketConsumption(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            self.hour = None
            self.index = None
            self.pos = None
            self.values = None

            super().__init__(ptype, pdata)
            if pdata[3] == 1:
                self.hour = pdata[8]
            elif pdata[3] == 2:
                self.index = pdata[11]
            elif pdata[3] >= 3 and pdata[3] <= 14:
                # convert remaining data to two-byte floats
                self.pos = (pdata[3] - 3) * 4
                self.values = [
                    ((pdata[x + 4] << 8) + (pdata[x + 5] & 255)) / 10
                    for x in range(0, len(pdata) - 4, 2)
                ]

    class COMPONENT(Enum):
        I_UNIT1 = 1
        O_UNIT1 = 9
        ALL_UNITS = 247
        APP = 249
        BLE_MODULE_UART = 254

    class OPERATION(Enum):
        SET = 0
        SET_RES = 1
        REQ = 2
        REQ_RES = 3
        NOTIFY = 4

    def __init__(self, src=None, dst=None, op=None, packets=None):
        self.src = self.COMPONENT[src]
        self.dst = self.COMPONENT[dst]
        self.op = self.OPERATION[op]
        self.packets = packets

    @staticmethod
    def parse(data: bytes):
        """Construct PanasonicBLEParcel from fd."""

        data = _decode(data)
        fd = io.BytesIO(data)

        if fd.read(1)[0] != 0x11:
            raise ValueError("Bad packet")

        src = PanasonicBLEParcel.COMPONENT(fd.read(1)[0])
        dst = PanasonicBLEParcel.COMPONENT(fd.read(1)[0])
        op = PanasonicBLEParcel.OPERATION(fd.read(1)[0])

        num_packets = fd.read(1)[0]
        packets = [
            PanasonicBLEParcel.PanasonicBLEPacket.parse(fd) for _ in range(num_packets)
        ]

        return PanasonicBLEParcel(
            src=src.name, dst=dst.name, op=op.name, packets=packets
        )

    def encode(self):
        fd = io.BytesIO()
        fd.write(b"\x11")
        fd.write(self.src.value.to_bytes())
        fd.write(self.dst.value.to_bytes())
        fd.write(self.op.value.to_bytes())

        fd.write(len(self.packets).to_bytes())
        for p in self.packets:
            fd.write(p.encode())

        fd.write(b"\x00")  # cksum

        return _encode(fd.getvalue())

    def __str__(self):
        s = f"{self.src.name} => {self.dst.name} {self.op.name}\n"
        for p in self.packets:
            s += f"\t{p}\n"
        return s

    def __iter__(self):
        self.idx = 0
        return self

    def __next__(self):
        if self.idx >= len(self.packets):
            raise StopIteration

        pkt = self.packets[self.idx]
        self.idx += 1
        return pkt


class PanasonicBLEMode(PanasonicBLEParcel):
    def __init__(self, mode):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(66, bytes([mode]))],
        )


class PanasonicBLEFanMode(PanasonicBLEParcel):
    def __init__(self, mode):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(76, bytes([17, mode, 0, 0])),
                PanasonicBLEParcel.PanasonicBLEPacket(76, bytes([18, mode, 0, 0]))
            ],
        )


class PanasonicBLEEnergySaving(PanasonicBLEParcel):
    def __init__(self, state):
        state = 11 if state else 9
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(84, bytes([state]))],
        )


class PanasonicBLEPower(PanasonicBLEParcel):
    def __init__(self, state):
        state += 2  # 2==OFF, 3==ON
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(65, bytes([state]))],
        )


class PanasonicBLEStatusReq(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(129, bytes([4, 0, 14]))],
        )


class PanasonicBLEPowerReq(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([2, 0, 19, x, 12]))
                for x in range(1, 3)
            ],
        )


class PanasonicBLEPowerReqHour(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([2, 0, 19, x, 12]))
                for x in range(3, 15)
            ],
        )


class PanasonicBLETemp(PanasonicBLEParcel):
    def __init__(self, temp):
        temp = int(temp * 2 + 70)
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(76, bytes([9, 0, temp, 0])),
                PanasonicBLEParcel.PanasonicBLEPacket(76, bytes([10, 0, temp, 0]))
            ],
        )


class PanasonicBLEOuting(PanasonicBLEParcel):
    def __init__(self, state):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([0, 0, 17, 2, state]))
            ],
        )


class PanasonicBLEAlarmReq(PanasonicBLEParcel):
    """Request the active alarm code (cm 105 generic read, fn=27, dn=5)."""

    def __init__(self):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([2, 0, 27, 5, 5]))],
        )


class PanasonicBLEAlarmHistoryReq(PanasonicBLEParcel):
    """Request an alarm-history slot (cm 39) from the indoor unit.

    ``index`` selects the history slot (1 = most recent).
    """

    def __init__(self, index: int = 1):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(39, bytes([index, 3]))],
        )
