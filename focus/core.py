"""Blocking state, lock rules, passes. Pure functions over the state dict; no I/O.

Lists: every blocked site sits in exactly one named list, and each list has its own week. A list is
blocked when its own hours say so, or while Block now runs (Block now covers every list). A pass
opens every list at once; it never opens the always-blocked categories.

Lock rule, per list: while that list's block is in force, anything that tightens it applies at once
and anything that loosens it is queued: it waits in st["pending"] and applies at that list's unlock
(its next open, after any Block now), with an Undo until then. Settings that are not one list's
(pass count and length, timezone, turning an always-blocked category off) lock while any list is
blocked, and unlock at the first moment no list is. Later blocks push a queued change back: it
applies only once its block is no longer in force. A running pass does not lift the lock.
"""
from __future__ import annotations

import datetime as dt
import re

from . import schedule as sch
from .schedule import DAYS, ScheduleError
from .state import DEFAULT_HOURS

BLOCK_NOW_MIN = 90
MAX_BLOCK_MIN = 7 * 24 * 60
MAX_PASSES = 10
MAX_PASS_MIN = 60
MAX_DOMAINS = 200
MAX_LISTS = 12
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}[a-z0-9]$")
LIST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,23}$")
RESERVED_NAMES = {"tz", "timezone"}
CATEGORY_NAMES = {"porn": "Porn", "gambling": "Gambling", "dating": "Dating", "piracy": "Piracy",
                  "social-networks": "Social networks", "gaming": "Gaming", "video-streaming": "Video streaming"}


class Refused(Exception):
    """An edit or action the rules do not allow right now. The message is shown as-is."""


# ------------------------------------------------------------------ lists

def lists(st: dict) -> list[dict]:
    return st["settings"]["lists"]


def find_list(st: dict, name: str | None) -> dict | None:
    n = (name or "").strip().lower()
    return next((L for L in lists(st) if L["name"].lower() == n), None)


def get_list(st: dict, name: str | None) -> dict:
    L = find_list(st, name)
    if L is None:
        names = ", ".join(x["name"] for x in lists(st)) or "none yet"
        raise Refused(f"No list called {(name or '').strip()[:40]!r} (lists: {names}).")
    return L


def list_of_domain(st: dict, domain: str) -> dict | None:
    return next((L for L in lists(st) if domain in L["domains"]), None)


def all_domains(st: dict) -> list[str]:
    return [d for L in lists(st) for d in L["domains"]]


def all_services(st: dict) -> list[str]:
    return [s for L in lists(st) for s in L["services"]]


def lsch(st: dict, L: dict) -> dict:
    """A list's week in the shape the schedule functions take."""
    return {"timezone": st["settings"]["timezone"], "hours": L["hours"]}


def is_empty(L: dict) -> bool:
    return not L["services"] and not L["domains"]


def categories(st: dict) -> list[dict]:
    return st["settings"].setdefault("categories", [])


def category_name(cid: str) -> str:
    return CATEGORY_NAMES.get(cid, cid.replace("-", " ").capitalize())


# ------------------------------------------------------------------ state queries

def block_now_active(st: dict, ts: float) -> bool:
    return st.get("block_until", 0) > ts


def pass_active(st: dict, ts: float) -> bool:
    return st.get("pass_until", 0) > ts


def list_sched_blocked(st: dict, L: dict, ts: float) -> bool:
    return sch.sched_blocked(lsch(st, L), ts)


def list_block_in_force(st: dict, L: dict, ts: float) -> bool:
    return list_sched_blocked(st, L, ts) or block_now_active(st, ts)


def list_blocked(st: dict, L: dict, ts: float) -> bool:
    """Is this list's content blocked right now (schedule or Block now, and no pass)?"""
    return list_block_in_force(st, L, ts) and not pass_active(st, ts)


def sched_blocked(st: dict, ts: float) -> bool:
    """Any list blocked by its own hours."""
    return any(list_sched_blocked(st, L, ts) for L in lists(st))


def block_in_force(st: dict, ts: float) -> bool:
    """Any list's block in force: the lock for settings that are not one list's."""
    return sched_blocked(st, ts) or block_now_active(st, ts)


def effective_blocked(st: dict, ts: float) -> bool:
    return block_in_force(st, ts) and not pass_active(st, ts)


