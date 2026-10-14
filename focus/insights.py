"""Insights from NextDNS analytics: lookups per day for 30 days, blocked share this month, top blocked
domains, lookups by rule and by device. Light, not a dashboard.

Fetched when the page loads, cached in the state folder (insights.json) for CACHE_SEC, so the page
waits on the API at most once per interval. A failed fetch is cached too and shows as "no data yet";
the page never breaks on it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

from . import schedule as sch

CACHE_SEC = 5 * 60
DAYS_BACK = 30
TOP = 5
TIMEOUT = 4  # seconds per call when the page is waiting
UNNAMED = "__UNIDENTIFIED__"

_lock = threading.Lock()


def _midnight(day: dt.date, tz) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz)


def fetch(api, settings: dict, ts: float) -> dict:
    """Four or five analytics calls. Raises ApiError on any failure."""
    tz = sch.tz_of(settings)
    today = sch.local(settings, ts).date()
    first = today - dt.timedelta(days=DAYS_BACK - 1)
    # from/to on local midnights give buckets aligned to local days (a bare from=-30d aligns them to now).
    series = api.analytics("status;series", {"from": _midnight(first, tz).isoformat(),
                                             "to": _midnight(today + dt.timedelta(days=1), tz).isoformat(),
                                             "interval": "86400"})
    times = ((series.get("meta") or {}).get("series") or {}).get("times") or []
    totals = [0] * len(times)
    blocked = [0] * len(times)
    for row in series.get("data") or []:
        for i, q in enumerate(row.get("queries") or []):
            if i < len(times):
                totals[i] += int(q or 0)
                if row.get("status") == "blocked":
                    blocked[i] += int(q or 0)
    days = []
    for i, t in enumerate(times):
        # Label each bucket by the local date 12 h into it, so a DST shift cannot move it a day.
        start = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        label = (start + dt.timedelta(hours=12)).astimezone(tz).date()
        days.append({"date": label.isoformat(), "queries": totals[i], "blocked": blocked[i]})
    month_from = _midnight(today.replace(day=1), tz).isoformat()
    month = {"total": 0, "blocked": 0}
    for row in api.analytics("status", {"from": month_from}).get("data") or []:
        q = int(row.get("queries") or 0)
        month["total"] += q
        if row.get("status") == "blocked":
            month["blocked"] += q
    top = [{"domain": str(r.get("domain", "?")), "queries": int(r.get("queries") or 0)}
           for r in (api.analytics("domains", {"from": "-30d", "status": "blocked", "limit": TOP}).get("data") or [])][:TOP]
    reasons = [{"name": str(r.get("name") or r.get("id") or "?"), "queries": int(r.get("queries") or 0)}
               for r in (api.analytics("reasons", {"from": "-30d"}).get("data") or [])]
    devices = []
    for r in api.analytics("devices", {"from": "-30d"}).get("data") or []:
        rid = r.get("id")
        name = r.get("name") or ("unnamed" if rid == UNNAMED or not rid else str(rid))
        devices.append({"name": str(name), "queries": int(r.get("queries") or 0)})
    return {"days": days, "month": month, "top_blocked": top, "reasons": reasons, "devices": devices}


def cache_path(cfg) -> Path:
    return Path(cfg.state_dir) / "insights.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".insights.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written just means the next load asks again


def get(cfg, api_factory, settings: dict, ts: float, fetcher=fetch) -> dict:
    """The cached insights, refreshed if older than CACHE_SEC. Always returns a dict:
    {"at", "ok": True, ...data} or {"at", "ok": False, "error"}."""
    path = cache_path(cfg)
    cached = _read(path)
    if cached and 0 <= ts - float(cached.get("at", 0)) < CACHE_SEC:
        return cached
    with _lock:
        cached = _read(path)  # another request may have refreshed it while this one waited
        if cached and 0 <= ts - float(cached.get("at", 0)) < CACHE_SEC:
            return cached
        try:
            out = {"at": ts, "ok": True, **fetcher(api_factory(), settings, ts)}
        except Exception as e:  # any failure: the page shows "no data yet"
            out = {"at": ts, "ok": False, "error": str(e)[:200]}
        _write(path, out)
        return out
