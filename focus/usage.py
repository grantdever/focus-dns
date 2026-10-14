"""Usage kept on the server, not at NextDNS: estimated time per site per device, and lookup counts.

NextDNS keeps its logs for one hour (`focus usage retention` sets that). Every 5 minutes `collect` pulls
the new lookups and folds them straight into a small SQLite file in the state folder. The raw lookups
are never written down: what is kept is

- sessions: device, site (the registrable domain, CDNs folded into their site), first and last lookup,
  and a count. A gap of GAP_SEC with no lookups ends a session. Only allowed lookups from the user's own
  devices make sessions; background hosts (NOISE: OS, CDN, analytics SDKs) do not.
- daily: lookups and blocked lookups per local day per device (the free-tier count and the chart).
- blocked: blocked lookups per day per site and rule. Category blocks (porn, gambling) are stored under
  the category name only, never the domain; ad and tracker blocklist hits as "(background)".
- gaps: stretches the collector missed (the server off for more than the hour NextDNS keeps).

No subdomains, IP addresses or full URLs are stored. Sessions are deleted after KEEP_SESSION_DAYS, the
daily and blocked counts after KEEP_DAYS; the weekly report (a few lines of text) is what lasts.

DNS gives the moment a name was looked up, never how long a page stayed open, so minutes are an
estimate: a session counts from its first lookup to its last plus TAIL_SEC, and only once it has
MIN_LOOKUPS lookups (one stray lookup is a check, not a visit). Devices cache answers and apps look
things up in the background, so expect minutes to be right to within a few, not to the second.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import schedule as sch

GAP_SEC = 5 * 60
TAIL_SEC = 60
MIN_LOOKUPS = 3
KEEP_SESSION_DAYS = 14
KEEP_DAYS = 35
NEXTDNS_RETENTION_SEC = 3600
PAGE_LIMIT = 1000
MAX_PAGES = 200  # 200,000 lookups in one pull is far past an hour of this household's traffic

# Devices that are machines, not people (say the server running focus, or a dev box full of agents): counted
# for the cap, no sessions. Set FOCUS_EXCLUDE_DEVICES (comma-separated NextDNS device names) in the env file;
# Config.exclude_devices reads it. This module default is for tests.
EXCLUDE_DEVICES: set[str] = set()

# Hosts and CDNs folded into the site a person would name.
ALIASES = {
    "twitter.com": "x.com", "twimg.com": "x.com", "t.co": "x.com", "twttr.com": "x.com",
    "youtube.com": "youtube.com", "ytimg.com": "youtube.com", "googlevideo.com": "youtube.com",
    "youtu.be": "youtube.com", "youtube-nocookie.com": "youtube.com", "youtubei.googleapis.com": "youtube.com",
    "ggpht.com": "youtube.com",
    "fbcdn.net": "facebook.com", "facebook.net": "facebook.com", "fb.com": "facebook.com",
    "cdninstagram.com": "instagram.com",
    "redditmedia.com": "reddit.com", "redditstatic.com": "reddit.com", "redd.it": "reddit.com",
    "licdn.com": "linkedin.com",
    "nflxvideo.net": "netflix.com", "nflximg.net": "netflix.com", "nflxext.com": "netflix.com",
    "nflxso.net": "netflix.com",
    "tiktokcdn.com": "tiktok.com", "tiktokv.com": "tiktok.com", "tiktokcdn-us.com": "tiktok.com",
    "byteoversea.com": "tiktok.com", "ibytedtos.com": "tiktok.com",
    "scdn.co": "spotify.com", "spotifycdn.com": "spotify.com",
    "substackcdn.com": "substack.com",
    "discordapp.com": "discord.com", "discordapp.net": "discord.com", "discord.gg": "discord.com",
    "twitchcdn.net": "twitch.tv", "jtvnw.net": "twitch.tv", "ttvnw.net": "twitch.tv",
    "bsky.network": "bsky.app", "bsky.social": "bsky.app",
}

# Background traffic every device makes whether or not anyone is using it.
NOISE = {
    "apple.com", "icloud.com", "icloud-content.com", "apple-dns.net", "aaplimg.com", "akadns.net",
    "com.akadns.net", "akamaiedge.net", "akamai.net", "edgekey.net", "edgesuite.net", "apple-cloudkit.com",
    "push-apple.com.akadns.net", "me.com", "mzstatic.com", "cdn-apple.com",
    "microsoft.com", "windows.com", "windowsupdate.com", "msftconnecttest.com", "msftncsi.com", "msn.com",
    "live.com", "office.net", "msedge.net", "azureedge.net", "azure.com", "trafficmanager.net", "bing.com",
    "skype.com", "microsoftonline.com", "office.com", "visualstudio.com", "vscode-cdn.net",
    "google.com",  # Chrome and Android check in all day; searches are lost with it, a fair trade
    "googleapis.com", "gstatic.com", "gvt1.com", "gvt2.com", "1e100.net", "googleusercontent.com",
    "google-analytics.com", "googletagmanager.com", "googlesyndication.com", "doubleclick.net",
    "app-measurement.com", "crashlytics.com", "firebaseio.com",
    "tailscale.com", "tailscale.io", "ts.net", "nextdns.io",
    "amazonaws.com", "cloudfront.net", "cloudflare.com", "cloudflare-dns.com", "fastly.net", "fastly-edge.com",
    "digicert.com", "letsencrypt.org", "lencr.org", "sectigo.com", "globalsign.com", "pki.goog",
    "datadoghq.com", "sentry.io", "segment.io", "mixpanel.com", "amplitude.com", "branch.io",
    "app-analytics-services.com", "appsflyersdk.com", "appsflyer.com", "nr-data.net", "newrelic.com",
    "kochava.com", "radar.io", "googleadservices.com", "adjust.com", "braze.com", "onesignal.com",
    "bugsnag.com", "instabug.com", "launchdarkly.com", "optimizely.com", "adsrvr.org",
    "anthropic.com", "claude.ai", "statsig.com", "github.com", "githubusercontent.com",
    "local", "arpa", "lan", "home.arpa",
}


def site_of(root: str, domain: str = "") -> str:
    root = (root or domain or "").lower().rstrip(".")
    domain = (domain or root).lower().rstrip(".")
    for host in (domain, root):
        if host in ALIASES:
            return ALIASES[host]
    for key, site in ALIASES.items():
        if domain.endswith("." + key):
            return site
    return root


KNOWN_SITES = set(ALIASES.values())


def is_noise(site: str, domain: str = "") -> bool:
    if site in KNOWN_SITES:  # youtubei.googleapis.com is YouTube, not Google background traffic
        return False
    if not site or site in NOISE:
        return True
    d = (domain or "").lower()
    return any(d == n or d.endswith("." + n) for n in NOISE)


def _epoch(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, device TEXT, site TEXT, start REAL, end REAL,
    lookups INTEGER);
CREATE INDEX IF NOT EXISTS sessions_dev_site ON sessions (device, site, end);
CREATE INDEX IF NOT EXISTS sessions_start ON sessions (start);
CREATE TABLE IF NOT EXISTS daily (day TEXT, device TEXT, total INTEGER, blocked INTEGER, PRIMARY KEY (day, device));
CREATE TABLE IF NOT EXISTS blocked (day TEXT, site TEXT, reason TEXT, n INTEGER, PRIMARY KEY (day, site, reason));
CREATE TABLE IF NOT EXISTS gaps (start REAL, end REAL);
"""


