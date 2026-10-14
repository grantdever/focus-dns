"""The local source of truth: settings plus pass/block bookkeeping, one JSON file under a file lock.

The defaults are exactly the NextDNS profile as configured on 2026-10-13, so the first
reconcile against it changes nothing. A state file from before lists (one hours table, one
services list, one domains list) is migrated on read into the same two lists the defaults have:
Video (youtube) and Social (twitter and every domain), each with the old hours.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .schedule import ScheduleError, normalize_rule

DEFAULT_HOURS = {"monday": "17:00-24:00", "tuesday": "17:00-24:00", "wednesday": "17:00-24:00",
                 "thursday": "17:00-24:00", "friday": "17:00-24:00", "saturday": "open", "sunday": "open"}

DEFAULT_SETTINGS = {
    "timezone": "America/New_York",
    # Named lists, each with its own week. A domain or service belongs to exactly one list.
    "lists": [
        {"name": "Video", "services": ["youtube"], "domains": [], "hours": dict(DEFAULT_HOURS)},
        {"name": "Social", "services": ["twitter"], "domains": ["x.com"], "hours": dict(DEFAULT_HOURS)},
    ],
    # Parental Control categories: always blocked (no schedule, a pass never opens them) while active.
    "categories": [{"id": "porn", "active": True}, {"id": "gambling", "active": True}],
    "pass_minutes": 15,
    "passes_per_day": 3,
}

OLD_KEYS = ("hours", "services", "domains")

KEEP_DAYS = 90


def default_state() -> dict:
    return {
        "version": 3,
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "pass_until": 0,
        "block_until": 0,
        "passes": [],          # [{"at": epoch, "minutes": n}]
        "blocks": [],          # [{"at": epoch, "until": epoch}]
        "pending": [],         # loosening edits queued during a block; see core.queue
        "pending_removals": [],  # domains taken off the list, still to delete at NextDNS
        "last_reconcile": None,  # {"at", "ok", "changes", "error"}
        "usage": None,           # {"at", "month", "queries"}
    }


def migrate_lists(old: dict) -> list[dict]:
    """v1/v2 settings (one hours table, services, domains) -> the v3 lists, with the old hours."""
    hours = dict(old.get("hours") or DEFAULT_HOURS)
    services = list(old.get("services") or [])
    domains = list(old.get("domains") or [])
    video = {"name": "Video", "services": [s for s in services if s == "youtube"], "domains": [],
             "hours": dict(hours)}
    social = {"name": "Social", "services": [s for s in services if s != "youtube"], "domains": domains,
              "hours": dict(hours)}
    out = [x for x in (video, social) if x["services"] or x["domains"]]
    return out or [social]


def _norm_hours(hours) -> None:
    if isinstance(hours, dict):
        for d, r in list(hours.items()):
            try:
                hours[d] = normalize_rule(r)
            except ScheduleError:
                pass  # left as is; reconcile refuses it loudly before anything reaches NextDNS


def _merge(data: dict) -> dict:
    st = default_state()
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "settings" and isinstance(v, dict):
                if "lists" not in v and any(k2 in v for k2 in OLD_KEYS):
                    v = {**v, "lists": migrate_lists(v)}
                st["settings"].update({k2: v2 for k2, v2 in v.items() if k2 not in OLD_KEYS})
            elif k in st and k != "version":
                st[k] = v
    for lst in st["settings"].get("lists") or []:
        if isinstance(lst, dict):
            lst.setdefault("services", [])
            lst.setdefault("domains", [])
            lst.setdefault("hours", dict(DEFAULT_HOURS))
            _norm_hours(lst["hours"])
    return st


def prune(st: dict, now: float) -> None:
    cutoff = now - KEEP_DAYS * 86400
    st["passes"] = [p for p in st["passes"] if p.get("at", 0) >= cutoff]
    st["blocks"] = [b for b in st["blocks"] if b.get("at", 0) >= cutoff]


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return _merge(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return default_state()
        except (json.JSONDecodeError, UnicodeDecodeError):
            # A broken file falls back to the defaults, which block. Keep the bad copy for a look.
            try:
                self.path.replace(self.path.with_name(f"state.bad-{int(time.time())}.json"))
            except OSError:
                pass
            return default_state()

    def _write(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".state.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)

    @contextmanager
    def _flock(self, mode):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_name("state.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, mode)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def locked(self):
        """Exclusive read-modify-write. Saved only if the block exits normally and something changed."""
        with self._flock(fcntl.LOCK_EX):
            st = self._read()
            before = json.dumps(st, sort_keys=True)
            yield st
            if json.dumps(st, sort_keys=True) != before or not self.path.exists():
                self._write(st)

    def read(self) -> dict:
        with self._flock(fcntl.LOCK_SH):
            return self._read()
