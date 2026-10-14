"""Weekly numbers: lookups this month against the free-tier cap, passes and blocks."""
from __future__ import annotations

import calendar
import datetime as dt

from . import core, usage
from . import schedule as sch
from .config import QUERY_CAP


def week_start(st: dict, ts: float) -> dt.date:
    d = sch.local(st["settings"], ts).date()
    return d - dt.timedelta(days=d.weekday())


def passes_by_day(st: dict, ts: float) -> list[tuple[dt.date, int]]:
    """This calendar week, Monday to Sunday, with pass counts (days still ahead count 0)."""
    ws = week_start(st, ts)
    counts: dict[dt.date, int] = {}
    for p in st.get("passes", []):
        d = sch.local(st["settings"], p["at"]).date()
        counts[d] = counts.get(d, 0) + 1
    return [(ws + dt.timedelta(days=i), counts.get(ws + dt.timedelta(days=i), 0)) for i in range(7)]


def passes_this_week(st: dict, ts: float) -> int:
    """Passes since Monday 00:00 of this week (a calendar week, not a rolling seven days)."""
    return sum(n for _, n in passes_by_day(st, ts))


def usage_line(st: dict, ts: float) -> str:
    u = st.get("usage")
    if not u:
        return f"Lookups this month: not fetched yet (cap {QUERY_CAP:,})."
    t = sch.local(st["settings"], ts)
    q = int(u.get("queries", 0))
    days_in = calendar.monthrange(t.year, t.month)[1]
    pace = int(q / max(t.day - 1 + t.hour / 24, 0.5) * days_in)
    return (f"Lookups this month: {q:,} of {QUERY_CAP:,} ({q / QUERY_CAP:.1%}); on pace for about {pace:,} "
            f"(as of {sch.fmt_time(st['settings'], u.get('at'), ts)}).")


def watched_sites(st: dict) -> set[str]:
    out = set()
    for L in core.lists(st):
        out |= {usage.site_of(f"{x}.com") for x in L["services"]} | {usage.site_of(d) for d in L["domains"]}
    return out


def usage_section(cfg, st: dict, ts: float, last_week: bool) -> str:
    ws = week_start(st, ts) - dt.timedelta(days=7 if last_week else 0)
    return usage.render_week(cfg, st["settings"], ws, watched=watched_sites(st))


def render(st: dict, ts: float) -> str:
    s = st["settings"]
    week = passes_by_day(st, ts)
    ws = week_start(st, ts)
    blocks = sum(1 for b in st.get("blocks", []) if sch.local(s, b["at"]).date() >= ws)
    lines = [
        f"focus report, {sch.local(s, ts).strftime('%a %Y-%m-%d %H:%M %Z')}",
        usage_line(st, ts),
        f"Passes used this week (since Mon {ws:%m-%d}): {passes_this_week(st, ts)}; Block now used: {blocks}.",
        "This week: " + ", ".join(f"{d:%a} {n}" for d, n in week),
        f"Rules: {s['passes_per_day']} passes a day, {s['pass_minutes']} min each.",
    ]
    for L in core.lists(st):
        sites = [f"{x} (service)" for x in L["services"]] + L["domains"]
        lines.append(f"List {L['name']}: {', '.join(sites) or 'no sites'}; {sch.summarize_week(L['hours'])}.")
    cats = [core.category_name(c["id"]) for c in core.categories(st) if c["active"]]
    if cats:
        lines.append(f"Always blocked: {', '.join(cats)}.")
    lr = st.get("last_reconcile")
    if lr:
        when = sch.fmt_time(s, lr.get("at"), ts)
        lines.append(f"Last sync with NextDNS: {when}, " + ("ok." if lr.get("ok") else f"FAILED: {lr.get('error')}"))
    if core.block_in_force(st, ts):
        lines.append(core.describe(st, ts)["headline"] + ".")
    return "\n".join(lines) + "\n"
