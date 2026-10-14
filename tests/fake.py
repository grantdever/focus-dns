"""An in-memory NextDNS that behaves like the real one did on 2026-10-13 (docs/api-notes.md)."""
import copy
import json
import re
from pathlib import Path

from focus.api import ApiError

FIX = Path(__file__).parent / "fixtures"
ANALYTICS = json.loads((Path(__file__).parent / "fixtures" / "analytics.json").read_text())
TIME = re.compile(r"^\d\d:\d\d(:\d\d)?$")


def seeded_profile() -> dict:
    return json.loads((FIX / "profile.json").read_text())


class FakeApi:
    profile = "fake01"

    def __init__(self, data: dict | None = None):
        self.data = copy.deepcopy(data or seeded_profile())
        self.writes: list[tuple] = []
        self.down = False
        self.reads = 0
        self.analytics_calls: list[tuple] = []

    def _check(self):
        if self.down:
            raise ApiError("GET : <urlopen error offline>")

    def profile_data(self):
        self._check()
        self.reads += 1
        return copy.deepcopy({k: v for k, v in self.data.items() if not k.startswith("_")})

    def parental(self):
        self._check()
        return copy.deepcopy(self.data["parentalControl"])

    def status_counts(self, since):
        self._check()
        return copy.deepcopy(self.data.get("_analytics_status", []))

    def patch_parental(self, body):
        self._check()
        self.writes.append(("patch_parental", body))
        pc = self.data["parentalControl"]
        if "recreation" in body:
            rec = body["recreation"]
            times = rec.get("times", {})
            if not all(isinstance(v, dict) and all(isinstance(v.get(k), str) and TIME.match(v[k]) for k in ("start", "end"))
                       for v in times.values()):
                pc["recreation"]["times"] = {}  # the real API wipes the schedule on a bad write
                raise ApiError("PATCH /parentalControl: HTTP 500", 500)
            pc["recreation"] = {"timezone": rec["timezone"],
                                "times": {d: {k: (t[k] + ":00" if len(t[k]) == 5 else t[k]) for k in ("start", "end")}
                                          for d, t in times.items()}}
        for k, v in body.items():
            if k != "recreation":
                pc[k] = v

    def _svc(self, sid):
        return next((s for s in self.data["parentalControl"]["services"] if s["id"] == sid), None)

    def add_service(self, sid, body):
        self._check()
        self.writes.append(("add_service", sid, body))
        self.data["parentalControl"]["services"].append({"id": sid, **body})

    def patch_service(self, sid, body):
        self._check()
        self.writes.append(("patch_service", sid, body))
        # Like the real API on 2026-10-13: a field left out of a service PATCH falls back to false.
        self._svc(sid).update({"active": bool(body.get("active")), "recreation": bool(body.get("recreation"))})

    def _deny(self, d):
        return next((x for x in self.data["denylist"] if x["id"] == d), None)

    def add_deny(self, d, body):
        self._check()
        self.writes.append(("add_deny", d, body))
        self.data["denylist"].append({"id": d, **body})

    def patch_deny(self, d, body):
        self._check()
        self.writes.append(("patch_deny", d, body))
        self._deny(d).update(body)

    def delete_deny(self, d):
        self._check()
        self.writes.append(("delete_deny", d))
        self.data["denylist"] = [x for x in self.data["denylist"] if x["id"] != d]

    def _cat(self, cid):
        return next((c for c in self.data["parentalControl"]["categories"] if c["id"] == cid), None)

    def add_category(self, cid, body):
        self._check()
        self.writes.append(("add_category", cid, body))
        self.data["parentalControl"]["categories"].append({"id": cid, **body})

    def patch_category(self, cid, body):
        self._check()
        self.writes.append(("patch_category", cid, body))
        # Assumed to behave like a service PATCH (fields left out fall back to false); focus sends both.
        self._cat(cid).update({"active": bool(body.get("active")), "recreation": bool(body.get("recreation"))})

    def analytics(self, kind, params):
        self._check()
        self.analytics_calls.append((kind, dict(params)))
        return copy.deepcopy(ANALYTICS[kind])

    # logs: self.log is a list of entries; pages of `limit`, cursor = next index
    log: list = []
    logs_settings_data = {"enabled": True, "retention": 604800, "location": "ch"}

    def logs(self, params):
        self._check()
        self.analytics_calls.append(("logs", dict(params)))
        from focus.usage import _epoch
        since = _epoch(params["from"]) if "from" in params else 0
        rows = sorted((e for e in self.log if _epoch(e["timestamp"]) >= since), key=lambda e: e["timestamp"])
        i = int(params.get("cursor") or 0)
        n = int(params.get("limit", 100))
        page = rows[i:i + n]
        return {"data": copy.deepcopy(page), "meta": {"pagination": {"cursor": str(i + n) if i + n < len(rows) else None}}}

    def logs_settings(self):
        return dict(self.logs_settings_data)

    def patch_logs_settings(self, body):
        self.writes.append(("patch_logs_settings", body))
        self.logs_settings_data.update(body)

    def resolve(self, name):
        return ["0.0.0.0"]
