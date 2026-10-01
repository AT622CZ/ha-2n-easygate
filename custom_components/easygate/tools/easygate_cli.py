#!/usr/bin/env python3
"""Command line tool for testing the 2N EasyGate GSM gateway.

Needs only pyserial:  apk add python3 py3-pyserial   (or pip install pyserial)

    python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 info
    python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 at "AT+CPBS=?"
    python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 monitor          # calls, URCs, new SMS
    python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 sms-list         # does not delete
    python3 /config/custom_components/easygate/tools/easygate_cli.py /dev/ttyUSB1 sms-send +420777123456 "test"   # costs money!
"""

from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import logging
from pathlib import Path
import sys
import time

def _find(*parts: str) -> Path:
    """custom_components/... in this folder or any parent (e.g. /config)."""
    here = Path(__file__).resolve()
    for folder in (here.parent, *here.parents):
        candidate = folder.joinpath("custom_components", *parts)
        if candidate.exists():
            return candidate
    raise SystemExit("custom_components/" + "/".join(parts) + " not found")


_spec = importlib.util.spec_from_file_location("easygate_at", _find("easygate", "at.py"))
assert _spec and _spec.loader
at = importlib.util.module_from_spec(_spec)
sys.modules["easygate_at"] = at
_spec.loader.exec_module(at)


def _now() -> str:
    return f"{datetime.now():%H:%M:%S}"


def cmd_info(client, args) -> None:
    client.ping()
    for key, value in client.identity().items():
        print(f"{key:<12}: {value}")
    dbm, ber = client.signal()
    print(f"{'signal':<12}: {dbm} dBm (ber {ber})")
    print(f"{'registration':<12}: {client.registration()}")
    print(f"{'operator':<12}: {client.operator()}")
    for book in ("MC", "RC", "LD"):
        try:
            print(f"{'calls ' + book:<12}: {client.phonebook(book, 5)}")
        except at.EasyGateError as err:
            print(f"{'calls ' + book:<12}: {err}")


def cmd_at(client, args) -> None:
    for line in client.command(args.command, timeout=args.timeout):
        print(line)
    print("OK")


def cmd_monitor(client, args) -> None:
    last_calls = None
    seen: set[tuple] = set()
    print("Ctrl+C to stop. Call the gateway, pick up, hang up...")
    while True:
        try:
            for urc in client.poll_urcs():
                print(f"{_now()} URC  {urc}")
            calls = client.calls()
            if calls != last_calls:
                print(f"{_now()} CLCC {calls or 'none'}")
                last_calls = calls
            for sms in at.join_parts(client.list_sms()):
                key = (sms.number, sms.timestamp, sms.text)
                if key not in seen:  # messages stay on the SIM, show each once
                    seen.add(key)
                    print(f"{_now()} SMS  {sms.number} {sms.timestamp}: {sms.text}")
        except at.EasyGateBusy:
            print(f"{_now()} port blocked (dialling / ringing)")
        except at.EasyGateError as err:
            print(f"{_now()} error: {err}")
        time.sleep(args.interval)


def cmd_sms_list(client, args) -> None:
    for sms in at.join_parts(client.list_sms()):
        print(f"[{','.join(map(str, sms.indexes))}] {sms.number} {sms.timestamp}\n  {sms.text}")


def cmd_sms_send(client, args) -> None:
    text = at.strip_diacritics(args.text) if not args.keep_diacritics else args.text
    parts = client.send_sms(args.number, text, max_parts=args.max_parts)
    print(f"sent {parts} SMS")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    parser.add_argument("port")
    parser.add_argument("-b", "--baud", type=int, default=at.DEFAULT_BAUDRATE)
    parser.add_argument("--no-rtscts", action="store_true", help="disable RTS/CTS")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command_name", required=True)
    sub.add_parser("info").set_defaults(func=cmd_info)
    p = sub.add_parser("at")
    p.add_argument("command")
    p.add_argument("-t", "--timeout", type=float, default=10)
    p.set_defaults(func=cmd_at)
    p = sub.add_parser("monitor")
    p.add_argument("-i", "--interval", type=float, default=2)
    p.set_defaults(func=cmd_monitor)
    sub.add_parser("sms-list").set_defaults(func=cmd_sms_list)
    p = sub.add_parser("sms-send")
    p.add_argument("number")
    p.add_argument("text")
    p.add_argument("--max-parts", type=int, default=1)
    p.add_argument("--keep-diacritics", action="store_true")
    p.set_defaults(func=cmd_sms_send)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    client = at.EasyGateClient(args.port, baudrate=args.baud, rtscts=not args.no_rtscts)
    try:
        args.func(client, args)
    except at.EasyGateBusy as err:
        print(f"gateway blocks the port: {err}")
    except at.EasyGateError as err:
        print(f"error: {err}")
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


if __name__ == "__main__":
    main()