def db_path(cfg) -> Path:
    return Path(cfg.state_dir) / "usage.sqlite"


@contextmanager
def connect(cfg):
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    con = sqlite3.connect(path, timeout=10)
    try:
        con.execute("PRAGMA secure_delete = ON")  # deleted rows are overwritten, not left in free pages
        con.executescript(SCHEMA)
        with con:
            yield con
    finally:
        con.close()


def has_data(cfg) -> bool:
    if not db_path(cfg).exists():
        return False
    with connect(cfg) as con:
        return con.execute("SELECT 1 FROM daily LIMIT 1").fetchone() is not None


def _meta(con, k: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else default


def _set_meta(con, k: str, v) -> None:
    con.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (k, str(v)))


def _key(e: dict) -> str:
    """Identifies a lookup at the edge of the last pull without keeping its name: a short hash."""
    raw = f'{e.get("timestamp")}|{e.get("domain")}|{(e.get("device") or {}).get("id")}|{e.get("type")}'
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _reason(e: dict) -> tuple[str, bool]:
    """The rule that blocked it, and whether it was a category (whose domain is not kept)."""
    reasons = e.get("reasons") or []
    for r in reasons:
        rid = str(r.get("id") or "")
        if rid.startswith("category:") or rid in ("porn", "gambling"):
            return str(r.get("name") or rid), True
    if reasons:
        r = reasons[-1]  # the most specific: the service or list rule after a generic "Denylist"
        return str(r.get("name") or r.get("id") or "?"), False
    return "?", False


