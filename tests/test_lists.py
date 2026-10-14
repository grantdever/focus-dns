"""v3: lists with their own hours, several windows a day, always-blocked categories, insights, doctor."""
import json
import unittest

from focus import cli, core, insights, page, reconcile, timers
from focus import schedule as sch
from focus.schedule import ScheduleError
from focus.state import Store

from .fake import FakeApi
from .helpers import AppCase, T, TUE_EVENING, TUE_NOON, fresh, lst, week


class TestWindows(unittest.TestCase):
    def test_parse_and_canonical_form(self):
        n = sch.normalize_rule
        self.assertEqual(n("07:00-09:00,17:00"), "07:00-09:00,17:00-24:00")
        self.assertEqual(n(" 17:00-00:00 , 07:00-09:00 "), "07:00-09:00,17:00-24:00")  # sorted
        self.assertEqual(n("07:00-10:00,09:00-12:00"), "07:00-12:00")                # overlap merged
        self.assertEqual(n("07:00-09:00,09:00-12:00"), "07:00-12:00")                # touching merged
        self.assertEqual(n("00:00-12:00,12:00-24:00"), "open")
        self.assertEqual(n(["07:00-09:00", "17:00-23:59"]), "07:00-09:00,17:00-24:00")
        self.assertEqual(n([]), "blocked")
        self.assertEqual(sch.make_windows([("07:00", "09:00"), ("17:00", "")]), "07:00-09:00,17:00-24:00")
        with self.assertRaises(ScheduleError):
            n(",".join(f"{h:02d}:00-{h:02d}:30" for h in range(7)))  # 7 windows: too many
        with self.assertRaises(ScheduleError):
            sch.make_windows([("09:00", "08:00")])

    def test_blocked_inside_the_gaps(self):
        s = week(tuesday="07:00-09:00,17:00-24:00")
        for hm, blocked in (((6, 59), True), ((7, 0), False), ((8, 59), False), ((9, 0), True),
                            ((16, 59), True), ((17, 0), False), ((23, 59), False)):
            self.assertEqual(sch.sched_blocked(s, T(2026, 10, 13, *hm)), blocked, hm)
        self.assertEqual(sch.next_sched_open(s, T(2026, 10, 13, 10, 0)), T(2026, 10, 13, 17, 0))
        b = sch.boundaries(s, T(2026, 10, 13, 6, 0))
        for hm in ((7, 0), (9, 0), (17, 0)):
            self.assertIn(T(2026, 10, 13, *hm), b)

    def test_adding_or_widening_a_window_loosens_shrinking_or_removing_tightens(self):
        old = "07:00-09:00,17:00-24:00"
        self.assertFalse(sch.is_tighter("07:00-09:00,12:00-13:00,17:00-24:00", old))  # added
        self.assertFalse(sch.is_tighter("07:00-10:00,17:00-24:00", old))              # widened
        self.assertFalse(sch.is_tighter("open", old))
        self.assertTrue(sch.is_tighter("07:30-09:00,17:00-24:00", old))               # shrunk
        self.assertTrue(sch.is_tighter("17:00-24:00", old))                           # removed
        self.assertTrue(sch.is_tighter("blocked", old))
        self.assertEqual(sch.intersect("06:00-08:00,12:00-18:00", old), "07:00-08:00,17:00-18:00")

    def test_coarse_span(self):
        self.assertEqual(sch.span(["07:00-09:00,17:00-24:00", "12:00-22:00"]), "07:00-24:00")
        self.assertEqual(sch.span(["blocked", "10:00-11:00,13:00-14:00"]), "10:00-14:00")
        self.assertEqual(sch.span(["blocked", "blocked"]), "blocked")
        self.assertEqual(sch.span(["open", "17:00"]), "open")
        with self.assertRaises(ScheduleError):  # NextDNS holds one window a day
            sch.to_recreation(week(monday="07:00-09:00,17:00-24:00"))

    def test_summary(self):
        self.assertEqual(sch.summarize_week(week()["hours"]), "Mon–Fri 17:00–00:00 · Sat–Sun all day")
        self.assertEqual(sch.describe_rule("07:00-09:00,17:00-24:00"),
                         "open from 07:00 to 09:00 and from 17:00 to midnight")


