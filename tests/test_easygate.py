"""EasyGate AT client tests against a simulated Siemens module.

Run:  python3 -m pytest tests
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

def _find(*parts: str) -> Path:
    """custom_components/... in this folder or any parent (e.g. /config)."""
    here = Path(__file__).resolve()
    for folder in (here.parent, *here.parents):
        candidate = folder.joinpath("custom_components", *parts)
        if candidate.exists():
            return candidate
    raise SystemExit("custom_components/" + "/".join(parts) + " not found")


_spec = importlib.util.spec_from_file_location("easygate_at", _find("easygate", "at.py"))
at = importlib.util.module_from_spec(_spec)
sys.modules["easygate_at"] = at
_spec.loader.exec_module(at)


def _deliver(number_field: str, dcs: int, scts: str, udl: int, ud: bytes, first=0x04):
    return f"00{first:02X}{number_field}00{dcs:02X}{scts}{udl:02X}{ud.hex().upper()}"


# +420123456789, 26-09-29 10:00:00 +02:00
OA = "0C91" + "241032547698"
SCTS = "62909201000080"


class FakeModem:
    """Serial-like Siemens module: echo on, PDU mode."""

    def __init__(self) -> None:
        self.out = bytearray()
        self.inbox: dict[int, str] = {}
        self.sent: list[str] = []
        self.calls: list[str] = []
        self.phonebooks = {"SM": [], "MC": ['"+420777111222",145,""']}
        self.pbs = "SM"
        self.cpms = "ME"
        self.storage_inbox: dict[str, dict[int, str]] = {"ME": self.inbox, "SM": {}}
        self.cts = True
        self._pending_cmgs: int | None = None
        self._line = b""

    @property
    def in_waiting(self) -> int:
        return len(self.out)

    def read(self, n: int) -> bytes:
        data, self.out = bytes(self.out[:n]), self.out[n:]
        return data

    def close(self) -> None:
        pass

    def _reply(self, *lines: str) -> None:
        for line in lines:
            self.out += b"\r\n" + line.encode("latin-1") + b"\r\n"

    def write(self, data: bytes) -> None:
        for b in data:
            if self._pending_cmgs is not None:
                if b == 0x1A:
                    self.sent.append(self._line.decode())
                    self._line = b""
                    self._pending_cmgs = None
                    self._reply(f"+CMGS: {len(self.sent)}", "OK")
                else:
                    self._line += bytes((b,))
                continue
            if b == 0x0D:
                cmd = self._line.decode()
                self._line = b""
                self.out += cmd.encode() + b"\r"  # echo
                self._handle(cmd)
            else:
                self._line += bytes((b,))

    def _handle(self, cmd: str) -> None:
        if cmd == "AT":
            self._reply("OK")
        elif cmd == "AT+CSQ":
            self._reply("+CSQ: 20,99", "OK")
        elif cmd == "AT+CREG?":
            self._reply("+CREG: 0,1", "OK")
        elif cmd == "AT+COPS?":
            self._reply('+COPS: 0,2,"23003"', "OK")
        elif cmd == "AT+CLCC":
            self._reply(*self.calls, "OK")
        elif cmd == "AT+CPMS?":
            me, sm = len(self.storage_inbox["ME"]), len(self.storage_inbox["SM"])
            self._reply(f'+CPMS: "{self.cpms}",{me},25,"ME",{me},25,"MT",{me + sm},45', "OK")
        elif cmd.startswith('AT+CPMS="'):
            self.cpms = cmd[9:-1]
            self._reply("+CPMS: 0,25,0,25,0,45", "OK")
        elif cmd == "AT+CMGL=4":
            lines = []
            for idx, pdu in sorted(self.storage_inbox[self.cpms].items()):
                lines += [f"+CMGL: {idx},0,,{len(pdu) // 2 - 1}", pdu]
            self._reply(*lines, "OK")
        elif cmd.startswith("AT+CMGD="):
            self.storage_inbox[self.cpms].pop(int(cmd[8:]), None)
            self._reply("OK")
        elif cmd.startswith("AT+CMGS="):
            self._pending_cmgs = int(cmd[8:])
            self.out += b"\r\n> "
        elif cmd == "AT+CPBS?":
            self._reply(f'+CPBS: "{self.pbs}",1,250', "OK")
        elif cmd.startswith('AT+CPBS="'):
            self.pbs = cmd[9:-1]
            self._reply("OK")
        elif cmd.startswith("AT+CPBR="):
            entries = self.phonebooks.get(self.pbs, [])
            self._reply(*[f"+CPBR: {i + 1},{e}" for i, e in enumerate(entries)], "OK")
        else:
            self._reply("ERROR")


@pytest.fixture
def modem():
    return FakeModem()


@pytest.fixture
def client(modem):
    return at.EasyGateClient("fake", serial_factory=lambda: modem)


def test_alphabet_size():
    assert len(at.GSM7_BASIC) == 128


def test_encode_reference_pdu():
    # Classic example: "hellohello" to +46708251358
    pdus = at.encode_sms("+46708251358", "hellohello")
    assert pdus == [("0001000B916407281553F800000AE8329BFD4697D9EC37", 22)]


def test_decode_7bit():
    ud = at.pack7(at.gsm7_septets("hellohello"))
    msg = at.decode_deliver(_deliver(OA, 0x00, SCTS, 10, ud))
    assert msg.number == "+420123456789"
    assert msg.text == "hellohello"
    assert msg.timestamp == datetime(2026, 9, 29, 10, 0, tzinfo=timezone(timedelta(hours=2)))


def test_decode_ucs2_czech():
    text = "Žluťoučký kůň"
    ud = text.encode("utf-16-be")
    msg = at.decode_deliver(_deliver(OA, 0x08, SCTS, len(ud), ud))
    assert msg.text == text


def test_decode_alphanumeric_sender():
    name = at.pack7(at.gsm7_septets("Vodafone"))
    oa = f"{len(name) * 2:02X}D0" + name.hex().upper()
    ud = at.pack7(at.gsm7_septets("Kredit 100"))
    msg = at.decode_deliver(_deliver(oa, 0x00, SCTS, 10, ud))
    assert msg.number == "Vodafone"
    assert msg.text == "Kredit 100"


def test_strip_diacritics_fits_gsm():
    plain = at.strip_diacritics("Výpadek proudu v kotelně!")
    assert plain == "Vypadek proudu v kotelne!"
    assert at.gsm7_septets(plain) is not None


def _submit_user_data(pdu_hex: str) -> tuple[int, bool, int, bytes]:
    pdu = bytes.fromhex(pdu_hex)[1:]  # skip empty SMSC
    first = pdu[0]
    da_len = pdu[2]
    i = 4 + (da_len + 1) // 2
    dcs, udl = pdu[i + 1], pdu[i + 2]
    return dcs, bool(first & 0x40), udl, pdu[i + 3 :]


@pytest.mark.parametrize("text", ["A" * 200 + "{}€", "Ř" * 100])
def test_multipart_roundtrip(text):
    pdus = at.encode_sms("+420123456789", text, max_parts=3, ref=7)
    assert len(pdus) == 2
    parts = []
    for pdu, length in pdus:
        assert length == len(pdu) // 2 - 1
        dcs, udhi, udl, ud = _submit_user_data(pdu)
        assert udhi
        part, header = at.decode_user_data(dcs, udhi, udl, ud)
        assert header[:3] == bytes((0, 3, 7))
        parts.append(part)
    assert "".join(parts) == text


def test_single_part_truncates():
    pdus = at.encode_sms("+420123456789", "x" * 300)
    assert len(pdus) == 1
    dcs, udhi, udl, ud = _submit_user_data(pdus[0][0])
    assert not udhi and udl == 160


def test_status_commands(client):
    assert client.signal() == (-73, None)
    assert client.registration() == "home"
    assert client.operator() == "Vodafone CZ"


def test_send_sms(client, modem):
    assert client.send_sms("+46708251358", "hellohello") == 1
    assert modem.sent == ["0001000B916407281553F800000AE8329BFD4697D9EC37"]


def test_receive_join_and_delete(client, modem):
    one = at.pack7(at.gsm7_septets("hello"))
    modem.inbox[1] = _deliver(OA, 0x00, SCTS, 5, one)
    # two-part message, arriving out of order
    for idx, (seq, chunk) in zip((3, 2), ((2, "world"), (1, "hello "))):
        udh = bytes((5, 0, 3, 9, 2, seq))
        ud = udh + at.pack7(at.gsm7_septets(chunk), 1)
        modem.inbox[idx] = _deliver(OA, 0x00, SCTS, 7 + len(chunk), ud, first=0x44)
    messages = at.join_parts(client.list_sms())
    texts = sorted(m.text for m in messages)
    assert texts == ["hello", "hello world"]
    for m in messages:
        for idx in m.indexes:
            client.delete_sms(idx)
    assert modem.inbox == {}


def test_calls_and_urcs(client, modem):
    modem.calls = ['+CLCC: 1,1,0,0,0,"+420777111222",145']
    modem.out += b'\r\nRING\r\n\r\n+CLIP: "+420777111222",145\r\n'
    assert [at.parse_clip(u) for u in client.poll_urcs()] == [None, "+420777111222"]
    calls = client.calls()
    assert calls[0].incoming and calls[0].state_name == "active"
    assert calls[0].number == "+420777111222"


def test_phonebook_restores_storage(client, modem):
    assert client.phonebook("MC") == ["+420777111222"]
    assert modem.pbs == "SM"


def test_busy_and_forbidden(client, modem):
    modem.cts = False
    with pytest.raises(at.EasyGateBusy):
        client.ping()
    modem.cts = True
    with pytest.raises(at.EasyGateCommandError):
        client.command("AT+CFUN=0")


def test_both_storages_and_usage(client, modem):
    one = at.pack7(at.gsm7_septets("hello"))
    modem.storage_inbox["SM"][4] = _deliver(OA, 0x00, SCTS, 5, one)
    assert client.sms_storage() == ("ME", 1, 45)
    found = []
    for storage in at.SMS_STORAGES:
        client.select_sms_storage(storage)
        for m in client.list_sms(storage):
            found.append((m.storage, m.indexes, m.text))
            for idx in m.indexes:
                client.delete_sms(idx)
    client.select_sms_storage("ME")
    assert found == [("SM", [4], "hello")]
    assert client.sms_storage() == ("ME", 0, 45)


def test_leftovers_are_stashed(client, modem):
    modem.out += b"\r\n3DEC4EBB40D637396C7EBBCB\r\n\r\nRIN"
    assert client.signal() == (-73, None)
    assert "3DEC4EBB40D637396C7EBBCB" in client.urcs
    assert "RIN" in client.urcs
    assert at.is_ring("RIN") and at.is_ring("RING") and not at.is_ring("OK")


def test_call_lists_restore_selection(client, modem):
    modem.phonebooks["RC"] = []
    lists = client.call_lists()
    assert lists["MC"] == (1, ["+420777111222"])
    assert lists["RC"] == (1, [])  # fake reports 1 used, no entries
    assert modem.pbs == "SM"


def test_ring_during_command_is_kept(client, modem):
    # RIN arriving in the middle of an answer must not end up as data
    modem.calls = ["RIN"]
    assert client.calls() == []
    assert "RIN" in client.urcs


def test_join_across_storages(client, modem):
    for storage, idx, seq, chunk in (("SM", 7, 2, "vyuctovani"), ("ME", 3, 1, "Dobry den, ")):
        udh = bytes((5, 0, 3, 42, 2, seq))
        ud = udh + at.pack7(at.gsm7_septets(chunk), 1)
        modem.storage_inbox[storage][idx] = _deliver(
            OA, 0x00, SCTS, 7 + len(chunk), ud, first=0x44
        )
    parts = []
    for storage in at.SMS_STORAGES:
        client.select_sms_storage(storage)
        parts += client.list_sms(storage)
    [message] = at.join_parts(parts)
    assert message.text == "Dobry den, vyuctovani" and not message.incomplete
    assert sorted(message.refs) == [("ME", 3), ("SM", 7)]