def _boundaries(st: dict, ts: float, L: dict | None = None) -> list[float]:
    out: set[float] = set()
    for x in ([L] if L is not None else lists(st)):
        out.update(sch.boundaries(lsch(st, x), ts))
    return sorted(out)


def _next_open(st: dict, ts: float, L: dict | None) -> float | None:
    """First moment after ts when the schedule is open: for one list, or for every list at once."""
    if L is not None:
        return sch.next_sched_open(lsch(st, L), ts)
    for b in _boundaries(st, ts):
        if b > ts and not sched_blocked(st, b):
            return b
    return None


def unlock_at(st: dict, ts: float, L: dict | None = None) -> float | None:
    """When the block in force ends (schedule and Block now combined), for one list or for all.
    None = not within a week."""
    blocked = (lambda t: list_sched_blocked(st, L, t)) if L is not None else (lambda t: sched_blocked(st, t))
    t = ts
    for _ in range(64):
        if block_now_active(st, t):
            t = st["block_until"]
            continue
        if blocked(t):
            nxt = _next_open(st, t, L)
            if nxt is None:
                return None
            t = nxt
            continue
        return t
    return None


def scope_in_force(st: dict, L: dict | None, ts: float) -> bool:
    return list_block_in_force(st, L, ts) if L is not None else block_in_force(st, ts)


def next_change(st: dict, ts: float, L: dict | None = None) -> tuple[float, bool] | None:
    """(when, blocked_after) for the next time this list (or the whole set) flips, or None."""
    state = (lambda t: list_blocked(st, L, t)) if L is not None else (lambda t: effective_blocked(st, t))
    cands = [b for b in _boundaries(st, ts, L) if b > ts]
    cands += [x for x in (st.get("pass_until", 0), st.get("block_until", 0)) if x > ts]
    cur = state(ts)
    for c in sorted(set(cands)):
        if state(c) != cur:
            return c, not cur
    return None


def _local_date(st: dict, ts: float):
    return sch.local(st["settings"], ts).date()


def passes_on(st: dict, ts: float) -> int:
    day = _local_date(st, ts)
    return sum(1 for p in st.get("passes", []) if _local_date(st, p["at"]) == day)


def passes_left(st: dict, ts: float) -> int:
    return max(0, int(st["settings"]["passes_per_day"]) - passes_on(st, ts))


def when(st: dict, t: float | None, now: float) -> str:
    return sch.fmt_time(st["settings"], t, now)


def unlock_words(st: dict, ts: float, L: dict | None = None) -> str:
    """'17:00', 'tomorrow 00:00', or a sentence when there is no unlock this week."""
    u = unlock_at(st, ts, L)
    return when(st, u, ts) if u is not None else "the next open hour (none this week)"


def locked_message(st: dict, ts: float, L: dict | None = None) -> str:
    u = unlock_at(st, ts, L)
    if u is None:
        return "Locked: no open hours this week"
    return f"Locked until {when(st, u, ts)}"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def list_status(st: dict, L: dict, ts: float) -> dict:
    nc = next_change(st, ts, L)
    blocked = list_blocked(st, L, ts)
    return {"name": L["name"], "blocked": blocked, "next_at": nc[0] if nc else None,
            "until": when(st, nc[0], ts) if nc else "further notice",
            "locked": list_block_in_force(st, L, ts)}


