"""NextDNS API client (urllib only). See docs/api-notes.md for what was verified.

The key is sent only in the X-Api-Key header and never appears in errors or logs.
Cloudflare in front of api.nextdns.io answers 403 to urllib's default User-Agent, so one is set.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.nextdns.io"
DOH = "https://dns.nextdns.io"
UA = "focus/1 (+local)"


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class NextDNS:
    def __init__(self, profile: str, key: str, timeout: float = 20):
        if not profile or not key:
            raise ApiError("NEXTDNS_PROFILE / NEXTDNS_API_KEY missing")
        self.profile = profile
        self._key = key
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg, timeout: float = 20) -> "NextDNS":
        try:
            env = cfg.read_env()
        except OSError as e:
            raise ApiError(f"cannot read {cfg.env_file}: {e.strerror}") from None
        return cls(env.get("NEXTDNS_PROFILE", ""), env.get("NEXTDNS_API_KEY", ""), timeout)

    def __repr__(self):
        return f"NextDNS(profile={self.profile!r})"

    def _call(self, method: str, path: str, body=None):
        url = f"{BASE}/profiles/{urllib.parse.quote(self.profile)}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Api-Key": self._key, "User-Agent": UA, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                errs = json.loads(e.read() or b"{}").get("errors") or []
                detail = ", ".join(str(x.get("code", x)) for x in errs)[:200]
            except (ValueError, AttributeError):
                pass
            raise ApiError(f"{method} {path.split('?')[0]}: HTTP {e.code}{' ' + detail if detail else ''}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(f"{method} {path.split('?')[0]}: {getattr(e, 'reason', e)}") from None
        if not raw:
            return None
        try:
            out = json.loads(raw)
        except ValueError:
            raise ApiError(f"{method} {path}: not JSON") from None
        if isinstance(out, dict) and out.get("errors"):
            raise ApiError(f"{method} {path}: {out['errors']}"[:300])
        return out

    # reads
    def profile_data(self) -> dict:
        return (self._call("GET", "") or {}).get("data") or {}

    def parental(self) -> dict:
        return (self._call("GET", "/parentalControl") or {}).get("data") or {}

    def status_counts(self, since_iso: str) -> list[dict]:
        q = urllib.parse.urlencode({"from": since_iso})
        return (self._call("GET", f"/analytics/status?{q}") or {}).get("data") or []

    def analytics(self, kind: str, params: dict) -> dict:
        """GET /analytics/<kind>?params; the whole answer ({"data", "meta"}). kind: status, domains,
        reasons, devices, or status;series."""
        q = urllib.parse.urlencode(params)
        return self._call("GET", f"/analytics/{kind}?{q}") or {}

    def logs(self, params: dict) -> dict:
        """GET /logs?params; the whole answer ({"data", "meta": {"pagination": {"cursor"}}})."""
        q = urllib.parse.urlencode(params)
        return self._call("GET", f"/logs?{q}") or {}

    def logs_settings(self) -> dict:
        return (self._call("GET", "/settings/logs") or {}).get("data") or {}

    def patch_logs_settings(self, body: dict) -> None:
        self._call("PATCH", "/settings/logs", body)

    # writes (204, empty body)
    def patch_parental(self, body: dict) -> None:
        self._call("PATCH", "/parentalControl", body)

    def add_service(self, sid: str, body: dict) -> None:
        self._call("POST", "/parentalControl/services", {"id": sid, **body})

    def patch_service(self, sid: str, body: dict) -> None:
        self._call("PATCH", f"/parentalControl/services/{urllib.parse.quote(sid)}", body)

    def add_category(self, cid: str, body: dict) -> None:
        self._call("POST", "/parentalControl/categories", {"id": cid, **body})

    def patch_category(self, cid: str, body: dict) -> None:
        self._call("PATCH", f"/parentalControl/categories/{urllib.parse.quote(cid)}", body)

    def add_deny(self, domain: str, body: dict) -> None:
        self._call("POST", "/denylist", {"id": domain, **body})

    def patch_deny(self, domain: str, body: dict) -> None:
        self._call("PATCH", f"/denylist/{urllib.parse.quote(domain)}", body)

    def delete_deny(self, domain: str) -> None:
        try:
            self._call("DELETE", f"/denylist/{urllib.parse.quote(domain)}")
        except ApiError as e:
            if e.status != 404:
                raise

    # the resolver, as a device on this profile would see it
    def resolve(self, name: str) -> list[str]:
        q = urllib.parse.urlencode({"name": name, "type": "A"})
        req = urllib.request.Request(f"{DOH}/{urllib.parse.quote(self.profile)}?{q}",
                                     headers={"accept": "application/dns-json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ApiError(f"DoH {name}: {getattr(e, 'reason', e)}") from None
        return [a.get("data", "") for a in data.get("Answer") or [] if a.get("type") == 1]
