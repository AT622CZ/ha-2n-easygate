"""Polling coordinator for the 2N EasyGate GSM gateway."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
import os
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .at import (
    SMS_STORAGES,
    Call,
    EasyGateBusy,
    EasyGateClient,
    EasyGateError,
    EasyGateTimeout,
    SmsMessage,
    encode_sms,
    is_ring,
    join_parts,
    parse_clip,
    strip_diacritics,
)
from .const import (
    ARCHIVE_FILE,
    CONF_DAILY_LIMIT,
    CONF_DELETE_SMS,
    CONF_MAX_PARTS,
    CONF_COMMAND_NUMBERS,
    CONF_COMMAND_PIN,
    CONF_MISSED_CALLS,
    CONF_RECIPIENTS,
    CONF_SCAN_INTERVAL,
    CONF_STRIP_DIACRITICS,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MAX_PARTS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_CALL,
    EVENT_COMMAND,
    EVENT_MISSED_CALL,
    EVENT_SMS,
    SEND_RETRIES,
    SEND_RETRY_DELAY,
    SILENCE_TOLERANCE,
    SMS_INTERVAL,
    PART_WAIT,
    STATUS_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


@dataclass
class EasyGateData:
    """Snapshot shown by the entities."""

    busy: bool = False
    ringing: bool = False
    signal_dbm: int | None = None
    bit_error_rate: int | None = None
    registration: str | None = None
    operator: str | None = None
    calls: list[Call] = field(default_factory=list)
    last_sms: SmsMessage | None = None
    last_missed: str | None = None
    last_missed_at: datetime | None = None
    last_caller: str | None = None
    sms_sent_today: int = 0
    sms_stored: int | None = None
    sms_capacity: int | None = None


@dataclass
class _Poll:
    busy: bool = False
    urcs: list[str] = field(default_factory=list)
    identity: dict[str, str] | None = None
    calls: list[Call] | None = None
    status: tuple[int | None, int | None, str, str | None] | None = None
    sms: list[SmsMessage] = field(default_factory=list)
    sms_usage: tuple[int, int] | None = None
    lists: dict[str, tuple[int, list[str]]] | None = None
    baseline: bool = False
    ringing: bool = False
    episode_end: bool = False


def split_recipients(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    return [v.strip() for v in value if v and v.strip()]


class EasyGateCoordinator(DataUpdateCoordinator[EasyGateData]):
    """Reads status, calls and incoming SMS; sends SMS with a daily limit."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: EasyGateClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.identity: dict[str, str] = {}
        self._state = EasyGateData()
        self._archive_path = hass.config.path(ARCHIVE_FILE)
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        # executor-side timers (monotonic seconds)
        self._last_status = -STATUS_INTERVAL
        self._last_sms = -SMS_INTERVAL
        self._last_ok = time.monotonic()
        self._in_episode = False
        self._episode_ring = False
        self._need_baseline = True
        self._sms_due = True
        # main-loop state
        self._lists: dict[str, tuple[int, list[str]]] = {}
        self._calls_seen: dict[int, str] = {}
        self._sent_date = ""
        self._last_sms_ts: datetime | None = None
        self._since: datetime | None = None
        self._sms_ref = 0

    @property
    def signal(self) -> str:
        """Dispatcher signal carrying (event type, data) for the event entities."""
        return f"{DOMAIN}_{self.config_entry.entry_id}_event"

    def fire(self, event_type: str, data: dict) -> None:
        self.hass.bus.async_fire(event_type, data)
        async_dispatcher_send(self.hass, self.signal, event_type, data)

    # -- options --

    @property
    def recipients(self) -> list[str]:
        return split_recipients(self.config_entry.options.get(CONF_RECIPIENTS))

    @property
    def daily_limit(self) -> int:
        return int(self.config_entry.options.get(CONF_DAILY_LIMIT, DEFAULT_DAILY_LIMIT))

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._sent_date = stored.get("date", "")
        self._state.sms_sent_today = stored.get("count", 0)
        if ts := stored.get("last_sms_ts"):
            self._last_sms_ts = dt_util.parse_datetime(ts)
        if ts := stored.get("since"):
            self._since = dt_util.parse_datetime(ts)
        if self._last_sms_ts is None:
            # First start: messages already on the SIM are archived, not announced.
            self._last_sms_ts = dt_util.now()
        if self._since is None:
            self._since = self._last_sms_ts
            await self._save()
        self._roll_day()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "date": self._sent_date,
                "count": self._state.sms_sent_today,
                "last_sms_ts": self._last_sms_ts.isoformat() if self._last_sms_ts else None,
                "since": self._since.isoformat() if self._since else None,
            }
        )

    def _roll_day(self) -> None:
        today = dt_util.now().date().isoformat()
        if self._sent_date != today:
            self._sent_date = today
            self._state.sms_sent_today = 0

    # -- polling (executor) --

    def _archive(self, messages: list[SmsMessage]) -> None:
        os.makedirs(os.path.dirname(self._archive_path), exist_ok=True)
        with open(self._archive_path, "a", encoding="utf-8") as file:
            for message in messages:
                file.write(message.archive_line())

    def _collect_sms(self, res: _Poll) -> None:
        client = self.client
        delete = self.config_entry.options.get(CONF_DELETE_SMS, True)
        selected, used, total = client.sms_storage()
        try:
            parts: list[SmsMessage] = []
            for storage in SMS_STORAGES:
                client.select_sms_storage(storage)
                parts += client.list_sms(storage)
            # Join across both memories; parts of one SMS can land in each.
            messages = join_parts(parts)
            cutoff = dt_util.now() - PART_WAIT
            ready = [
                m
                for m in messages
                if not (m.incomplete and m.timestamp and m.timestamp > cutoff)
            ]  # a missing part may still arrive, keep waiting parts on the SIM
            if ready:
                self._archive(ready)  # always before deleting
                if delete:
                    for storage in SMS_STORAGES:
                        indexes = [i for m in ready for s, i in m.refs if s == storage]
                        if indexes:
                            client.select_sms_storage(storage)
                            for index in indexes:
                                client.delete_sms(index)
                res.sms += ready
        finally:
            client.select_sms_storage(selected)
        if res.sms and delete:
            _, used, total = client.sms_storage()
        res.sms_usage = (used, total)

    def _poll(self) -> _Poll:
        client = self.client
        res = _Poll()
        options = self.config_entry.options
        now = time.monotonic()
        try:
            with client.lock:
                res.urcs = client.poll_urcs()
                ring_now = any(is_ring(u) for u in res.urcs)
                if ring_now:
                    self._in_episode = self._episode_ring = True
                if any(u.startswith("+CMTI") for u in res.urcs):
                    self._sms_due = True
                if not self.identity:
                    res.identity = client.identity()
                res.calls = client.calls()
                if res.calls:
                    self._in_episode = True
                if now - self._last_status >= STATUS_INTERVAL:
                    dbm, ber = client.signal()
                    res.status = (dbm, ber, client.registration(), client.operator())
                    self._last_status = now
                if self._sms_due or now - self._last_sms >= SMS_INTERVAL:
                    self._collect_sms(res)
                    self._last_sms = now
                    self._sms_due = False
                lists_wanted = options.get(CONF_MISSED_CALLS, True)
                # A call "episode" ends with the first quiet poll after it.
                if self._in_episode and not res.calls and not ring_now:
                    if self._episode_ring and lists_wanted:
                        res.lists = client.call_lists()
                        res.episode_end = True
                    self._in_episode = self._episode_ring = False
                elif self._need_baseline and lists_wanted:
                    res.lists = client.call_lists()
                    res.baseline = True
                    self._need_baseline = False
            self._last_ok = time.monotonic()
        except EasyGateBusy:
            res.busy = True
            self._in_episode = True
        except EasyGateTimeout:
            # The gateway's CPU owns the module while a call rings or is dialled.
            if time.monotonic() - self._last_ok > SILENCE_TOLERANCE:
                raise
            res.busy = True
            self._in_episode = True
        res.ringing = self._episode_ring
        return res

    # -- main loop --

    async def _async_update_data(self) -> EasyGateData:
        try:
            res = await self.hass.async_add_executor_job(self._poll)
        except EasyGateError as err:
            raise UpdateFailed(f"EasyGate: {err}") from err

        state = self._state
        state.busy = res.busy
        state.ringing = res.ringing
        if res.identity:
            self.identity = res.identity
        rang = False
        for urc in res.urcs:
            number = parse_clip(urc)
            if (is_ring(urc) and not rang) or number:
                rang = True
                if number:
                    state.last_caller = number
                self.fire(
                    EVENT_CALL,
                    {"direction": "incoming", "state": "ringing", "number": number},
                )
        if res.calls is not None:
            self._handle_calls(res.calls)
        if res.status is not None:
            state.signal_dbm, state.bit_error_rate, state.registration, state.operator = (
                res.status
            )
        if res.sms_usage is not None:
            state.sms_stored, state.sms_capacity = res.sms_usage
        if res.sms:
            await self._handle_sms(res.sms)
        if res.lists is not None:
            if res.episode_end:
                self._handle_episode(res.lists)
            self._lists = res.lists
        self._roll_day()
        return replace(state, calls=list(state.calls))

    async def _handle_sms(self, messages: list[SmsMessage]) -> None:
        newest = self._last_sms_ts
        # Deleted messages cannot come back, so only the first-start cut-off
        # matters; a late-joined long SMS may be older than the last one shown.
        deleting = self.config_entry.options.get(CONF_DELETE_SMS, True)
        cut = self._since if deleting else self._last_sms_ts
        for message in messages:
            ts = message.timestamp
            if ts is not None and cut is not None and ts <= cut:
                _LOGGER.info("archived old SMS from %s (%s)", message.number, ts)
                continue
            self._state.last_sms = message
            self.fire(EVENT_SMS, message.as_event())
            self._maybe_command(message)
            if ts is not None and (newest is None or ts > newest):
                newest = ts
        if newest != self._last_sms_ts:
            self._last_sms_ts = newest
            await self._save()

    def _maybe_command(self, message: SmsMessage) -> None:
        """Fire easygate_command for SMS from an allowed number (and PIN)."""
        options = self.config_entry.options
        allowed = [_last9(n) for n in split_recipients(options.get(CONF_COMMAND_NUMBERS))]
        if not allowed or _last9(message.number) not in allowed:
            return
        words = message.text.split()
        pin = str(options.get(CONF_COMMAND_PIN) or "").strip()
        if pin:
            if not words or words[0] != pin:
                _LOGGER.warning("SMS command from %s without valid PIN ignored", message.number)
                return
            words = words[1:]
        if not words:
            return
        self.fire(
            EVENT_COMMAND,
            {
                "number": message.number,
                "command": strip_diacritics(words[0]).upper(),
                "args": words[1:],
                "text": " ".join(words),
            },
        )

    def _handle_calls(self, calls: list[Call]) -> None:
        state = self._state
        state.calls = calls
        seen: dict[int, str] = {}
        for call in calls:
            seen[call.id] = call.state_name
            if self._calls_seen.get(call.id) != call.state_name:
                if call.incoming and call.number:
                    state.last_caller = call.number
                self.fire(
                    EVENT_CALL,
                    {
                        "direction": "incoming" if call.incoming else "outgoing",
                        "state": call.state_name,
                        "number": call.number,
                    },
                )
        self._calls_seen = seen

    def _handle_episode(self, lists: dict[str, tuple[int, list[str]]]) -> None:
        """An incoming call has ended: answered (RC changed) or missed."""
        state = self._state
        received = lists.get("RC", (0, []))
        missed = lists.get("MC", (0, []))
        if received != self._lists.get("RC") and received[1]:
            number = received[1][0]
            state.last_caller = number
            self.fire(
                EVENT_CALL, {"direction": "incoming", "state": "answered", "number": number}
            )
            return
        # MC changed, or the same caller again (list unchanged): a missed call.
        number = missed[1][0] if missed[1] else None
        state.last_missed = number
        state.last_missed_at = dt_util.now()
        if number:
            state.last_caller = number
        self.fire(EVENT_MISSED_CALL, {"number": number})

    # -- sending --

    def _send_blocking(self, number: str, text: str, max_parts: int, ref: int) -> int:
        for attempt in range(SEND_RETRIES):
            try:
                return self.client.send_sms(number, text, max_parts=max_parts, ref=ref)
            except (EasyGateBusy, EasyGateTimeout) as err:
                if isinstance(err, EasyGateTimeout) and "SMS prompt" not in str(err):
                    raise  # timeout after the PDU was sent: don't risk sending twice
                _LOGGER.debug("gateway busy, SMS retry %d", attempt + 1)
                time.sleep(SEND_RETRY_DELAY)
        raise EasyGateBusy("gateway kept the COM port blocked, SMS not sent")

    async def async_send_sms(
        self, message: str, recipients: list[str] | None = None, force: bool = False
    ) -> int:
        """Send ``message``; returns the number of SMS parts sent."""
        options = self.config_entry.options
        recipients = recipients or self.recipients
        if not recipients:
            raise HomeAssistantError("EasyGate: no recipient (set one in the options)")
        text = message
        if options.get(CONF_STRIP_DIACRITICS, True):
            text = strip_diacritics(text)
        max_parts = int(options.get(CONF_MAX_PARTS, DEFAULT_MAX_PARTS))
        try:
            needed = sum(len(encode_sms(r, text, max_parts=max_parts)) for r in recipients)
        except ValueError as err:
            raise HomeAssistantError(f"EasyGate: {err}") from err

        self._roll_day()
        state = self._state
        if not force and state.sms_sent_today + needed > self.daily_limit:
            _LOGGER.warning(
                "SMS not sent, daily limit %d reached (%d sent today): %s",
                self.daily_limit,
                state.sms_sent_today,
                message,
            )
            raise HomeAssistantError(
                f"EasyGate: daily SMS limit {self.daily_limit} reached "
                f"({state.sms_sent_today} sent today)"
            )

        sent = 0
        try:
            for number in recipients:
                self._sms_ref = (self._sms_ref + 1) & 0xFF
                sent += await self.hass.async_add_executor_job(
                    self._send_blocking, number, text, max_parts, self._sms_ref
                )
        except EasyGateError as err:
            raise HomeAssistantError(f"EasyGate: {err}") from err
        finally:
            state.sms_sent_today += sent
            await self._save()
            if self.data is not None:
                self.async_set_updated_data(replace(state, calls=list(state.calls)))
        return sent


def _last9(number: str) -> str:
    """Compare numbers without country prefix: +420 777 123 456 == 777123456."""
    digits = "".join(c for c in number if c.isdigit())
    return digits[-9:] if digits else number