def describe(st: dict, ts: float) -> dict:
    """Plain-words status for the page and `focus status`."""
    per = [list_status(st, L, ts) for L in lists(st)]
    groups: dict[tuple, list[str]] = {}
    for p in per:
        groups.setdefault((p["blocked"], p["next_at"]), []).append(p["name"])
    blocked_any = any(p["blocked"] for p in per)
    blocked_all = bool(per) and all(p["blocked"] for p in per)
    if len(groups) <= 1:
        nc = next_change(st, ts)
        until = when(st, nc[0], ts) if nc else "further notice"
        by_block_now = bool(nc) and block_now_active(st, ts) and nc[0] == st.get("block_until")
        today = bool(nc) and _local_date(st, nc[0]) == _local_date(st, ts)
        if pass_active(st, ts):
            headline = f"Pass running: open until {until}"
        elif blocked_any and by_block_now and today:
            headline = f"Blocked until {until} (today)"
        elif blocked_any:
            headline = f"Blocked until {until}"
        else:
            headline = f"Open until {until}"
        if nc is None:
            nxt = "No change in the next week."
        else:
            nc2 = next_change(st, nc[0])
            state = "blocked" if nc[1] else "open"
            nxt = f"Then {state} from {until} to {when(st, nc2[0], ts) if nc2 else 'further notice'}."
        next_at = nc[0] if nc else None
    else:
        parts = []
        for (b, at), names in sorted(groups.items(), key=lambda kv: (not kv[0][0], kv[0][1] or 1e18)):
            parts.append(f"{_join(names)} {'blocked' if b else 'open'} until {when(st, at, ts) if at else 'further notice'}")
        headline = ". ".join(parts)
        if pass_active(st, ts):
            headline = f"Pass running: open until {when(st, st['pass_until'], ts)}"
        soon = min((p for p in per if p["next_at"]), key=lambda p: p["next_at"], default=None)
        nxt = (f"Next change: {soon['name']} {'opens' if soon['blocked'] else 'blocks'} at {soon['until']}."
               if soon else "No change in the next week.")
        next_at = soon["next_at"] if soon else None
    locked = block_in_force(st, ts)
    return {"blocked": blocked_any, "blocked_all": blocked_all, "headline": headline, "next": nxt,
            "next_at": next_at, "lists": per, "locked": locked,
            "locked_message": locked_message(st, ts) if locked else "",
            "passes_left": passes_left(st, ts), "pass_active": pass_active(st, ts),
            "unlock": unlock_at(st, ts) if locked else None}


# ------------------------------------------------------------------ queue (loosening during a block)
#
# One pending entry per field, keyed by id:
#   remove:<domain>          {"domain", "list"}          take a domain off its list
#   hours:<list>:<day>       {"list", "day", "rule"}     wider open windows for that list's day
#   passes                   {"value"}                   more passes a day
#   minutes                  {"value"}                   longer passes
#   timezone                 {"value"}                   a new timezone
#   category:<id>            {"category"}                turn an always-blocked category off
# "list" names the list whose unlock the entry waits for; entries without one wait for the moment
# no list is blocked. Each also has "queued" and "applies" (kept current by refresh_pending).
# apply_due applies an entry only once its block is no longer in force, never earlier.

def pending(st: dict) -> list[dict]:
    return st.setdefault("pending", [])


def find_pending(st: dict, pid: str) -> dict | None:
    return next((p for p in pending(st) if p.get("id") == pid), None)


def _drop(st: dict, pid: str) -> bool:
    before = len(pending(st))
    st["pending"] = [p for p in pending(st) if p.get("id") != pid]
    return len(st["pending"]) != before


def _scope(st: dict, p: dict) -> dict | None:
    return find_list(st, p.get("list")) if p.get("list") else None


def _queue(st: dict, ts: float, pid: str, L: dict | None = None, **fields) -> dict:
    _drop(st, pid)
    entry = {"id": pid, "queued": ts, "applies": unlock_at(st, ts, L), **fields}
    if L is not None:
        entry["list"] = L["name"]
    pending(st).append(entry)
    return entry


def refresh_pending(st: dict, ts: float) -> None:
    """Keep each entry's "applies" on its current unlock (a tightening edit can push it later)."""
    for p in pending(st):
        L = _scope(st, p)
        p["applies"] = unlock_at(st, ts, L) if scope_in_force(st, L, ts) else ts


def hours_pid(L: dict, day: str) -> str:
    return f"hours:{L['name'].lower()}:{day}"


def describe_pending(st: dict, p: dict, ts: float | None = None) -> str:
    """What the change does, in words: 'Remove x.com', 'Social Tue: open from 16:00 to midnight'."""
    pid = p.get("id", "")
    if pid.startswith("remove:"):
        return f"Remove {p['domain']}"
    if pid.startswith("hours:"):
        return f"{p.get('list', '')} {sch.ABBR.get(p.get('day'), '?')}: {sch.describe_rule(p['rule'])}".strip()
    if pid == "passes":
        return f"{p['value']} passes a day"
    if pid == "minutes":
        return f"{p['value']}-minute passes"
    if pid == "timezone":
        return f"Timezone {p['value']}"
    if pid.startswith("category:"):
        return f"{category_name(p['category'])} off"
    return pid


