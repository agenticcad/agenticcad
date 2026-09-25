"""Display units. The kernel, scripts and files are always millimetres; this decides how numbers are SHOWN and how the
agent interprets a bare number from the user. Default follows the computer's timezone: US zones → inches, else mm.
Scripts write imperial dimensions as `2.5 * inch` (inch = 25.4 is pre-imported), so a script can mix both."""
from __future__ import annotations

import os
import time
from pathlib import Path

INCH = 25.4
UNITS = ("mm", "in")

# IANA zone names (or prefixes ending in /) that belong to the United States
_US_ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
             "America/Anchorage", "America/Adak", "America/Juneau", "America/Sitka", "America/Nome", "America/Yakutat",
             "America/Metlakatla", "America/Detroit", "America/Boise", "America/Menominee", "America/Indiana/",
             "America/Kentucky/", "America/North_Dakota/", "Pacific/Honolulu", "America/Puerto_Rico", "US/")
# Windows zone display names for US zones (time.tzname on Windows)
_US_WINDOWS = ("Eastern Standard Time", "Central Standard Time", "Mountain Standard Time", "US Mountain Standard Time",
               "Pacific Standard Time", "Alaskan Standard Time", "Hawaiian Standard Time", "Aleutian Standard Time")
_US_ABBR_PAIRS = {("EST", "EDT"), ("CST", "CDT"), ("MST", "MDT"), ("PST", "PDT"), ("AKST", "AKDT"), ("HST", "HST"), ("MST", "MST")}


def local_zone_name() -> str:
    """Best effort IANA zone name of this computer ('' if unknown)."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz
    try:
        target = str(Path("/etc/localtime").resolve())
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def units_for_timezone(zone: str, tznames: tuple[str, str] | None = None) -> str:
    """'in' for a US timezone, else 'mm'. `tznames` is time.tzname (standard, daylight) as a fallback."""
    if zone:
        return "in" if any(zone == z or (z.endswith("/") and zone.startswith(z)) for z in _US_ZONES) else "mm"
    if tznames:
        std, dst = (tznames + (tznames[0],))[:2]
        if std in _US_WINDOWS:
            return "in"
        if (std, dst) in _US_ABBR_PAIRS and std != "MST" or (std, dst) == ("MST", "MDT"):
            return "in"
    return "mm"


def detect_default_units() -> tuple[str, str]:
    """(units, reason) for this computer."""
    zone = local_zone_name()
    u = units_for_timezone(zone, tuple(time.tzname) if len(time.tzname) == 2 else (time.tzname[0], time.tzname[0]))
    return u, (f"timezone {zone}" if zone else f"timezone {'/'.join(time.tzname)}")


def to_units(mm: float, units: str) -> float:
    return mm / INCH if units == "in" else mm


def fmt_len(mm: float, units: str, digits: int | None = None) -> str:
    """'12.7 mm' or '0.5 in' (inches to 3 decimals, mm to 2, trailing zeros trimmed)."""
    if units == "in":
        s = f"{mm / INCH:.{3 if digits is None else digits}f}"
    else:
        s = f"{mm:.{2 if digits is None else digits}f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return f"{s or '0'} {units}"
