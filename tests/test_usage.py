"""Local usage: sessions from lookups, what is (not) stored, gaps, paging, the week report, Insights."""
import contextlib
import datetime as dt
import io
import sqlite3
from unittest import mock

from focus import cli, usage

from .helpers import AppCase, T, TUE_EVENING

DEV = {"id": "d1", "name": "phone", "model": "iOS"}
LAPTOP = {"id": "d2", "name": "laptop", "model": "windows"}
SERVER = {"id": "d3", "name": "server", "model": "linux"}


def e(ts, domain, root=None, dev=DEV, status="default", reasons=(), **kw):
    return {"timestamp": usage._iso(ts), "domain": domain, "root": root or domain, "device": dev,
            "status": status, "reasons": list(reasons), "clientIp": "203.0.113.9", **kw}


def visit(start, minutes, domain="api.x.com", root="x.com", dev=DEV, every=60):
    return [e(start + i, domain, root, dev) for i in range(0, int(minutes * 60) + 1, every)]


class UsageCase(AppCase):
    start = TUE_EVENING

    def setUp(self):
        super().setUp()
        self.api.log = []

    def collect(self, at=None):
        if at is not None:
            self.clock.t = at
        return self.app.collect_usage()

    def db(self):
        return sqlite3.connect(usage.db_path(self.cfg))


