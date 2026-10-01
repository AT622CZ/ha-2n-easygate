"""AT command client for the 2N EasyGate GSM gateway (Siemens MC39i/MC55 inside).

No Home Assistant dependency, usable from ``easygate_cli.py``.

The gateway's own CPU shares the GSM module with the COM port.  While it
dials or an incoming call rings it blocks the port by dropping CTS, so the
port must be opened with RTS/CTS flow control and "busy" is a normal state.

The module state is never changed: SMS are handled in PDU mode (the module
default), no AT+CMGF / AT+CSCS / AT+CNMI / ATE, and the commands the manual
forbids are refused by :meth:`EasyGateClient.command`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import re
import threading
import time
from typing import Any
import unicodedata

_LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 9600

# Commands the EasyGate manual says must not be used (chapter 8.6).
FORBIDDEN_PREFIXES = (
    "ATQ1", "ATS3=", "ATS4=", "ATS5=", "ATV0", "AT&C0", "AT+IPR=", "AT+CFUN=",
    "AT+CMUT=", "AT+CMUX=", "AT+CSCS=", "AT^SAIC=", "AT^SM20=", "AT^SMSO=",
    "AT^SNFA=", "AT^SNFD=", "AT^SNFI=", "AT^SNFM=", "AT^SNFO=", "AT^SNFPT=",
    "AT^SNFS=", "AT^SNFV=", "AT^SNFW=",
)  # fmt: skip

_FINAL_OK = "OK"
_FINAL_ERRORS = ("ERROR", "+CME ERROR", "+CMS ERROR", "NO CARRIER")
_URC_PREFIXES = ("RING", "+CRING", "+CLIP", "+CMTI", "+CDSI", "+CREG:", "^S")


class EasyGateError(Exception):
    """Communication failed."""


class EasyGateBusy(EasyGateError):
    """The gateway blocks the COM port (dialling / ringing / not registered)."""


class EasyGateTimeout(EasyGateError):
    """No final result code."""


class EasyGateCommandError(EasyGateError):
    """The module answered ERROR."""


# --- GSM 03.38 alphabet ----------------------------------------------------

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\", 0x3C: "[",
            0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€"}  # fmt: skip
_GSM7_INDEX = {ch: i for i, ch in enumerate(GSM7_BASIC) if ch != "\x1b"}
_GSM7_EXT_INDEX = {ch: code for code, ch in GSM7_EXT.items()}


def strip_diacritics(text: str) -> str:
    """'Žluťoučký kůň' -> 'Zlutoucky kun' (keeps GSM characters like ä, é)."""
    out = []
    for ch in text:
        if ch in _GSM7_INDEX or ch in _GSM7_EXT_INDEX:
            out.append(ch)
            continue
        base = "".join(
            c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)
        )
        out.append(base)
    return "".join(out)


def gsm7_septets(text: str) -> list[int] | None:
    """Septets for ``text`` or None if it needs UCS2."""
    septets: list[int] = []
    for ch in text:
        if ch in _GSM7_INDEX:
            septets.append(_GSM7_INDEX[ch])
        elif ch in _GSM7_EXT_INDEX:
            septets += [0x1B, _GSM7_EXT_INDEX[ch]]
        else:
            return None
    return septets


def gsm7_decode(septets: list[int]) -> str:
    out = []
    escape = False
    for s in septets:
        if escape:
            out.append(GSM7_EXT.get(s, " "))
            escape = False
        elif s == 0x1B:
            escape = True
        else:
            out.append(GSM7_BASIC[s])
    return "".join(out)


def pack7(septets: list[int], fill_bits: int = 0) -> bytes:
    acc = 0
    nbits = fill_bits
    out = bytearray()
    for s in septets:
        acc |= s << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def unpack7(data: bytes, count: int, fill_bits: int = 0) -> list[int]:
    value = int.from_bytes(data, "little") >> fill_bits
    return [(value >> (7 * i)) & 0x7F for i in range(count)]


# --- addresses / timestamps ------------------------------------------------


def _encode_address(number: str) -> bytes:
    number = number.replace(" ", "")
    toa = 0x81
    if number.startswith("+"):
        toa = 0x91
        number = number[1:]
    elif number.startswith("00"):
        toa = 0x91
        number = number[2:]
    if not number or not re.fullmatch(r"[0-9*#]+", number):
        raise ValueError(f"invalid phone number {number!r}")
    digits = number.replace("*", "A").replace("#", "B")
    if len(digits) % 2:
        digits += "F"
    swapped = "".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2))
    return bytes((len(number), toa)) + bytes.fromhex(swapped)


def _decode_semi_octets(data: bytes) -> str:
    out = []
    for b in data:
        for nib in (b & 0x0F, b >> 4):
            if nib == 0xF:
                continue
            out.append("0123456789*#abc"[nib])
    return "".join(out)


def _decode_address(length: int, toa: int, data: bytes) -> str:
    if toa & 0x70 == 0x50:  # alphanumeric sender, e.g. "Vodafone"
        return gsm7_decode(unpack7(data, length * 4 // 7))
    number = _decode_semi_octets(data)[:length]
    return ("+" if toa & 0x70 == 0x10 else "") + number


def _decode_scts(data: bytes) -> datetime | None:
    try:
        d = [int(f"{b & 0x0F}{b >> 4}") for b in data[:6]]
        tz = data[6]
        quarters = int(f"{tz & 0x07}{tz >> 4}")
        sign = -1 if tz & 0x08 else 1
        return datetime(
            2000 + d[0], d[1], d[2], d[3], d[4], d[5],
            tzinfo=timezone(timedelta(minutes=sign * quarters * 15)),
        )  # fmt: skip
    except ValueError:
        return None


# --- PDU encoding ----------------------------------------------------------


def encode_sms(
    number: str, text: str, *, max_parts: int = 1, ref: int = 0
) -> list[tuple[str, int]]:
    """SMS-SUBMIT PDUs as (hex, length for AT+CMGS).

    Text that does not fit into ``max_parts`` messages is truncated.
    """
    address = _encode_address(number)
    septets = gsm7_septets(text)
    ucs2 = septets is None
    if ucs2:
        units = [text[i : i + 1] for i in range(len(text))]
        single, multi = 70, 67
    else:
        single, multi = 160, 153

    def _size(chunk: list[Any]) -> int:
        if ucs2:
            return sum(len(u.encode("utf-16-be")) // 2 for u in chunk)
        return sum(len(c) for c in chunk)

    items: list[Any] = units if ucs2 else _septet_chars(text)
    if _size(items) <= single or max_parts <= 1:
        parts = [_take(items, single, _size)]
    else:
        parts = []
        rest = items
        while rest and len(parts) < max_parts:
            chunk = _take(rest, multi, _size)
            parts.append(chunk)
            rest = rest[len(chunk) :]

    pdus = []
    total = len(parts)
    for seq, chunk in enumerate(parts, start=1):
        udh = bytes((5, 0, 3, ref & 0xFF, total, seq)) if total > 1 else b""
        if ucs2:
            payload = "".join(chunk).encode("utf-16-be")
            ud = udh + payload
            udl = len(ud)
            dcs = 0x08
        else:
            chunk_septets = [s for c in chunk for s in c]
            if udh:
                fill = (7 - (len(udh) * 8) % 7) % 7
                ud = udh + pack7(chunk_septets, fill)
                udl = (len(udh) * 8 + fill) // 7 + len(chunk_septets)
            else:
                ud = pack7(chunk_septets)
                udl = len(chunk_septets)
            dcs = 0x00
        first = 0x01 | (0x40 if udh else 0)
        tpdu = bytes((first, 0x00)) + address + bytes((0x00, dcs, udl)) + ud
        pdus.append(("00" + tpdu.hex().upper(), len(tpdu)))
    return pdus


def _septet_chars(text: str) -> list[list[int]]:
    """Septets grouped per character, so escapes are never split."""
    out = []
    for ch in text:
        out.append(gsm7_septets(ch) or [])
    return out


def _take(items: list[Any], limit: int, size: Callable[[list[Any]], int]) -> list[Any]:
    chunk: list[Any] = []
    for item in items:
        if size(chunk + [item]) > limit:
            break
        chunk.append(item)
    return chunk


# --- PDU decoding ----------------------------------------------------------


@dataclass
class SmsMessage:
    """A received SMS (or one part of a concatenated one)."""

    index: int | None
    number: str
    text: str
    timestamp: datetime | None
    concat: tuple[int, int, int] | None = None  # (ref, total, seq)
    indexes: list[int] = field(default_factory=list)
    storage: str = ""
    # (storage, index) of every part, parts may sit in different memories
    refs: list[tuple[str, int]] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        """A part of a concatenated SMS whose other parts are missing."""
        return self.concat is not None

    def as_event(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "text": self.text,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }

    def archive_line(self) -> str:
        when = self.timestamp.isoformat() if self.timestamp else "?"
        text = self.text.replace("\r", "").replace("\n", "\\n")
        return f"{when}\t{self.number}\t{text}\n"


def decode_user_data(dcs: int, udhi: bool, udl: int, ud: bytes) -> tuple[str, bytes]:
    """Return (text, user data header)."""
    header = b""
    if dcs & 0xC0 == 0x00:
        alphabet = (dcs >> 2) & 0x03
    elif dcs & 0xF0 == 0xF0:
        alphabet = 1 if dcs & 0x04 else 0
    elif dcs & 0xF0 == 0xE0:
        alphabet = 2
    else:
        alphabet = 0
    if alphabet == 0:
        fill = 0
        skip = 0
        if udhi:
            udhl = ud[0]
            header = ud[1 : 1 + udhl]
            bits = (udhl + 1) * 8
            fill = (7 - bits % 7) % 7
            skip = (bits + fill) // 7
            ud = ud[udhl + 1 :]
        return gsm7_decode(unpack7(ud, udl - skip, fill)), header
    if udhi:
        udhl = ud[0]
        header = ud[1 : 1 + udhl]
        ud = ud[udhl + 1 : udl]
        udl = len(ud)
    data = ud[:udl]
    if alphabet == 2:
        return data.decode("utf-16-be", errors="replace"), header
    return data.decode("latin-1"), header


def _concat_info(header: bytes) -> tuple[int, int, int] | None:
    i = 0
    while i + 1 < len(header):
        iei, length = header[i], header[i + 1]
        value = header[i + 2 : i + 2 + length]
        if iei == 0x00 and length == 3:
            return value[0], value[1], value[2]
        if iei == 0x08 and length == 4:
            return value[0] << 8 | value[1], value[2], value[3]
        i += 2 + length
    return None


def decode_deliver(pdu_hex: str, index: int | None = None) -> SmsMessage | None:
    """Decode an SMS-DELIVER PDU (with SMSC prefix); None for other types."""
    pdu = bytes.fromhex(pdu_hex)
    i = 1 + pdu[0]
    first = pdu[i]
    i += 1
    if first & 0x03 != 0x00:
        return None
    oa_len, oa_toa = pdu[i], pdu[i + 1]
    oa_bytes = (oa_len + 1) // 2
    number = _decode_address(oa_len, oa_toa, pdu[i + 2 : i + 2 + oa_bytes])
    i += 2 + oa_bytes
    dcs = pdu[i + 1]
    i += 2
    timestamp = _decode_scts(pdu[i : i + 7])
    i += 7
    udl = pdu[i]
    text, header = decode_user_data(dcs, bool(first & 0x40), udl, pdu[i + 1 :])
    return SmsMessage(
        index=index,
        number=number,
        text=text,
        timestamp=timestamp,
        concat=_concat_info(header) if header else None,
        indexes=[index] if index is not None else [],
    )


def join_parts(messages: list[SmsMessage]) -> list[SmsMessage]:
    """Merge complete concatenated messages; incomplete parts stay separate."""
    singles = [m for m in messages if m.concat is None]
    groups: dict[tuple[str, int, int], list[SmsMessage]] = {}
    for m in messages:
        if m.concat is not None:
            ref, total, _ = m.concat
            groups.setdefault((m.number, ref, total), []).append(m)
    for (number, _ref, total), parts in groups.items():
        parts.sort(key=lambda m: m.concat[2] if m.concat else 0)
        if len({p.concat[2] for p in parts if p.concat}) == total:
            singles.append(
                SmsMessage(
                    index=None,
                    number=number,
                    text="".join(p.text for p in parts),
                    timestamp=parts[0].timestamp,
                    indexes=[i for p in parts for i in p.indexes],
                    storage=parts[0].storage,
                    refs=[ref for p in parts for ref in p.refs],
                )
            )
        else:
            singles.extend(parts)
    singles.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
    return singles


# --- parsed responses ------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One line of AT+CLCC."""

    id: int
    incoming: bool
    state: int  # 0 active, 1 held, 2 dialing, 3 alerting, 4 incoming, 5 waiting
    number: str

    STATES = ("active", "held", "dialing", "alerting", "incoming", "waiting")

    @property
    def state_name(self) -> str:
        return self.STATES[self.state] if 0 <= self.state < len(self.STATES) else "unknown"


