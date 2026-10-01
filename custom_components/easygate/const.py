"""Constants for the 2N EasyGate integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "easygate"
MANUFACTURER = "2N"
MODEL = "EasyGate"

CONF_PORT = "port"
CONF_BAUDRATE = "baudrate"
CONF_RTSCTS = "rtscts"
CONF_RECIPIENTS = "recipients"
CONF_DAILY_LIMIT = "daily_limit"
CONF_MAX_PARTS = "max_parts"
CONF_STRIP_DIACRITICS = "strip_diacritics"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MISSED_CALLS = "missed_calls"
CONF_DELETE_SMS = "delete_sms"
CONF_COMMAND_NUMBERS = "command_numbers"
CONF_COMMAND_PIN = "command_pin"

BAUDRATES = [9600, 19200, 38400, 57600, 115200]
DEFAULT_BAUDRATE = 9600
DEFAULT_DAILY_LIMIT = 5
DEFAULT_MAX_PARTS = 1
DEFAULT_SCAN_INTERVAL = 10

# Signal / operator / missed calls change slowly.
STATUS_INTERVAL = 60
# Incoming SMS are checked this often (and right after a +CMTI notice).
SMS_INTERVAL = 30
# Parts of a long SMS may arrive minutes apart; incomplete ones wait this long.
PART_WAIT = timedelta(hours=1)
# The module stays silent while a call rings; only this long is an error.
SILENCE_TOLERANCE = 180
# Every received SMS is appended here (inside /config) before deletion.
ARCHIVE_FILE = "easygate_sms.txt"
# How long a send waits for the gateway to unblock the COM port.
SEND_RETRIES = 12
SEND_RETRY_DELAY = 5

EVENT_SMS = f"{DOMAIN}_sms"
EVENT_CALL = f"{DOMAIN}_call"
EVENT_MISSED_CALL = f"{DOMAIN}_missed_call"
EVENT_COMMAND = f"{DOMAIN}_command"

SERVICE_SEND_SMS = "send_sms"
SERVICE_AT_COMMAND = "at_command"

REGISTRATION_NAMES = {
    "not_registered": "neregistrováno",
    "home": "domácí síť",
    "searching": "hledá síť",
    "denied": "odmítnuto",
    "unknown": "neznámé",
    "roaming": "roaming",
}