class TestSessions(UsageCase):
    def test_a_visit_becomes_one_session_with_estimated_minutes(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 53) + [e(t0 + 120, "pbs.twimg.com", "twimg.com")]
        self.collect(T(2026, 10, 13, 19, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        site, minutes, n, devs = s["sites"][0]
        self.assertEqual((site, n), ("x.com", 1))  # twimg folded into x.com
        self.assertAlmostEqual(minutes, 54, delta=0.1)  # 53 min of lookups + 1 min tail
        self.assertEqual(s["devices"][0][0], "phone")

    def test_a_gap_over_five_minutes_starts_a_new_session(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 10) + visit(t0 + 20 * 60, 10)
        self.collect(T(2026, 10, 13, 19, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        self.assertEqual(s["sites"][0][2], 2)

    def test_sessions_carry_across_collects(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 10)
        self.collect(t0 + 10 * 60 + 30)
        self.api.log += visit(t0 + 12 * 60, 10)
        self.collect(t0 + 30 * 60)
        with self.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_lookups_are_not_counted_twice(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 5)
        self.collect(t0 + 400)
        self.collect(t0 + 700)  # same lookups come back from `from=last`
        with self.db() as con:
            self.assertEqual(con.execute("SELECT SUM(total) FROM daily").fetchone()[0], 6)

    def test_a_stray_lookup_is_not_a_visit(self):
        self.api.log = [e(T(2026, 10, 13, 18, 0), "www.nytimes.com", "nytimes.com")]
        self.collect(T(2026, 10, 13, 19, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        self.assertEqual(s["sites"], [])

    def test_noise_and_excluded_devices_make_no_sessions_but_are_counted(self):
        patch = mock.patch.object(usage, "EXCLUDE_DEVICES", {"server"})
        patch.start()
        self.addCleanup(patch.stop)
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = (visit(t0, 10, "gateway.icloud.com", "icloud.com") + visit(t0, 10, dev=SERVER)
                        + visit(t0, 10, "sdk.appsflyersdk.com", "appsflyersdk.com"))
        self.collect(T(2026, 10, 13, 19, 0))
        with self.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT SUM(total) FROM daily").fetchone()[0], 33)

    def test_a_known_site_on_a_noisy_host_is_still_that_site(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 5, "youtubei.googleapis.com", "googleapis.com") + [
            e(t0, "youtubei.googleapis.com", "googleapis.com", status="blocked", reasons=[{"id": "service:youtube", "name": "YouTube"}]),
            e(t0, "analyticsnew.overwolf.com", "overwolf.com", status="blocked",
              reasons=[{"id": "blocklist:nextdns-recommended", "name": "NextDNS Ads & Trackers Blocklist"}])]
        self.api.log = [x | {"tracker": "youtube"} for x in self.api.log]  # a company label, not "background"
        self.collect(T(2026, 10, 13, 19, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        self.assertEqual(s["sites"][0][0], "youtube.com")
        self.assertEqual(dict(s["blocked"]), {"youtube.com": 1, "(background)": 1})

    def test_overlapping_sites_count_once_toward_a_device(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 30) + visit(t0, 30, "www.youtube.com", "youtube.com")
        self.collect(T(2026, 10, 13, 19, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        self.assertAlmostEqual(dict(s["devices"])["phone"], 31, delta=0.1)


class TestPrivacy(UsageCase):
    def test_no_subdomains_ips_or_category_domains_are_stored(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 5, "secret.sub.example.org", "example.org") + [
            e(t0, "www.badsite.example", status="blocked", reasons=[{"id": "porn", "name": "Porn"}]),
            e(t0, "api.x.com", "x.com", status="blocked", reasons=[{"id": "denylist", "name": "Denylist"},
                                                                    {"id": "service:twitter", "name": "X"}])]
        self.collect(T(2026, 10, 13, 19, 0))
        dump = "\n".join(usage.db_path(self.cfg).parent.joinpath("usage.sqlite").read_bytes().decode("latin-1")
                         .split("\x00"))
        with self.db() as con:
            text = "\n".join(con.iterdump())
        for leaked in ("secret.sub", "203.0.113.9", "badsite"):
            self.assertNotIn(leaked, text)
            self.assertNotIn(leaked, dump)
        self.assertIn("(Porn)", text)
        with self.db() as con:
            self.assertEqual(con.execute("SELECT reason FROM blocked WHERE site = 'x.com'").fetchone()[0], "X")

    def test_file_is_private_and_old_rows_are_deleted(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 5)
        self.collect(T(2026, 10, 13, 19, 0))
        self.assertEqual(usage.db_path(self.cfg).stat().st_mode & 0o777, 0o600)
        self.api.log = []
        self.collect(t0 + 15 * 86400)
        with self.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 1)  # kept 35 days
        self.collect(t0 + 36 * 86400)
        with self.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 0)


class TestCollect(UsageCase):
    def test_pages_through_and_asks_for_raw_logs_from_the_last_lookup(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 40, every=1)  # 2,401 lookups: three pages
        self.assertEqual(self.collect(T(2026, 10, 13, 19, 0)), "2401 new lookups")
        calls = [p for k, p in self.api.analytics_calls if k == "logs"]
        self.assertEqual(len(calls), 3)
        self.assertEqual((calls[0]["raw"], calls[0]["sort"]), (1, "asc"))
        self.collect(T(2026, 10, 13, 19, 5))
        self.assertEqual([p for k, p in self.api.analytics_calls if k == "logs"][-1]["from"], self.api.log[-1]["timestamp"])

    def test_a_long_outage_is_recorded_as_a_gap(self):
        self.collect(T(2026, 10, 13, 18, 0))
        self.collect(T(2026, 10, 13, 22, 0))
        s = usage.summary(self.cfg, self.st()["settings"], dt.date(2026, 10, 12))
        self.assertAlmostEqual(s["gap_hours"], 3.0, delta=0.01)
        self.assertIn("Missing: about 3 h", usage.render_week(self.cfg, self.st()["settings"], dt.date(2026, 10, 12)))


class TestReading(UsageCase):
    def test_week_report_and_cli(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 53) + visit(t0, 20, dev=LAPTOP)
        self.collect(T(2026, 10, 13, 19, 0))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["usage"], app=self.app), 0)
        text = out.getvalue()
        self.assertIn("phone: 54m", text)
        self.assertIn("x.com *: 1h 15m over 2 visits (phone 54m, laptop 21m)", text)

    def test_insights_come_from_the_local_counts(self):
        t0 = T(2026, 10, 13, 18, 0)
        self.api.log = visit(t0, 5) + [e(t0, "api.x.com", "x.com", status="blocked",
                                         reasons=[{"id": "service:twitter", "name": "X"}])]
        self.collect(T(2026, 10, 13, 19, 0))
        n = len(self.api.analytics_calls)
        ins = self.app.insights()
        self.assertEqual(len(self.api.analytics_calls), n)  # no NextDNS analytics call
        self.assertEqual(ins["days"][-1], {"date": "2026-10-13", "queries": 7, "blocked": 1})
        self.assertEqual(ins["top_blocked"], [{"domain": "x.com", "queries": 1}])
        self.assertEqual(ins["devices"], [{"name": "phone", "queries": 7}])

    def test_monthly_count_never_drops_below_the_local_one(self):
        self.api.log = visit(T(2026, 10, 13, 18, 0), 5)
        self.collect(T(2026, 10, 13, 19, 0))
        self.api.data["_analytics_status"] = [{"status": "default", "queries": 2}]
        self.assertEqual(self.app.refresh_usage()["queries"], 6)

    def test_retention_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["usage", "retention", "1h"], app=self.app)
        self.assertIn(("patch_logs_settings", {"retention": 3600}), self.api.writes)
        self.assertIn("NextDNS keeps logs for 1h (logging on, stored in CH).", out.getvalue())