def pending_when(st: dict, ts: float, p: dict | None = None) -> str:
    """When a queued entry (or, without one, the global queue) applies, in words."""
    L = _scope(st, p) if p else None
    return unlock_words(st, ts, L) if scope_in_force(st, L, ts) else "now"


def undo(st: dict, pid: str, ts: float) -> str:
    p = find_pending(st, (pid or "").strip())
    if not p:
        raise Refused("Nothing queued under that name.")
    _drop(st, p["id"])
    return f"Undone: {describe_pending(st, p)}. Nothing changes."


def _remove_now(st: dict, L: dict, d: str) -> None:
    L["domains"] = [x for x in L["domains"] if x != d]
    if d not in st.setdefault("pending_removals", []):
        st["pending_removals"].append(d)


def _apply_one(st: dict, p: dict) -> None:
    s = st["settings"]
    pid = p["id"]
    if pid.startswith("remove:"):
        d = p["domain"]
        L = list_of_domain(st, d)
        if L is not None:
            _remove_now(st, L, d)
    elif pid.startswith("hours:"):
        L = get_list(st, p["list"])
        L["hours"] = {**L["hours"], p["day"]: sch.normalize_rule(p["rule"])}
    elif pid == "passes":
        s["passes_per_day"] = int(p["value"])
    elif pid == "minutes":
        s["pass_minutes"] = int(p["value"])
    elif pid == "timezone":
        s["timezone"] = sch.validate_timezone(p["value"])
    elif pid.startswith("category:"):
        for c in categories(st):
            if c["id"] == p["category"]:
                c["active"] = False
    else:
        raise KeyError(pid)


def apply_due(st: dict, ts: float) -> list[str]:
    """Apply every queued change whose block is no longer in force. Returns what was applied, in words."""
    done = []
    keep = []
    for p in list(pending(st)):
        if p.get("list") and find_list(st, p["list"]) is None and not p["id"].startswith("remove:"):
            done.append(f"dropped a queued change that no longer fits: {p.get('id')}")
            continue
        L = _scope(st, p)
        if scope_in_force(st, L, ts):
            keep.append(p)
            continue
        try:
            _apply_one(st, p)
            done.append("applied queued change: " + describe_pending(st, p).lower())
        except (ScheduleError, KeyError, ValueError, TypeError, Refused):
            done.append(f"dropped a queued change that no longer fits: {p.get('id')}")
    st["pending"] = keep
    return done


# ------------------------------------------------------------------ guards

def clean_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if not DOMAIN_RE.match(d):
        raise Refused(f"Not a domain name: {raw.strip()[:80]!r}")
    return d


def clean_list_name(raw: str) -> str:
    n = " ".join((raw or "").split())
    if not LIST_NAME_RE.match(n) or n.lower() in RESERVED_NAMES:
        raise Refused("A list name is 1 to 24 letters, digits, spaces, - or _ (and not 'tz').")
    return n


# ------------------------------------------------------------------ edits

def add_list(st: dict, raw: str, ts: float) -> str:
    name = clean_list_name(raw)
    if find_list(st, name):
        raise Refused(f"There is already a list called {name}.")
    if len(lists(st)) >= MAX_LISTS:
        raise Refused(f"At most {MAX_LISTS} lists.")
    lists(st).append({"name": name, "services": [], "domains": [], "hours": dict(DEFAULT_HOURS)})
    return f"New list {name}: {sch.summarize_week(DEFAULT_HOURS)}. Add sites to it, then set its hours."


def remove_list(st: dict, name: str, ts: float) -> str:
    L = get_list(st, name)
    if not is_empty(L):
        raise Refused(f"{L['name']} still has sites; take them off first.")
    if len(lists(st)) == 1:
        raise Refused("That is the only list.")
    st["settings"]["lists"] = [x for x in lists(st) if x is not L]
    st["pending"] = [p for p in pending(st) if (p.get("list") or "").lower() != L["name"].lower()]
    return f"List {L['name']} is gone."