class TestListRules(unittest.TestCase):
    def test_seed_matches_the_live_profile(self):
        st = fresh()
        self.assertEqual([(L["name"], L["services"], L["domains"]) for L in core.lists(st)],
                         [("Video", ["youtube"], []), ("Social", ["twitter"], ["x.com"])])
        api = FakeApi()
        self.assertEqual(reconcile.run(api, st, TUE_NOON), [])
        self.assertEqual(reconcile.run(api, st, T(2026, 10, 13, 17, 30)), ["x.com: open"])
        self.assertEqual(reconcile.coarse_hours(st), week()["hours"])

    def test_v2_state_file_migrates_to_the_seed(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        old = {"version": 1, "settings": {"timezone": "America/New_York", "services": ["youtube", "twitter"],
                                          "domains": ["x.com"], "pass_minutes": 15, "passes_per_day": 3,
                                          "hours": {**week()["hours"], "monday": "17:00"}},
               "pending": [], "passes": [], "blocks": [], "pass_until": 0, "block_until": 0}
        (d / "state.json").write_text(json.dumps(old))
        st = Store(d / "state.json").read()
        self.assertEqual(st["version"], 3)
        self.assertNotIn("hours", st["settings"])
        self.assertNotIn("domains", st["settings"])
        self.assertEqual([L["name"] for L in core.lists(st)], ["Video", "Social"])
        self.assertEqual(lst(st, "Social")["domains"], ["x.com"])
        self.assertEqual(lst(st, "Video")["hours"]["monday"], "17:00-24:00")
        self.assertEqual([c["id"] for c in core.categories(st)], ["porn", "gambling"])
        self.assertEqual(reconcile.run(FakeApi(), st, TUE_NOON), [])

    def test_a_domain_belongs_to_one_list(self):
        st = fresh()
        with self.assertRaises(core.Refused) as c:
            core.add_domain(st, "x.com", TUE_EVENING, "Video")
        self.assertIn("x.com is on Social", str(c.exception))
        with self.assertRaises(core.Refused) as c:
            core.add_domain(st, "reddit.com", TUE_EVENING)  # two lists: which one?
        self.assertIn("Which list", str(c.exception))
        with self.assertRaises(core.Refused):
            core.add_domain(st, "reddit.com", TUE_EVENING, "Nope")
        self.assertEqual(core.add_domain(st, "reddit.com", TUE_EVENING, "video"), "Added reddit.com to Video.")
        self.assertEqual(core.all_domains(st), ["reddit.com", "x.com"])

    def test_unlock_is_per_list(self):
        st = fresh()
        lst(st, "Video")["hours"]["tuesday"] = "12:00-24:00"
        ts = T(2026, 10, 13, 11, 0)
        self.assertEqual(core.unlock_at(st, ts, lst(st, "Video")), T(2026, 10, 13, 12, 0))
        self.assertEqual(core.unlock_at(st, ts, lst(st, "Social")), T(2026, 10, 13, 17, 0))
        self.assertEqual(core.unlock_at(st, ts), T(2026, 10, 13, 17, 0))  # no list blocked
        core.add_domain(st, "vimeo.com", ts, "Video")
        self.assertEqual(core.remove_domain(st, "vimeo.com", ts), "Removes vimeo.com at 12:00.")
        self.assertEqual(core.remove_domain(st, "x.com", ts), "Removes x.com at 17:00.")
        self.assertIn("apply at 12:00", core.set_hours(st, "Video", {"wednesday": "09:00-24:00"}, ts))
        done = core.apply_due(st, T(2026, 10, 13, 12, 0))
        self.assertEqual(len(done), 2)
        self.assertEqual(lst(st, "Video")["domains"], [])
        self.assertEqual(lst(st, "Video")["hours"]["wednesday"], "09:00-24:00")
        self.assertEqual([p["id"] for p in st["pending"]], ["remove:x.com"])
        self.assertEqual(core.apply_due(st, T(2026, 10, 13, 16, 59)), [])
        self.assertEqual(len(core.apply_due(st, T(2026, 10, 13, 17, 0))), 1)
        # Video open, Social blocked: Video edits apply at once now.
        st2 = fresh()
        lst(st2, "Video")["hours"]["tuesday"] = "12:00-24:00"
        core.add_domain(st2, "vimeo.com", T(2026, 10, 13, 13, 0), "Video")
        self.assertEqual(core.remove_domain(st2, "vimeo.com", T(2026, 10, 13, 13, 0)), "Removed vimeo.com.")

    def test_a_list_with_no_sites_takes_any_hours_at_once(self):
        st = fresh()
        msg = core.add_list(st, "  Reading  ", TUE_NOON)
        self.assertIn("New list Reading", msg)
        self.assertIn("Reading hours saved", core.set_hours(st, "reading", {"tuesday": "open"}, TUE_NOON))
        self.assertEqual(st["pending"], [])
        core.add_domain(st, "news.ycombinator.com", TUE_NOON, "Reading")
        self.assertIn("apply at", core.set_hours(st, "Reading", {"wednesday": "open"}, T(2026, 10, 14, 12, 0)))
        for bad in ("", "tz", "a" * 30, "bad/name"):
            with self.assertRaises(core.Refused, msg=bad):
                core.add_list(st, bad, TUE_NOON)
        with self.assertRaises(core.Refused):
            core.add_list(st, "video", TUE_NOON)
        with self.assertRaises(core.Refused):
            core.remove_list(st, "Social", TUE_NOON)  # has sites
        core.add_list(st, "Empty", TUE_NOON)
        self.assertIn("gone", core.remove_list(st, "empty", TUE_NOON))

    def test_block_now_and_pass_cover_every_list(self):
        st = fresh()
        lst(st, "Video")["hours"]["tuesday"] = "12:00-24:00"
        ts = T(2026, 10, 13, 13, 0)  # Video open, Social blocked
        d = core.describe(st, ts)
        self.assertEqual(d["headline"], "Social blocked until 17:00. Video open until tomorrow 00:00")
        self.assertTrue(d["blocked"])
        self.assertFalse(d["blocked_all"])
        core.block_now(st, ts)  # to Social's open at 17:00 (later than 90 min)
        self.assertEqual(st["block_until"], T(2026, 10, 13, 17, 0))
        self.assertTrue(core.list_blocked(st, lst(st, "Video"), ts + 60))
        core.take_pass(st, ts + 120)
        self.assertFalse(any(core.list_blocked(st, L, ts + 180) for L in core.lists(st)))


class TestReconcileLists(unittest.TestCase):
    def test_coarse_window_and_gaps(self):
        api = FakeApi()
        st = fresh()
        lst(st, "Video")["hours"]["tuesday"] = "07:00-09:00,17:00-24:00"
        lst(st, "Social")["hours"]["tuesday"] = "12:00-22:00"
        reconcile.run(api, st, T(2026, 10, 13, 6, 0))
        self.assertEqual(api.data["parentalControl"]["recreation"]["times"]["tuesday"],
                         {"start": "07:00:00", "end": "23:59:00"})
        svc = lambda sid: (api._svc(sid)["active"], api._svc(sid)["recreation"])  # noqa: E731
        self.assertEqual((svc("youtube"), svc("twitter")), ((True, True), (True, True)))  # outside the span
        reconcile.run(api, st, T(2026, 10, 13, 8, 0))   # Video open, Social in a gap of the span
        self.assertEqual((svc("youtube"), svc("twitter")), ((True, True), (True, False)))
        reconcile.run(api, st, T(2026, 10, 13, 10, 0))  # both in gaps
        self.assertEqual((svc("youtube"), svc("twitter")), ((True, False), (True, False)))
        reconcile.run(api, st, T(2026, 10, 13, 13, 0))  # Social open, Video in its gap
        self.assertEqual((svc("youtube"), svc("twitter")), ((True, False), (True, True)))
        self.assertFalse(api._deny("x.com")["active"])
        reconcile.run(api, st, T(2026, 10, 13, 22, 30))  # Social closed again, Video open
        self.assertEqual((svc("youtube"), svc("twitter")), ((True, True), (True, False)))
        self.assertTrue(api._deny("x.com")["active"])

    def test_lists_without_services_do_not_widen_nextdns(self):
        st = fresh()
        core.add_list(st, "Reading", TUE_EVENING)
        core.set_hours(st, "Reading", {"monday": "06:00-07:00"}, TUE_EVENING)
        core.add_domain(st, "news.ycombinator.com", TUE_EVENING, "Reading")
        self.assertEqual(reconcile.coarse_hours(st)["monday"], "17:00-24:00")
        text = timers.render_dropin(st["settings"])
        self.assertIn("OnCalendar=Mon *-*-* 06:00:00 America/New_York", text)  # the timers still flip it
        self.assertIn("OnCalendar=Mon *-*-* 07:00:00 America/New_York", text)

    def test_flip_timer_has_every_edge_of_every_list(self):
        st = fresh()
        lst(st, "Video")["hours"]["tuesday"] = "07:00-09:00,17:00-24:00"
        lst(st, "Social")["hours"]["tuesday"] = "12:00-22:00"
        text = timers.render_dropin(st["settings"])
        for t in ("07:00", "09:00", "12:00", "22:00"):
            self.assertIn(f"OnCalendar=Tue *-*-* {t}:00 America/New_York", text)
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 17:00:00 America/New_York", text)
        self.assertEqual(text.count("OnCalendar="), 7)

    def test_categories_always_blocked_and_never_opened_by_a_pass(self):
        api = FakeApi()
        st = fresh()
        core.take_pass(st, TUE_NOON)
        reconcile.run(api, st, TUE_NOON)
        self.assertFalse([w for w in api.writes if "category" in w[0]])
        self.assertTrue(all(c["active"] and not c["recreation"] for c in api.data["parentalControl"]["categories"]))
        self.assertFalse(api._svc("youtube")["active"])  # the pass did open the lists

    def test_category_off_is_queued_during_a_block_and_on_is_immediate(self):
        api = FakeApi()
        st = fresh()
        self.assertEqual(core.set_category(st, "porn", False, TUE_NOON), "Porn goes off at 17:00.")
        reconcile.run(api, st, TUE_NOON)
        self.assertEqual(api.writes, [])
        self.assertTrue(api._cat("porn")["active"])
        reconcile.run(api, st, T(2026, 10, 13, 17, 0))
        self.assertIn(("patch_category", "porn", {"active": False, "recreation": False}), api.writes)
        self.assertEqual(core.set_category(st, "porn", True, T(2026, 10, 14, 9, 0)), "Porn is blocked again.")
        api.writes.clear()
        reconcile.run(api, st, T(2026, 10, 14, 9, 0))
        self.assertEqual([w for w in api.writes if "category" in w[0]],
                         [("patch_category", "porn", {"active": True, "recreation": False})])
        st2 = fresh()
        core.set_category(st2, "gambling", False, TUE_NOON)
        self.assertIn("stays blocked (queued change undone)", core.set_category(st2, "gambling", True, TUE_NOON))
        with self.assertRaises(core.Refused):
            core.set_category(st2, "chess", False, TUE_NOON)

    def test_missing_category_is_added_back(self):
        api = FakeApi()
        api.data["parentalControl"]["categories"] = [c for c in api.data["parentalControl"]["categories"]
                                                     if c["id"] != "gambling"]
        reconcile.run(api, fresh(), TUE_NOON)
        self.assertIn(("add_category", "gambling", {"active": True, "recreation": False}), api.writes)


class TestInsights(AppCase):
    def test_shapes_from_the_analytics(self):
        data = insights.fetch(self.api, self.st()["settings"], TUE_NOON)
        self.assertEqual(len(data["days"]), 30)
        self.assertEqual((data["days"][0]["date"], data["days"][-1]["date"]), ("2026-09-14", "2026-10-13"))
        self.assertEqual(data["days"][-1], {"date": "2026-10-13", "queries": 49, "blocked": 33})
        self.assertEqual(data["month"], {"total": 49, "blocked": 33})
        self.assertEqual(len(data["top_blocked"]), 5)
        self.assertEqual(data["devices"], [{"name": "unnamed", "queries": 49}])
        kind, params = self.api.analytics_calls[0]
        self.assertEqual(kind, "status;series")
        self.assertEqual((params["from"], params["to"], params["interval"]),
                         ("2026-09-14T00:00:00-04:00", "2026-10-14T00:00:00-04:00", "86400"))

    def test_cached_for_five_minutes(self):
        a = self.app.insights()
        self.assertTrue(a["ok"])
        n = len(self.api.analytics_calls)
        self.clock.t += 4 * 60
        self.assertEqual(self.app.insights()["at"], a["at"])
        self.assertEqual(len(self.api.analytics_calls), n)
        self.clock.t += 2 * 60
        self.assertGreater(self.app.insights()["at"], a["at"])
        self.assertEqual(len(self.api.analytics_calls), 2 * n)
        self.assertTrue(insights.cache_path(self.cfg).exists())

    def test_errors_degrade_to_no_data_and_are_cached(self):
        self.api.down = True
        out = self.app.insights()
        self.assertFalse(out["ok"])
        self.api.down = False
        self.clock.t += 60
        self.assertFalse(self.app.insights()["ok"])  # no second wait inside the interval
        self.assertEqual(self.api.analytics_calls, [])
        html = page.render(self.st(), self.clock.t, insights=out)
        self.assertIn("No data yet.", html)
        html = page.render(self.st(), self.clock.t, insights=None)
        self.assertIn("No data yet.", html)

    def test_chart_on_the_page(self):
        import re
        html = page.render(self.st(), TUE_NOON, insights=self.app.insights())
        svg = re.search(r"<svg.*?</svg>", html, flags=re.S).group(0)
        self.assertEqual(svg.count('<g class="col">'), 30)
        self.assertEqual(svg.count('class="bar"'), 2)              # two days with lookups
        self.assertIn("<title>Tue Oct 13: 49 lookups, 33 blocked</title>", svg)
        self.assertIn('class="peak"', html)
        self.assertIn("Blocked this month: <span class=\"num\">67%</span>", html)
        self.assertIn("<span>unnamed</span>", html)
        self.assertIn("<th>Day</th>", html)                         # the table view
        self.assertNotRegex(svg, r"#[0-9a-fA-F]{3,8}\b")            # colors come from the tokens


class TestDoctorTailscale(unittest.TestCase):
    NONE = """=== 'Use Tailscale DNS' status ===

Tailscale DNS: enabled.

=== MagicDNS configuration ===

Resolvers (in preference order):
  (no resolvers configured, system default will be used: see 'System DNS configuration' below)

Split DNS Routes:
  - ts.net.                        -> 199.247.155.53
"""

    def test_no_global_nameserver(self):
        lines = cli.tailscale_dns_lines(self.NONE, "abc123")
        self.assertEqual(lines, ["Tailnet global nameserver: none set (devices use their own resolver).",
                                 "This node accepts tailnet DNS (accept-dns on)."])

    def test_nextdns_global_nameserver(self):
        text = self.NONE.replace("  (no resolvers configured, system default will be used: see 'System DNS configuration' below)",
                                 "  - https://dns.nextdns.io/abc123\n  - 45.90.28.0").replace("enabled.", "disabled.")
        lines = cli.tailscale_dns_lines(text, "abc123")
        self.assertEqual(lines[0], "Tailnet global nameserver: NextDNS (https://dns.nextdns.io/abc123); also 45.90.28.0.")
        self.assertEqual(lines[1], "This node does not accept tailnet DNS (accept-dns off).")
        other = self.NONE.replace("  (no resolvers configured, system default will be used: see 'System DNS configuration' below)",
                                  "  - 1.1.1.1")
        self.assertIn("not NextDNS (1.1.1.1)", cli.tailscale_dns_lines(other, "abc123")[0])
        self.assertIn("did not answer", cli.tailscale_dns_lines(None, "abc123")[0])


class TestPageLists(AppCase):
    def test_one_block_per_list_and_always_blocked(self):
        html = page.render(self.st(), TUE_NOON)
        for name in ("Video", "Social"):
            self.assertIn(f'id="list-{name.lower()}"', html)
            self.assertIn(f'<input type="hidden" name="list" value="{name}">', html)
        self.assertIn("Hours · Mon–Fri 17:00–00:00 · Sat–Sun all day", html)
        self.assertIn('<h2 id="h-always">Always blocked</h2>', html)
        self.assertIn('<span>Porn</span>', html)
        self.assertIn('name="on" value="0"><button class="link">Off</button>', html)
        self.assertIn('name="plus" value="monday"', html)
        self.app.category("porn", False)
        html = page.render(self.st(), TUE_NOON)
        self.assertIn("Off at 17:00", html)

    def test_more_opens_a_blank_window(self):
        html = page.render(self.st(), TUE_NOON, more=("social", "monday"))
        self.assertIn('name="start_monday_1" value=""', html)
        self.assertIn('<details id="hours-social" open>', html)
        self.assertNotIn('<details id="hours-video" open>', html)


if __name__ == "__main__":
    unittest.main()