_CLCC = re.compile(r'\+CLCC:\s*(\d+),(\d),(\d),(\d),(\d)(?:,"([^"]*)",(\d+))?')
_CPBR = re.compile(r'\+CPBR:\s*(\d+),"([^"]*)",(\d+)(?:,"([^"]*)")?')
_CLIP = re.compile(r'\+CLIP:\s*"([^"]*)"')


def parse_clip(line: str) -> str | None:
    m = _CLIP.match(line)
    return m.group(1) if m else None


def is_ring(line: str) -> bool:
    """RING, sometimes cut to 'RIN' when the gateway takes the port over."""
    return line.startswith(("RIN", "+CRING"))


# Storages holding received SMS: phone memory and SIM.
SMS_STORAGES = ("ME", "SM")


OPERATORS = {
    "23001": "T-Mobile CZ",
    "23002": "O2 CZ",
    "23003": "Vodafone CZ",
    "23004": "Nordic Telecom",
    "23102": "Telekom SK",
    "23105": "Orange SK",
    "23106": "O2 SK",
}

REGISTRATION = {
    0: "not_registered",
    1: "home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "roaming",
}


# --- client ----------------------------------------------------------------


class EasyGateClient:
    """Blocking AT client; use from an executor thread."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        rtscts: bool = True,
        serial_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.rtscts = rtscts
        self._serial_factory = serial_factory or self._open_serial
        self._ser: Any = None
        self._buffer = b""
        self.lock = threading.RLock()
        self.urcs: list[str] = []

    def _open_serial(self) -> Any:
        import serial  # pylint: disable=import-outside-toplevel

        ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=2,
            rtscts=self.rtscts,
            exclusive=True,
        )
        ser.dtr = True
        return ser

    def _ensure_open(self) -> None:
        if self._ser is None:
            try:
                self._ser = self._serial_factory()
            except Exception as err:  # serial.SerialException, OSError
                raise EasyGateError(f"cannot open {self.port}: {err}") from err
            self._buffer = b""

    def close(self) -> None:
        with self.lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # pylint: disable=broad-except
                    pass
                self._ser = None

    # -- low level --

    def _write(self, data: bytes) -> None:
        if self.rtscts and not getattr(self._ser, "cts", True):
            raise EasyGateBusy("COM port blocked by the gateway (CTS off)")
        _LOGGER.debug("tx %r", data)
        try:
            self._ser.write(data)
        except Exception as err:  # SerialTimeoutException when CTS stays off
            if type(err).__name__ == "SerialTimeoutException":
                raise EasyGateBusy("COM port blocked by the gateway") from err
            self.close()
            raise EasyGateError(f"serial error: {err}") from err

    def _read_some(self) -> bytes:
        try:
            data = self._ser.read(max(1, getattr(self._ser, "in_waiting", 0) or 1))
        except Exception as err:  # serial.SerialException, OSError
            self.close()
            raise EasyGateError(f"serial error: {err}") from err
        if data:
            _LOGGER.debug("rx %r", data)
        return data

    def _read_line(self, deadline: float) -> str | None:
        """Next non-empty line, or '>' for the SMS prompt; None on timeout."""
        while True:
            while b"\n" in self._buffer or self._buffer.lstrip(b"\r\n").startswith(b">"):
                stripped = self._buffer.lstrip(b"\r\n")
                if stripped.startswith(b">"):
                    self._buffer = stripped[1:].lstrip(b" ")
                    return ">"
                line, _, self._buffer = self._buffer.partition(b"\n")
                text = line.decode("latin-1").strip()
                if text:
                    return text
            if time.monotonic() > deadline:
                return None
            self._buffer += self._read_some()

    def poll_urcs(self) -> list[str]:
        """Collect unsolicited lines that arrived between commands."""
        with self.lock:
            self._ensure_open()
            self._buffer += self._read_some()
            deadline = time.monotonic()
            while (line := self._read_line(deadline)) is not None:
                if line != ">":
                    self.urcs.append(line)
            urcs, self.urcs = self.urcs, []
            return urcs

    def command(self, cmd: str, timeout: float = 5.0) -> list[str]:
        """Send one AT command; return the information lines."""
        upper = cmd.upper()
        if upper.startswith(FORBIDDEN_PREFIXES):
            raise EasyGateCommandError(f"{cmd} is forbidden by the EasyGate manual")
        with self.lock:
            self._ensure_open()
            self._stash_pending()
            self._write(cmd.encode("ascii") + b"\r")
            return self._collect(cmd, time.monotonic() + timeout)

    def _stash_pending(self) -> None:
        """Move leftovers (URCs, tails of aborted answers) out of the way."""
        if getattr(self._ser, "in_waiting", 0):
            self._buffer += self._read_some()
        deadline = time.monotonic()
        while (line := self._read_line(deadline)) is not None:
            if line != ">":
                self.urcs.append(line)
        # Nothing of ours is in flight yet, so a partial line is stale too.
        if tail := self._buffer.decode("latin-1").strip():
            self.urcs.append(tail)
        self._buffer = b""

    def _collect(self, cmd: str, deadline: float) -> list[str]:
        lines: list[str] = []
        while True:
            line = self._read_line(deadline)
            if line is None:
                raise EasyGateTimeout(f"no answer to {cmd}")
            if line == cmd or line == ">":
                continue  # echo
            if line == _FINAL_OK:
                return lines
            if line.startswith(_FINAL_ERRORS):
                raise EasyGateCommandError(f"{cmd}: {line}")
            if is_ring(line) or (
                line.startswith(_URC_PREFIXES) and not line.startswith(_info_prefix(cmd))
            ):
                self.urcs.append(line)
                continue
            lines.append(line)

    # -- high level --

    def ping(self) -> None:
        self.command("AT", timeout=2)

    def identity(self) -> dict[str, str]:
        info = {}
        for key, cmd in (("model", "AT+CGMM"), ("imei", "AT+CGSN"), ("revision", "AT+CGMR")):
            try:
                lines = self.command(cmd)
                info[key] = lines[0] if lines else ""
            except EasyGateCommandError:
                pass
        return info

    def signal(self) -> tuple[int | None, int | None]:
        """(dBm, bit error rate); None when unknown."""
        for line in self.command("AT+CSQ"):
            m = re.match(r"\+CSQ:\s*(\d+),(\d+)", line)
            if m:
                rssi, ber = int(m.group(1)), int(m.group(2))
                return (None if rssi == 99 else -113 + 2 * rssi), (None if ber == 99 else ber)
        return None, None

    def registration(self) -> str:
        for line in self.command("AT+CREG?"):
            m = re.match(r"\+CREG:\s*\d+,(\d+)", line)
            if m:
                return REGISTRATION.get(int(m.group(1)), "unknown")
        return "unknown"

    def operator(self) -> str | None:
        for line in self.command("AT+COPS?", timeout=10):
            m = re.match(r'\+COPS:\s*\d+(?:,\d+,"([^"]*)")?', line)
            if m:
                return OPERATORS.get(m.group(1) or "", m.group(1))
        return None

    def calls(self) -> list[Call]:
        out = []
        for line in self.command("AT+CLCC"):
            m = _CLCC.match(line)
            if m:
                out.append(
                    Call(int(m.group(1)), m.group(2) == "1", int(m.group(3)), m.group(6) or "")
                )
        return out

    def sms_storage(self) -> tuple[str, int, int]:
        """(selected read storage, used, total of the receive storage)."""
        for line in self.command("AT+CPMS?", timeout=15):
            if line.startswith("+CPMS:"):
                mems = re.findall(r'"([^"]+)",(\d+),(\d+)', line)
                if mems:
                    return mems[0][0], int(mems[-1][1]), int(mems[-1][2])
        raise EasyGateCommandError("unexpected AT+CPMS? answer")

    def select_sms_storage(self, storage: str) -> None:
        """Select the read/delete storage (mem1) only."""
        self.command(f'AT+CPMS="{storage}"', timeout=15)

    def list_sms(self, storage: str = "") -> list[SmsMessage]:
        """All messages in the currently selected read storage (PDU mode)."""
        lines = self.command("AT+CMGL=4", timeout=30)
        messages = []
        index: int | None = None
        for line in lines:
            m = re.match(r"\+CMGL:\s*(\d+),", line)
            if m:
                index = int(m.group(1))
                continue
            if index is not None and re.fullmatch(r"[0-9A-Fa-f]+", line):
                try:
                    msg = decode_deliver(line, index)
                    if msg is not None:
                        msg.storage = storage
                        msg.refs = [(storage, index)]
                except (IndexError, ValueError) as err:
                    _LOGGER.warning("cannot decode SMS %s: %s", index, err)
                    msg = None
                if msg is not None:
                    messages.append(msg)
                index = None
        return messages

    def delete_sms(self, index: int) -> None:
        self.command(f"AT+CMGD={index}")

    def send_pdu(self, pdu: str, length: int) -> None:
        with self.lock:
            self._ensure_open()
            self._stash_pending()
            cmd = f"AT+CMGS={length}"
            self._write(cmd.encode("ascii") + b"\r")
            deadline = time.monotonic() + 10
            while True:
                line = self._read_line(deadline)
                if line is None:
                    raise EasyGateTimeout("no SMS prompt")
                if line == ">":
                    break
                if line.startswith(_FINAL_ERRORS):
                    raise EasyGateCommandError(f"{cmd}: {line}")
            self._write(pdu.encode("ascii") + b"\x1a")
            self._collect(cmd, time.monotonic() + 60)

    def send_sms(self, number: str, text: str, *, max_parts: int = 1, ref: int = 0) -> int:
        """Send a text; returns the number of SMS parts sent."""
        pdus = encode_sms(number, text, max_parts=max_parts, ref=ref)
        for pdu, length in pdus:
            self.send_pdu(pdu, length)
        return len(pdus)

    def phonebook(self, storage: str, count: int = 10) -> list[str]:
        """Numbers from a call list: "MC" missed, "RC" received, "LD" dialled."""
        with self.lock:
            previous = None
            for line in self.command("AT+CPBS?", timeout=15):
                m = re.match(r'\+CPBS:\s*"([^"]+)"', line)
                if m:
                    previous = m.group(1)
            self.command(f'AT+CPBS="{storage}"', timeout=15)
            try:
                numbers = []
                try:
                    lines = self.command(f"AT+CPBR=1,{count}", timeout=20)
                except EasyGateCommandError:
                    lines = []  # empty list answers ERROR on some firmwares
                for line in lines:
                    m = _CPBR.match(line)
                    if m:
                        numbers.append(m.group(2))
                return numbers
            finally:
                if previous and previous != storage:
                    self.command(f'AT+CPBS="{previous}"', timeout=15)

    def call_lists(
        self, storages: tuple[str, ...] = ("MC", "RC"), count: int = 5
    ) -> dict[str, tuple[int, list[str]]]:
        """{storage: (entries used, newest numbers first)}; restores the selection."""
        result: dict[str, tuple[int, list[str]]] = {}
        with self.lock:
            previous = _selected_phonebook(self.command("AT+CPBS?", timeout=15))
            try:
                for storage in storages:
                    self.command(f'AT+CPBS="{storage}"', timeout=15)
                    used = 0
                    for line in self.command("AT+CPBS?", timeout=15):
                        m = re.match(r'\+CPBS:\s*"[^"]+",(\d+)', line)
                        if m:
                            used = int(m.group(1))
                    numbers = []
                    if used:
                        try:
                            lines = self.command(f"AT+CPBR=1,{count}", timeout=20)
                        except EasyGateCommandError:
                            lines = []
                        numbers = [m.group(2) for m in map(_CPBR.match, lines) if m]
                    result[storage] = (used, numbers)
            finally:
                if previous:
                    self.command(f'AT+CPBS="{previous}"', timeout=15)
        return result


def _selected_phonebook(lines: list[str]) -> str | None:
    for line in lines:
        m = re.match(r'\+CPBS:\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return None


def _info_prefix(cmd: str) -> str:
    m = re.match(r"AT([+^][A-Z]+)", cmd.upper())
    return m.group(1) + ":" if m else "\0"