def add_domain(st: dict, raw: str, ts: float, list_name: str | None = None) -> str:
    d = clean_domain(raw)
    if list_name is None:
        if len(lists(st)) != 1:
            raise Refused("Which list? Give one: focus add DOMAIN --list NAME.")
        L = lists(st)[0]
    else:
        L = get_list(st, list_name)
    have = list_of_domain(st, d)
    if have is L:
        if _drop(st, f"remove:{d}"):
            return f"{d} stays on {L['name']} (queued removal undone)."
        return f"{d} is already on {L['name']}."
    if have is not None:
        raise Refused(f"{d} is on {have['name']}. A site belongs to one list; take it off {have['name']} first.")
    if len(all_domains(st)) >= MAX_DOMAINS:
        raise Refused(f"The lists are full ({MAX_DOMAINS} domains).")
    L["domains"] = L["domains"] + [d]
    st["pending_removals"] = [x for x in st.get("pending_removals", []) if x != d]
    return f"Added {d} to {L['name']}."


def remove_domain(st: dict, raw: str, ts: float) -> str:
    d = (raw or "").strip().lower()
    L = list_of_domain(st, d)
    if L is None:
        raise Refused(f"{d[:80]} is not on any list.")
    if list_block_in_force(st, L, ts):
        _queue(st, ts, f"remove:{d}", L, domain=d)
        return f"Removes {d} at {unlock_words(st, ts, L)}."
    _remove_now(st, L, d)
    return f"Removed {d}."


def set_hours(st: dict, list_name: str | None, changes: dict, ts: float) -> str:
    """changes: {day: rule} for any subset of days of one list. All-or-nothing validation.

    Per day: windows inside the current ones (shorter, fewer, or blocked) apply at once. While the
    list's block is in force, a rule that opens anything new is queued for the list's unlock; the
    part of it that is also tighter applies now (the overlap of old and new), so nothing opens early.
    A list with no sites has nothing to loosen, so its hours always apply at once."""
    if list_name is None:
        if len(lists(st)) != 1:
            raise Refused("Which list? focus hours LIST DAY RULE.")
        L = lists(st)[0]
    else:
        L = get_list(st, list_name)
    cur = {d: sch.normalize_rule(L["hours"][d]) for d in DAYS}
    new = {}
    for day, rule in changes.items():
        if day not in DAYS:
            raise Refused(f"Unknown day {day!r}")
        try:
            new[day] = sch.normalize_rule(rule)
        except ScheduleError as e:
            raise Refused(f"{sch.ABBR[day]}: {e}") from None
    locked = list_block_in_force(st, L, ts) and not is_empty(L)
    applied, queued = [], []
    hours = dict(cur)
    for day, rule in new.items():
        if rule == cur[day]:
            continue  # the page posts all seven days; an unchanged day keeps its queued change
        if not locked or sch.is_tighter(rule, cur[day]):
            hours[day] = rule
            _drop(st, hours_pid(L, day))
            applied.append(day)
        else:
            now = sch.intersect(rule, cur[day])
            if now != cur[day]:
                hours[day] = now
            queued.append((day, rule))
    L["hours"] = hours
    for day, rule in queued:
        _queue(st, ts, hours_pid(L, day), L, day=day, rule=rule)
    if not applied and not queued:
        return "No change to the hours."
    parts = []
    if applied:
        parts.append(f"{L['name']} hours saved.")
    if queued:
        parts.append(f"Longer open hours for {L['name']} {', '.join(sch.ABBR[d] for d, _ in queued)} "
                     f"apply at {unlock_words(st, ts, L)}.")
    return " ".join(parts)


def set_timezone(st: dict, tz: str, ts: float) -> str:
    try:
        tz = sch.validate_timezone(tz)
    except ScheduleError as e:
        raise Refused(str(e)) from None
    if tz == st["settings"]["timezone"]:
        return "No change to the timezone."
    if block_in_force(st, ts):
        _queue(st, ts, "timezone", value=tz)
        return f"Timezone changes to {tz} at {unlock_words(st, ts)}."
    st["settings"]["timezone"] = tz
    return f"Timezone set to {tz}."


