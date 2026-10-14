"""Make NextDNS match the local state. PATCH only what differs; verify by re-reading.

What is asserted every run (docs/api-notes.md, findings 2 and 3):
  - parentalControl.recreation: one coarse window per day (the earliest open to the latest close across
    the lists that use services) + timezone, validated as HH:MM before writing. It is the floor that
    holds if the server is down; the timers enforce every edge inside it.
  - parentalControl.blockBypass: true
  - each service: active (false only during a pass) and recreation (true, except during Block now and
    in a gap: a moment the service's own list is blocked while the coarse window is open)
  - each always-blocked category: active as set, recreation false (a pass never touches them)
  - each denylist domain: active = its list blocked right now (denylist ignores Recreation Time)
  - domains removed through focus: deleted from the denylist
Entries added in NextDNS by hand are left alone.

Before any of that, a real (not dry) run applies queued loosening edits whose block has ended
(core.apply_due) and moves the rest onto the current unlock time. Nothing queued applies earlier.
"""
from __future__ import annotations

import time

from . import core
from . import schedule as sch
from .api import ApiError

USAGE_MAX_AGE = 30 * 60


def coarse_hours(st: dict) -> dict:
    """Per day, the span from the earliest open to the latest close across the lists that use
    services (all lists if none do). This is what NextDNS Recreation Time holds."""
    ls = [L for L in core.lists(st) if L["services"]] or core.lists(st)
    return {d: sch.span([L["hours"][d] for L in ls]) for d in sch.DAYS}


def coarse(st: dict) -> dict:
    return {"timezone": st["settings"]["timezone"], "hours": coarse_hours(st)}


def desired(st: dict, ts: float) -> dict:
    for L in core.lists(st):
        sch.validate_hours(L["hours"])  # raises ScheduleError before anything is written
    c = coarse(st)
    rec = sch.to_recreation(c)
    coarse_open = not sch.sched_blocked(c, ts)
    passing = core.pass_active(st, ts)
    services, deny = {}, {}
    for L in core.lists(st):
        gap = core.list_sched_blocked(st, L, ts) and coarse_open
        for sid in L["services"]:
            services[sid] = {"active": not passing,
                             "recreation": not (core.block_now_active(st, ts) or gap)}
        for d in L["domains"]:
            deny[d] = {"active": core.list_blocked(st, L, ts)}
    return {
        "recreation": rec,
        "blockBypass": True,
        "services": services,
        "categories": {c["id"]: {"active": bool(c["active"]), "recreation": False} for c in core.categories(st)},
        "denylist": deny,
        "remove": [d for d in st.get("pending_removals", []) if d not in deny],
    }


def plan(remote: dict, st: dict, ts: float) -> list[tuple]:
    """remote = GET /profiles/{id} data. Returns ops: (kind, target, body, words)."""
    want = desired(st, ts)
    pc = remote.get("parentalControl") or {}
    ops: list[tuple] = []
    if sch.normalize_remote_recreation(pc.get("recreation")) != want["recreation"]:
        ops.append(("schedule", None, {"recreation": want["recreation"]}, "write the weekly hours"))
    if pc.get("blockBypass") is not True:
        ops.append(("parental", None, {"blockBypass": True}, "turn block-bypass protection back on"))
    have_svc = {x.get("id"): x for x in pc.get("services") or []}
    for sid, body in want["services"].items():
        cur = have_svc.get(sid)
        if cur is None:
            ops.append(("service_add", sid, body, f"add service {sid}"))
            continue
        diff = {k: v for k, v in body.items() if bool(cur.get(k)) != v}
        if diff:
            # Always send both fields: a PATCH of only `active` resets `recreation` to false (seen 2026-10-13).
            ops.append(("service", sid, dict(body), f"{sid}: " + ", ".join(f"{k}={v}" for k, v in diff.items())))
    have_cat = {x.get("id"): x for x in pc.get("categories") or []}
    for cid, body in want["categories"].items():
        cur = have_cat.get(cid)
        if cur is None:
            ops.append(("category_add", cid, body, f"add category {cid}"))
            continue
        diff = {k: v for k, v in body.items() if bool(cur.get(k)) != v}
        if diff:
            # Both fields every time, as for services.
            ops.append(("category", cid, dict(body), f"{cid}: " + ", ".join(f"{k}={v}" for k, v in diff.items())))
    have_deny = {x.get("id"): x for x in remote.get("denylist") or []}
    for d, body in want["denylist"].items():
        cur = have_deny.get(d)
        if cur is None:
            ops.append(("deny_add", d, body, f"add {d} ({'blocked' if body['active'] else 'open'})"))
        elif bool(cur.get("active")) != body["active"]:
            ops.append(("deny", d, body, f"{d}: {'block' if body['active'] else 'open'}"))
    for d in want["remove"]:
        if d in have_deny:
            ops.append(("deny_delete", d, None, f"remove {d}"))
    return ops