def ingest(con, entries: list[dict], settings: dict, exclude: set[str] | None = None) -> int:
    """Fold lookups (any order) into sessions and counts. Returns how many were new."""
    exclude = EXCLUDE_DEVICES if exclude is None else exclude
    edge = float(_meta(con, "last_ts", "0"))
    edge_keys = set(filter(None, (_meta(con, "edge_keys", "") or "").split("\n")))
    rows = []
    for e in entries:
        try:
            t = _epoch(e["timestamp"])
        except (KeyError, ValueError, AttributeError):
            continue
        if t < edge or (t == edge and _key(e) in edge_keys):
            continue
        rows.append((t, e))
    rows.sort(key=lambda r: r[0])
    open_: dict[tuple[str, str], list] = {}
    daily: dict[tuple[str, str], list[int]] = {}
    blocked: dict[tuple[str, str, str], int] = {}
    for t, e in rows:
        dev = (e.get("device") or {}).get("name") or "unnamed"
        day = sch.local(settings, t).date().isoformat()
        is_blocked = e.get("status") == "blocked"
        c = daily.setdefault((day, dev), [0, 0])
        c[0] += 1
        c[1] += int(is_blocked)
        site = site_of(e.get("root", ""), e.get("domain", ""))
        if is_blocked:
            reason, is_cat = _reason(e)
            if is_cat:
                site = f"({reason})"
            elif any(str(r.get("id", "")).startswith("blocklist:") for r in e.get("reasons") or []) \
                    or is_noise(site, e.get("domain", "")):
                site = "(background)"
            k = (day, site, reason)
            blocked[k] = blocked.get(k, 0) + 1
            continue
        # NextDNS's "tracker" field names a company (YouTube has one), so it is not used here.
        if dev in exclude or is_noise(site, e.get("domain", "")):
            continue
        s = open_.get((dev, site))
        if s is None:
            row = con.execute("SELECT id, start, end, lookups FROM sessions WHERE device = ? AND site = ? "
                              "ORDER BY end DESC LIMIT 1", (dev, site)).fetchone()
            s = list(row) if row else None
        if s is not None and t - s[2] <= GAP_SEC:
            s[2] = max(s[2], t)
            s[3] += 1
        else:
            s = [None, t, t, 1]
        open_[(dev, site)] = s
        if s[0] is None:
            s[0] = con.execute("INSERT INTO sessions (device, site, start, end, lookups) VALUES (?, ?, ?, ?, ?)",
                               (dev, site, s[1], s[2], s[3])).lastrowid
        else:
            con.execute("UPDATE sessions SET end = ?, lookups = ? WHERE id = ?", (s[2], s[3], s[0]))
    for (day, dev), (n, b) in daily.items():
        con.execute("INSERT INTO daily VALUES (?, ?, ?, ?) ON CONFLICT (day, device) DO UPDATE SET "
                    "total = total + excluded.total, blocked = blocked + excluded.blocked", (day, dev, n, b))
    for (day, site, reason), n in blocked.items():
        con.execute("INSERT INTO blocked VALUES (?, ?, ?, ?) ON CONFLICT (day, site, reason) DO UPDATE SET "
                    "n = n + excluded.n", (day, site, reason, n))
    if rows:
        last = rows[-1][0]
        keys = {_key(e) for t, e in rows if t == last} | (edge_keys if last == edge else set())
        _set_meta(con, "last_ts", repr(last))
        _set_meta(con, "edge_keys", "\n".join(sorted(keys)))
    return len(rows)


