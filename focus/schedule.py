"""Weekly hours: per weekday, one rule. A day's rule is a list of open windows.

    "blocked"                   blocked all day
    "open"                      open all day
    "HH:MM-HH:MM[,HH:MM-HH:MM]" open during each window, blocked the rest of the day

Input: a string with comma-separated windows, or a list of window strings. A window "HH:MM" means
HH:MM-24:00; an end of "", 00:00 or 23:59 means midnight (24:00). Windows do not cross midnight.
Overlapping or touching windows are merged; 00:00-24:00 is "open".

NextDNS holds one window per day per profile (Recreation Time). focus writes the coarse span there
(the earliest open to the latest close across the lists that use services; see `span`), and the
The server's timers enforce every edge of every list's windows.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ABBR = {d: d[:3].capitalize() for d in DAYS}
DAY_ALIASES = {**{d: (d,) for d in DAYS}, **{d[:3]: (d,) for d in DAYS},
               "weekdays": DAYS[:5], "weekend": DAYS[5:], "all": DAYS}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
REC_END = "23:59"      # how NextDNS holds "to midnight"
MIDNIGHT = "24:00"     # how focus holds it
DAY_MIN = 24 * 60
MAX_WINDOWS = 6        # per day per list


class ScheduleError(ValueError):
    pass


def validate_hhmm(s: str) -> str:
    if not isinstance(s, str) or not HHMM.match(s):
        raise ScheduleError(f"time must be HH:MM (24-hour), got {s!r}")
    return s


def _mins(hhmm: str) -> int:
    if hhmm == MIDNIGHT:
        return DAY_MIN
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(mins: int) -> str:
    return MIDNIGHT if mins >= DAY_MIN else f"{mins // 60:02d}:{mins % 60:02d}"


def _window(start: str, end: str | None) -> tuple[int, int]:
    start = validate_hhmm((start or "").strip())
    end = (end or "").strip()
    if end in ("", "00:00", REC_END, MIDNIGHT):
        end = MIDNIGHT
    else:
        validate_hhmm(end)
    if _mins(end) <= _mins(start):
        raise ScheduleError(f"the end ({end}) must be after the start ({start}); windows do not cross midnight")
    return _mins(start), _mins(end)


def _from_windows(ws: list[tuple[int, int]]) -> str:
    """Canonical rule from minute windows: sorted, overlaps and touching windows merged."""
    merged: list[list[int]] = []
    for s, e in sorted(ws):
        if s >= e:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if not merged:
        return "blocked"
    if merged == [[0, DAY_MIN]]:
        return "open"
    return ",".join(f"{_hhmm(s)}-{_hhmm(e)}" for s, e in merged)


def make_rule(start: str, end: str | None = None) -> str:
    """Canonical rule for one window. Blank, 00:00, 23:59 and 24:00 ends all mean midnight."""
    return _from_windows([_window(start, end)])


def make_windows(pairs) -> str:
    """Canonical rule from (start, end) pairs. No pairs is blocked all day."""
    pairs = list(pairs)
    if len(pairs) > MAX_WINDOWS:
        raise ScheduleError(f"at most {MAX_WINDOWS} windows a day")
    return _from_windows([_window(a, b) for a, b in pairs])


def _parse_window(part: str) -> tuple[str, str | None]:
    p = part.strip()
    if "-" in p:
        a, _, b = p.partition("-")
        if "-" in b:
            raise ScheduleError(f"bad window {part!r}")
        return a.strip(), b.strip()
    return p, None


def normalize_rule(rule) -> str:
    """Validate one day's rule (string or list of windows) and return its canonical form."""
    if isinstance(rule, (list, tuple)):
        if not all(isinstance(x, str) for x in rule):
            raise ScheduleError(f"bad rule {rule!r}")
        return make_windows([_parse_window(x) for x in rule])
    if not isinstance(rule, str):
        raise ScheduleError(f"bad rule {rule!r}")
    r = rule.strip().lower()
    if r in ("blocked", "open"):
        return r
    parts = r.split(",")
    if any(not x.strip() for x in parts):
        raise ScheduleError(f"bad rule {rule!r}")
    return make_windows([_parse_window(x) for x in parts])


def validate_hours(hours: dict) -> dict:
    if not isinstance(hours, dict) or set(hours) != set(DAYS):
        raise ScheduleError("hours need exactly the seven days")
    return {d: normalize_rule(hours[d]) for d in DAYS}