def apply(api, ops: list[tuple]) -> None:
    for kind, target, body, _ in ops:
        if kind == "schedule":
            api.patch_parental(body)
            got = sch.normalize_remote_recreation(api.parental().get("recreation"))
            if got != body["recreation"]:
                raise ApiError("schedule write did not stick (re-read differs)")
        elif kind == "parental":
            api.patch_parental(body)
        elif kind == "service_add":
            api.add_service(target, body)
        elif kind == "service":
            api.patch_service(target, body)
        elif kind == "category_add":
            api.add_category(target, body)
        elif kind == "category":
            api.patch_category(target, body)
        elif kind == "deny_add":
            api.add_deny(target, body)
        elif kind == "deny":
            api.patch_deny(target, body)
        elif kind == "deny_delete":
            api.delete_deny(target)


def month_start_iso(settings: dict, ts: float) -> tuple[str, str]:
    t = sch.local(settings, ts)
    first = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.strftime("%Y-%m"), first.isoformat()


def refresh_usage(api, st: dict, ts: float, force: bool = False) -> None:
    month, since = month_start_iso(st["settings"], ts)
    u = st.get("usage") or {}
    if not force and u.get("month") == month and ts - u.get("at", 0) < USAGE_MAX_AGE:
        return
    rows = api.status_counts(since)
    st["usage"] = {"at": ts, "month": month, "queries": sum(int(r.get("queries", 0)) for r in rows),
                   "by_status": {str(r.get("status")): int(r.get("queries", 0)) for r in rows}}


def run(api, st: dict, ts: float | None = None, dry_run: bool = False) -> list[str]:
    """One reconcile pass over a locked state dict. Records the outcome in st['last_reconcile']."""
    ts = time.time() if ts is None else ts
    applied: list[str] = []
    try:
        if not dry_run:
            applied = core.apply_due(st, ts)
            core.refresh_pending(st, ts)
        remote = api.profile_data()
        ops = plan(remote, st, ts)
        words = [o[3] for o in ops]
        if dry_run:
            return words
        apply(api, ops)
        if ops:
            left = plan(api.profile_data(), st, ts)
            if left:
                raise ApiError("still differs after writing: " + "; ".join(o[3] for o in left))
        removed = {o[1] for o in ops if o[0] == "deny_delete"}
        have = {x.get("id") for x in remote.get("denylist") or []}
        st["pending_removals"] = [d for d in st.get("pending_removals", []) if d not in removed and d in have]
        words = applied + words
        st["last_reconcile"] = {"at": ts, "ok": True, "changes": words, "error": None}
        try:
            refresh_usage(api, st, ts)
        except ApiError:
            pass
        return words
    except (ApiError, sch.ScheduleError) as e:
        if not dry_run:
            st["last_reconcile"] = {"at": ts, "ok": False, "changes": [], "error": str(e)[:300]}
        raise