def prune(con, ts: float, settings: dict) -> None:
    con.execute("DELETE FROM sessions WHERE end < ?", (ts - KEEP_SESSION_DAYS * 86400,))
    cutoff = (sch.local(settings, ts).date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    con.execute("DELETE FROM daily WHERE day < ?", (cutoff,))
    con.execute("DELETE FROM blocked WHERE day < ?", (cutoff,))
    con.execute("DELETE FROM gaps WHERE end < ?", (ts - KEEP_DAYS * 86400,))


def collect(cfg, api, settings: dict, ts: float) -> str:
    """Pull every lookup since the last run and fold it in. Notes a gap if more than NextDNS's hour passed."""
    with connect(cfg) as con:
        last_ok = _meta(con, "last_collect")
        since = float(_meta(con, "last_ts", "0"))
        from_ts = since if since else ts - 7 * 86400  # first run: whatever NextDNS still has
        if not _meta(con, "first_collect"):
            _set_meta(con, "first_collect", repr(ts))
        if last_ok and ts - float(last_ok) > NEXTDNS_RETENTION_SEC:
            con.execute("INSERT INTO gaps VALUES (?, ?)", (float(last_ok), ts - NEXTDNS_RETENTION_SEC))
        entries, cursor = [], None
        for _ in range(MAX_PAGES):
            page = api.logs({"from": _iso(from_ts), "sort": "asc", "limit": PAGE_LIMIT, "raw": 1,
                             **({"cursor": cursor} if cursor else {})})
            entries.extend(page.get("data") or [])
            cursor = ((page.get("meta") or {}).get("pagination") or {}).get("cursor")
            if not cursor or not page.get("data"):
                break
        n = ingest(con, entries, settings, getattr(cfg, "exclude_devices", None))
        _set_meta(con, "last_collect", repr(ts))
        prune(con, ts, settings)
    return f"{n} new lookups"


# ----------------------------------------------------------------------------- reading it back

def _merge(intervals: list[tuple[float, float]]) -> float:
    total, cur = 0.0, None
    for a, b in sorted(intervals):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                total += cur[1] - cur[0]
            cur = [a, b]
    if cur:
        total += cur[1] - cur[0]
    return total


def _counted(rows) -> list[tuple[str, str, float, float]]:
    return [(d, s, a, b + TAIL_SEC) for d, s, a, b, n in rows if n >= MIN_LOOKUPS]


def summary(cfg, settings: dict, start: dt.date, days: int = 7) -> dict:
    """Estimated minutes for [start, start + days): per device (overlapping sites counted once), per site,
    and blocked tries per site; plus gaps the collector missed."""
    tz = sch.tz_of(settings)
    a = dt.datetime(start.year, start.month, start.day, tzinfo=tz).timestamp()
    end_day = start + dt.timedelta(days=days)
    b = dt.datetime(end_day.year, end_day.month, end_day.day, tzinfo=tz).timestamp()
    with connect(cfg) as con:
        rows = con.execute("SELECT device, site, start, end, lookups FROM sessions WHERE start < ? AND end >= ?",
                           (b, a)).fetchall()
        blocked = con.execute("SELECT site, SUM(n) FROM blocked WHERE day >= ? AND day < ? GROUP BY site "
                              "ORDER BY 2 DESC", (start.isoformat(), end_day.isoformat())).fetchall()
        gaps = con.execute("SELECT start, end FROM gaps WHERE start < ? AND end > ?", (b, a)).fetchall()
        first = _meta(con, "first_collect")
    sess = [(d, s, max(x, a), min(y, b)) for d, s, x, y in _counted(rows)]
    devices: dict[str, list] = {}
    sites: dict[str, dict] = {}
    for d, s, x, y in sess:
        devices.setdefault(d, []).append((x, y))
        e = sites.setdefault(s, {"sec": 0.0, "sessions": 0, "devices": {}})
        e["sec"] += y - x
        e["sessions"] += 1
        e["devices"][d] = e["devices"].get(d, 0.0) + y - x
    per_day: dict[str, dict[str, list]] = {}
    for d, s, x, y in sess:
        day = sch.local(settings, x).date().isoformat()
        per_day.setdefault(day, {}).setdefault(d, []).append((x, y))
    return {
        "start": start.isoformat(), "days": days,
        "devices": sorted(((d, _merge(iv) / 60) for d, iv in devices.items()), key=lambda r: -r[1]),
        "sites": sorted(((s, v["sec"] / 60, v["sessions"], {d: t / 60 for d, t in v["devices"].items()})
                         for s, v in sites.items()), key=lambda r: -r[1]),
        "by_day": {day: {d: _merge(iv) / 60 for d, iv in devs.items()} for day, devs in sorted(per_day.items())},
        "blocked": [(s, int(n)) for s, n in blocked],
        "gap_hours": sum(min(y, b) - max(x, a) for x, y in gaps) / 3600,
        "since": float(first) if first else None,
    }


def _hm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def render_week(cfg, settings: dict, start: dt.date, watched: set[str] | None = None, top: int = 10) -> str:
    s = summary(cfg, settings, start)
    end = start + dt.timedelta(days=6)
    lines = [f"Estimated time online, {start:%a %b %-d} to {end:%a %b %-d} (from DNS lookups; right to within "
             f"a few minutes per visit, not to the second)."]
    if not s["devices"]:
        lines.append("  No sessions recorded this week.")
    for d, m in s["devices"]:
        lines.append(f"  {d}: {_hm(m)}")
    if s["sites"]:
        lines.append(f"Top sites:")
        for site, m, n, devs in s["sites"][:top]:
            where = ", ".join(f"{d} {_hm(t)}" for d, t in sorted(devs.items(), key=lambda r: -r[1]))
            flag = " *" if watched and site in watched else ""
            lines.append(f"  {site}{flag}: {_hm(m)} over {n} visit{'s' if n != 1 else ''} ({where})")
        if watched and any(x[0] in watched for x in s["sites"][:top]):
            lines.append("  (* on one of your focus lists; time there is time inside open hours or a pass)")
    if s["blocked"]:
        lines.append("Blocked tries: " + ", ".join(f"{site} {n}" for site, n in s["blocked"][:top]) + ".")
    if s["gap_hours"] >= 0.1:
        lines.append(f"Missing: about {s['gap_hours']:.0f} h when the server was off for more than an hour.")
    return "\n".join(lines) + "\n"


def insights(cfg, settings: dict, ts: float, days_back: int = 30, top: int = 5) -> dict:
    """The page's Insights, from the local counts (the shape insights.fetch returns)."""
    today = sch.local(settings, ts).date()
    first = today - dt.timedelta(days=days_back - 1)
    month = today.replace(day=1).isoformat()
    with connect(cfg) as con:
        per = {d: (t, b) for d, t, b in con.execute(
            "SELECT day, SUM(total), SUM(blocked) FROM daily WHERE day >= ? GROUP BY day", (first.isoformat(),))}
        mt, mb = con.execute("SELECT COALESCE(SUM(total), 0), COALESCE(SUM(blocked), 0) FROM daily WHERE day >= ?",
                             (month,)).fetchone()
        top_rows = con.execute("SELECT site, SUM(n) FROM blocked WHERE day >= ? GROUP BY site ORDER BY 2 DESC "
                               "LIMIT ?", (first.isoformat(), top)).fetchall()
        reasons = con.execute("SELECT reason, SUM(n) FROM blocked WHERE day >= ? GROUP BY reason ORDER BY 2 DESC",
                              (first.isoformat(),)).fetchall()
        devs = con.execute("SELECT device, SUM(total) FROM daily WHERE day >= ? GROUP BY device ORDER BY 2 DESC",
                           (first.isoformat(),)).fetchall()
    days = []
    for i in range(days_back):
        d = (first + dt.timedelta(days=i)).isoformat()
        t, b = per.get(d, (0, 0))
        days.append({"date": d, "queries": int(t), "blocked": int(b)})
    monday = today - dt.timedelta(days=today.weekday())
    wk = summary(cfg, settings, monday)
    week = {"start": monday.isoformat(), "devices": [{"name": d, "minutes": round(m)} for d, m in wk["devices"]],
            "sites": [{"site": x, "minutes": round(m), "visits": n} for x, m, n, _ in wk["sites"][:top]]}
    return {"days": days, "month": {"total": int(mt), "blocked": int(mb)}, "week": week,
            "top_blocked": [{"domain": s, "queries": int(n)} for s, n in top_rows],
            "reasons": [{"name": r, "queries": int(n)} for r, n in reasons],
            "devices": [{"name": d, "queries": int(n)} for d, n in devs], "source": "local"}


def month_total(cfg, settings: dict, ts: float) -> int:
    if not db_path(cfg).exists():
        return 0
    month = sch.local(settings, ts).date().replace(day=1).isoformat()
    with connect(cfg) as con:
        return int(con.execute("SELECT COALESCE(SUM(total), 0) FROM daily WHERE day >= ?", (month,)).fetchone()[0])