def validate_timezone(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ScheduleError(f"unknown timezone {tz!r}") from None
    return tz


def windows(rule) -> list[tuple[int, int]]:
    """The open windows as minutes from midnight. Blocked all day is []."""
    r = normalize_rule(rule)
    if r == "blocked":
        return []
    if r == "open":
        return [(0, DAY_MIN)]
    out = []
    for part in r.split(","):
        a, b = part.split("-")
        out.append((_mins(a), _mins(b)))
    return out


def window_pairs(rule) -> list[tuple[str, str]]:
    """The open windows as (HH:MM, HH:MM); midnight is 24:00."""
    return [(_hhmm(s), _hhmm(e)) for s, e in windows(rule)]


def is_tighter(new, old) -> bool:
    """True if the new rule opens nothing the old one kept blocked (every new window fits inside old ones)."""
    olds = windows(old)
    return all(any(os_ <= s and e <= oe for os_, oe in olds) for s, e in windows(new))


def intersect(a, b) -> str:
    """The rule open only where both are open: tighter than either."""
    out = []
    for as_, ae in windows(a):
        for bs, be in windows(b):
            s, e = max(as_, bs), min(ae, be)
            if s < e:
                out.append((s, e))
    return _from_windows(out)


def span(rules) -> str:
    """One window from the earliest open to the latest close of these rules (blocked if none open)."""
    ws = [w for r in rules for w in windows(r)]
    if not ws:
        return "blocked"
    return _from_windows([(min(s for s, _ in ws), max(e for _, e in ws))])


def _fmt_end(e: str) -> str:
    return "midnight" if e == MIDNIGHT else e


def describe_rule(rule) -> str:
    r = normalize_rule(rule)
    if r == "blocked":
        return "blocked all day"
    if r == "open":
        return "open all day"
    return "open " + " and ".join(f"from {s} to {_fmt_end(e)}" for s, e in window_pairs(r))


def short_rule(rule) -> str:
    """Compact form for summaries: 'all day', 'blocked', '07:00–09:00, 17:00–00:00'."""
    r = normalize_rule(rule)
    if r == "blocked":
        return "blocked"
    if r == "open":
        return "all day"
    return ", ".join(f"{s}–{'00:00' if e == MIDNIGHT else e}" for s, e in window_pairs(r))


def summarize_week(hours: dict) -> str:
    """'Mon–Fri 17:00–00:00 · Sat–Sun all day': consecutive days with the same rule grouped."""
    groups: list[list] = []
    for d in DAYS:
        r = normalize_rule(hours[d])
        if groups and groups[-1][2] == r:
            groups[-1][1] = d
        else:
            groups.append([d, d, r])
    out = []
    for a, b, r in groups:
        days = ABBR[a] if a == b else f"{ABBR[a]}–{ABBR[b]}"
        out.append(f"{days} {short_rule(r)}")
    return " · ".join(out)


def to_recreation(settings: dict) -> dict:
    """The NextDNS parentalControl.recreation object for {timezone, hours}. Each day must be one window
    (the coarse span). Validates first: a malformed write there wipes the whole schedule."""
    hours = validate_hours(settings["hours"])
    tz = validate_timezone(settings["timezone"])
    times = {}
    for d in DAYS:
        pairs = window_pairs(hours[d])
        if not pairs:
            continue
        if len(pairs) > 1:
            raise ScheduleError(f"NextDNS holds one window a day; {d} has {len(pairs)}")
        s, e = pairs[0]
        times[d] = {"start": validate_hhmm(s), "end": REC_END if e == MIDNIGHT else validate_hhmm(e)}
    return {"timezone": tz, "times": times}


def normalize_remote_recreation(rec: dict | None) -> dict:
    """NextDNS reads times back as HH:MM:SS; compare on HH:MM."""
    rec = rec or {}
    times = {}
    for d, v in (rec.get("times") or {}).items():
        if not isinstance(v, dict):
            continue
        times[d] = {k: str(v.get(k, ""))[:5] for k in ("start", "end")}
    return {"timezone": rec.get("timezone"), "times": times}


# ---------------------------------------------------------------- time math

def tz_of(settings: dict) -> ZoneInfo:
    return ZoneInfo(settings["timezone"])


def local(settings: dict, ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz_of(settings))


def sched_blocked(settings: dict, ts: float) -> bool:
    """settings: anything with "timezone" and "hours" (a list's hours with the global timezone)."""
    t = local(settings, ts)
    m = t.hour * 60 + t.minute
    return not any(s <= m < e for s, e in windows(settings["hours"][DAYS[t.weekday()]]))


def edge_times(rule) -> list[str]:
    """The HH:MM moments inside a day where this rule flips (midnight not included)."""
    out = set()
    for s, e in windows(rule):
        out.update(x for x in (s, e) if 0 < x < DAY_MIN)
    return [_hhmm(x) for x in sorted(out)]


def boundaries(settings: dict, ts: float, days: int = 9) -> list[float]:
    """Every schedule edge (midnights, open and close times) from yesterday through `days` ahead."""
    tz = tz_of(settings)
    start = local(settings, ts).date() - dt.timedelta(days=1)
    out = []
    for i in range(days + 1):
        day = start + dt.timedelta(days=i)
        out.append(dt.datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())
        for hm in edge_times(settings["hours"][DAYS[day.weekday()]]):
            h, m = map(int, hm.split(":"))
            out.append(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp())
    return sorted(set(out))


def next_sched_open(settings: dict, ts: float) -> float | None:
    """First moment after ts when the schedule is open, or None if never in the next week."""
    for b in boundaries(settings, ts):
        if b > ts and not sched_blocked(settings, b):
            return b
    return None


def fmt_time(settings: dict, ts: float | None, now: float) -> str:
    """'17:00', 'tomorrow 00:00', or 'Wed 17:00'."""
    if ts is None:
        return "never"
    t, n = local(settings, ts), local(settings, now)
    hm = t.strftime("%H:%M")
    delta = (t.date() - n.date()).days
    if delta == 0:
        return hm
    if delta == 1:
        return f"tomorrow {hm}"
    return f"{t.strftime('%a')} {hm}"
