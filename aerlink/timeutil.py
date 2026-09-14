"""Dates and times, handled explicitly.

The host's clock is never the incident date. "Now" for a case is the time the contact
was received (`meta.received_at`, else the message's own `Date:` header). If neither
is available, time-relative reasoning is blocked rather than guessed.

Availability rows carry local clock times at the origin and destination
(`departure_local`, `arrival_local`, the latter with a ``+1`` suffix when it lands the
next day). Those are the only times the inventory system gives us, so deadlines are
compared in the same terms -- destination local -- and the record says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.parser import Parser
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"
_LOCAL_TIME_RE = re.compile(r"^(\d{2}):(\d{2})(\+(\d))?$")
_EMAIL_DATE_RE = re.compile(
    r"(?im)^date:\s*(?:\w{3},\s*)?(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{2}):(\d{2})"
)
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


class UnknownCaseTime(Exception):
    """No trustworthy 'now' is available for this case."""


def parse_iso_z(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _ISO_Z).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def case_now(meta_received_at: str | None, inbound_text: str) -> tuple[datetime, str]:
    """Establish 'now' for the case, and say where it came from."""
    parsed = parse_iso_z(meta_received_at)
    if parsed is not None:
        return parsed, "meta.received_at"

    # Only top-level message headers, never a Date: line inside a forwarded body.
    headers = Parser().parsestr(inbound_text, headersonly=True)
    date_headers = headers.get_all("Date", [])
    if len(date_headers) == 1:
        try:
            stamp = parsedate_to_datetime(date_headers[0])
            if stamp.tzinfo is not None:
                return stamp.astimezone(timezone.utc), "inbound Date: header (offset applied, converted to UTC)"
        except (TypeError, ValueError, OverflowError):
            pass
    raise UnknownCaseTime(
        "Neither meta.received_at nor a parseable Date: header is available, so the "
        "date of the incident cannot be established. The host clock is deliberately "
        "not used as a substitute."
    )


@dataclass(frozen=True)
class LocalTime:
    """A clock time from the inventory system, plus how many days it rolls over."""

    hour: int
    minute: int
    day_offset: int

    @property
    def absolute_minutes(self) -> int:
        return self.day_offset * 1440 + self.hour * 60 + self.minute

    def on(self, base: date) -> datetime:
        return datetime(base.year, base.month, base.day, self.hour, self.minute) + timedelta(
            days=self.day_offset
        )

    def render(self) -> str:
        return "{:02d}:{:02d}{}".format(
            self.hour, self.minute, "+{}".format(self.day_offset) if self.day_offset else ""
        )


def parse_local_time(value: str | None) -> LocalTime | None:
    """Parse '09:55' or '23:40+1' as produced by GET /flights/availability."""
    if not value:
        return None
    match = _LOCAL_TIME_RE.match(value.strip())
    if not match:
        return None
    hour, minute, _grp, offset = match.groups()
    if int(hour) > 23 or int(minute) > 59:
        return None
    return LocalTime(int(hour), int(minute), int(offset) if offset else 0)


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_local_deadline(value: str | None) -> datetime | None:
    """Parse a 'YYYY-MM-DDTHH:MM' deadline expressed in destination local time."""
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        parsed_date = parse_date(text)
        return datetime.combine(parsed_date, datetime.max.time()) if parsed_date else None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", text):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None



def arrival_datetime(row: dict, flight_date: date | None) -> datetime | None:
    """Destination-local arrival for an availability row."""
    base = parse_date(row.get("date")) or flight_date
    local = parse_local_time(row.get("arrival_local"))
    if base is None or local is None:
        return None
    return local.on(base)


def departure_local_minutes(row: dict) -> int | None:
    local = parse_local_time(row.get("departure_local"))
    return None if local is None else local.absolute_minutes


# Airport time zones supplied in env/data/stations.json; the API exposes no airport
# metadata route. This is static configuration, not a local booking-data shortcut.
STATION_TIMEZONES = {
    "LHR": "Europe/London", "LGW": "Europe/London", "MAN": "Europe/London",
    "EDI": "Europe/London", "DUB": "Europe/Dublin", "AMS": "Europe/Amsterdam",
    "BCN": "Europe/Madrid", "MAD": "Europe/Madrid", "FCO": "Europe/Rome",
    "LIS": "Europe/Lisbon", "GVA": "Europe/Zurich", "DXB": "Asia/Dubai",
    "CDG": "Europe/Paris",
}


def station_local_now(iata: str, instant: datetime) -> datetime | None:
    zone = STATION_TIMEZONES.get(str(iata).upper())
    if zone is None or instant.tzinfo is None:
        return None
    try:
        return instant.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError:
        return None


def inventory_departure_utc(row: dict, flight_date: date) -> datetime | None:
    local = parse_local_time(row.get("departure_local"))
    base = parse_date(row.get("date")) or flight_date
    zone = STATION_TIMEZONES.get(str(row.get("origin", "")).upper())
    if local is None or zone is None:
        return None
    try:
        tz = ZoneInfo(zone)
    except ZoneInfoNotFoundError:
        return None
    naive = local.on(base)
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    # A clock-only inventory row cannot resolve a DST overlap or skipped hour.
    if first.utcoffset() != second.utcoffset():
        return None
    utc = first.astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return utc
