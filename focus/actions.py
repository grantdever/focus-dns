"""The one place edits happen: lock the state, apply a rule-checked change, push it to NextDNS.

Used by both the CLI and the page, so the phone, the desktop and the terminal obey the same rules.
"""
from __future__ import annotations

import time

from . import core, insights, reconcile, timers, usage
from .api import ApiError, NextDNS
from .schedule import ScheduleError
from .state import Store, prune

SYNC_FAILED = "Saved here; NextDNS did not answer, so it will be retried within 5 minutes."


class Focus:
    def __init__(self, cfg, api=None, runner=None, clock=time.time):
        self.cfg = cfg
        self.store = Store(cfg.state_file)
        self._api = api
        self.runner = runner or timers._run
        self.clock = clock

    @property
    def api(self):
        if self._api is None:
            self._api = NextDNS.from_config(self.cfg)
        return self._api

    # ---------------------------------------------------------------- internals

    def _push(self, st: dict, ts: float) -> str | None:
        """Reconcile now and refresh the flip timer. Returns a warning, or None if all went through."""
        warn = None
        try:
            reconcile.run(self.api, st, ts)
        except (ApiError, ScheduleError):
            warn = SYNC_FAILED
        try:
            timers.sync_dropin(self.cfg, st["settings"], self.runner)
        except (OSError, ScheduleError):
            warn = warn or "Saved; the flip timer could not be rewritten (the 5-minute reconcile still runs)."
        return warn

    def _edit(self, fn, *args, **kw) -> str:
        with self.store.locked() as st:
            ts = self.clock()
            core.apply_due(st, ts)  # anything whose block has ended applies before this edit
            msg = fn(st, *args, ts=ts, **kw)
            prune(st, ts)
            warn = self._push(st, ts)
        return f"{msg} {warn}" if warn else msg

    # ---------------------------------------------------------------- edits (lock rules in core)

    def add(self, domain: str, list_name: str | None = None) -> str:
        return self._edit(core.add_domain, domain, list_name=list_name)

    def list_add(self, name: str) -> str:
        return self._edit(core.add_list, name)

    def list_remove(self, name: str) -> str:
        return self._edit(core.remove_list, name)

    def category(self, cid: str, on: bool) -> str:
        return self._edit(core.set_category, cid, on)

    def remove(self, domain: str) -> str:
        return self._edit(core.remove_domain, domain)

    def hours(self, list_name: str | None, changes: dict) -> str:
        return self._edit(core.set_hours, list_name, changes)

    def timezone(self, tz: str) -> str:
        return self._edit(core.set_timezone, tz)

    def limits(self, passes=None, minutes=None) -> str:
        return self._edit(core.set_limits, passes=passes, minutes=minutes)

    def undo(self, pid: str) -> str:
        """Cancel a queued change. It never loosened anything yet, so this is always allowed."""
        return self._edit(core.undo, pid)

    # ---------------------------------------------------------------- actions

    def take_pass(self, minutes: int | None = None) -> str:
        with self.store.locked() as st:
            ts = self.clock()
            until = core.take_pass(st, ts, minutes)
            try:
                reconcile.run(self.api, st, ts)
            except (ApiError, ScheduleError) as e:
                # The pass did not go through, so it is not spent. Put back whatever did change.
                st["passes"] = st["passes"][:-1]
                st["pass_until"] = 0
                try:
                    reconcile.run(self.api, st, ts)
                except (ApiError, ScheduleError):
                    pass  # the 5-minute reconcile keeps trying
                raise core.Refused(f"NextDNS did not take the pass ({e}); no pass used.") from None
            prune(st, ts)
            msg = f"Pass on: open until {core.when(st, until, ts)}. {core.passes_left(st, ts)} left today."
        code, err = timers.schedule_reconcile_at(self.cfg, until, self.runner, ts)
        if code:
            msg += " (Restore timer failed; the 5-minute reconcile will close it.)"
        return msg

    def block(self, minutes: int = core.BLOCK_NOW_MIN, until_hhmm: str | None = None) -> str:
        """Block now for `minutes` (default 90), or until HH:MM today. Never shortens a block."""
        with self.store.locked() as st:
            ts = self.clock()
            if until_hhmm:
                until = core.block_until(st, until_hhmm, ts)
            else:
                until = core.block_now(st, ts, minutes)
            warn = self._push(st, ts)
            end = core.unlock_at(st, ts) or until  # the scheduled block may run past it
            today = core._local_date(st, end) == core._local_date(st, ts)
            msg = f"Blocked until {core.when(st, end, ts)}{' (today)' if today else ''}."
        timers.schedule_reconcile_at(self.cfg, until, self.runner, ts)
        return f"{msg} {warn}" if warn else msg

    def reconcile(self, dry_run: bool = False) -> list[str]:
        with self.store.locked() as st:
            ts = self.clock()
            changes = reconcile.run(self.api, st, ts, dry_run=dry_run)
            if not dry_run:
                self._local_count(st)
                prune(st, ts)
                try:
                    if timers.sync_dropin(self.cfg, st["settings"], self.runner):
                        changes = changes + ["rewrote the flip timer from the hours"]
                except OSError as e:
                    changes = changes + [f"could not rewrite the flip timer: {e.strerror}"]
        return changes

    def refresh_usage(self) -> dict | None:
        with self.store.locked() as st:
            reconcile.refresh_usage(self.api, st, self.clock(), force=True)
            self._local_count(st)
            return st["usage"]

    def _local_count(self, st: dict) -> None:
        """NextDNS's own monthly count shrinks to an hour once its logs do; the local count does not."""
        u = st.get("usage")
        if u:
            try:
                u["queries"] = max(int(u.get("queries", 0)), usage.month_total(self.cfg, st["settings"], self.clock()))
            except Exception:  # the count is a convenience; never let it break a reconcile
                pass

    def collect_usage(self) -> str:
        st = self.state()
        return usage.collect(self.cfg, self.api, st["settings"], self.clock())

    def logs_retention(self, seconds: int | None = None) -> dict:
        if seconds is not None:
            self.api.patch_logs_settings({"retention": seconds})
        return self.api.logs_settings()

    def state(self) -> dict:
        return self.store.read()

    def insights(self, settings: dict | None = None) -> dict:
        """Analytics for the page, cached for a few minutes; {"ok": False} on any error."""
        def factory():
            return self._api if self._api is not None else NextDNS.from_config(self.cfg, timeout=insights.TIMEOUT)
        if usage.has_data(self.cfg):
            local = lambda _api, s, ts: usage.insights(self.cfg, s, ts)  # noqa: E731
            return insights.get(self.cfg, factory, settings or self.state()["settings"], self.clock(), fetcher=local)
        return insights.get(self.cfg, factory, settings or self.state()["settings"], self.clock())