def set_limits(st: dict, ts: float, passes: int | None = None, minutes: int | None = None) -> str:
    s = st["settings"]
    np_ = s["passes_per_day"] if passes is None else int(passes)
    nm = s["pass_minutes"] if minutes is None else int(minutes)
    if not 0 <= np_ <= MAX_PASSES:
        raise Refused(f"Passes per day must be 0 to {MAX_PASSES}.")
    if not 1 <= nm <= MAX_PASS_MIN:
        raise Refused(f"Pass length must be 1 to {MAX_PASS_MIN} minutes.")
    locked = block_in_force(st, ts)
    out = []
    for key, field, val, unit in (("passes", "passes_per_day", np_, "passes a day"),
                                  ("minutes", "pass_minutes", nm, "min a pass")):
        if (key == "passes" and passes is None) or (key == "minutes" and minutes is None):
            continue
        if val == s[field]:
            continue  # unchanged (the form posts both fields); a queued change stays queued
        if val > s[field] and locked:
            _queue(st, ts, key, value=val)
            out.append(f"{val} {unit} from {unlock_words(st, ts)}.")
        else:
            s[field] = val
            _drop(st, key)
    if not out:
        return f"Passes: {s['passes_per_day']} a day, {s['pass_minutes']} min each."
    return f"Passes: {s['passes_per_day']} a day, {s['pass_minutes']} min each now; " + " ".join(out)


def set_category(st: dict, cid: str, on: bool, ts: float) -> str:
    """Always-blocked categories: on applies at once; off is loosening (queued while any list is blocked)."""
    c = next((x for x in categories(st) if x["id"] == (cid or "").strip().lower()), None)
    if c is None:
        raise Refused(f"No always-blocked category {cid!r}.")
    name = category_name(c["id"])
    pid = f"category:{c['id']}"
    if on:
        dropped = _drop(st, pid)
        if c["active"]:
            return f"{name} stays blocked{' (queued change undone)' if dropped else ''}."
        c["active"] = True
        return f"{name} is blocked again."
    if not c["active"]:
        return f"{name} is already off."
    if block_in_force(st, ts):
        _queue(st, ts, pid, category=c["id"])
        return f"{name} goes off at {unlock_words(st, ts)}."
    c["active"] = False
    return f"{name} is off."


# ------------------------------------------------------------------ actions

def take_pass(st: dict, ts: float, minutes: int | None = None) -> float:
    """Start a pass: every list opens; always-blocked categories stay blocked. Returns when it ends."""
    limit = int(st["settings"]["pass_minutes"])
    m = limit if minutes is None else int(minutes)
    if m < 1:
        raise Refused("A pass is at least 1 minute.")
    if m > limit:
        raise Refused(f"A pass is at most {limit} min.")
    if not block_in_force(st, ts):
        raise Refused("Nothing is blocked right now.")
    if pass_active(st, ts):
        raise Refused(f"A pass is already running until {when(st, st['pass_until'], ts)}.")
    if passes_left(st, ts) <= 0:
        raise Refused(f"No passes left today ({st['settings']['passes_per_day']} used). More at midnight.")
    st["pass_until"] = ts + m * 60
    st.setdefault("passes", []).append({"at": ts, "minutes": m})
    return st["pass_until"]


def _set_block(st: dict, ts: float, until: float) -> float:
    st["block_until"] = max(st.get("block_until", 0), until)
    if pass_active(st, ts):
        st["pass_until"] = ts
    st.setdefault("blocks", []).append({"at": ts, "until": st["block_until"]})
    refresh_pending(st, ts)
    return st["block_until"]


def block_now(st: dict, ts: float, minutes: int = BLOCK_NOW_MIN) -> float:
    """Block every list until `minutes` from now, or until the latest scheduled open among the lists
    blocked right now, whichever is later. Ends a running pass. Never shortens an existing block."""
    m = int(minutes)
    if not 1 <= m <= MAX_BLOCK_MIN:
        raise Refused(f"Block for 1 to {MAX_BLOCK_MIN} minutes.")
    until = ts + m * 60
    for L in lists(st):
        if list_sched_blocked(st, L, ts):
            nxt = sch.next_sched_open(lsch(st, L), ts)
            if nxt:
                until = max(until, nxt)
    return _set_block(st, ts, until)


def block_until(st: dict, hhmm: str, ts: float) -> float:
    """Block every list until HH:MM today. Ends a running pass. Never shortens an existing block."""
    try:
        sch.validate_hhmm((hhmm or "").strip())
    except ScheduleError:
        raise Refused(f"Block until needs a time as HH:MM, got {hhmm!r}.") from None
    h, m = map(int, hhmm.strip().split(":"))
    now = sch.local(st["settings"], ts)
    target = dt.datetime(now.year, now.month, now.day, h, m, tzinfo=sch.tz_of(st["settings"])).timestamp()
    if target <= ts:
        raise Refused(f"{hhmm.strip()} has passed today. Block until takes a time later today.")
    return _set_block(st, ts, target)
