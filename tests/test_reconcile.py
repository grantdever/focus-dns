"""reconcile against the fake API seeded with the live profile's shape."""
import unittest

from focus import core, reconcile
from focus.api import ApiError
from focus.schedule import ScheduleError

from .fake import FakeApi
from focus.actions import Focus

from .helpers import AppCase, T, TUE_EVENING, TUE_NOON, fresh, lst


class TestPlan(unittest.TestCase):
    def test_first_reconcile_is_a_noop_on_the_current_profile(self):
        api = FakeApi()
        st = fresh()
        self.assertEqual(reconcile.run(api, st, TUE_NOON), [])
        self.assertEqual(api.writes, [])
        self.assertTrue(st["last_reconcile"]["ok"])

    def test_weekday_evening_flips_only_the_denylist(self):
        api = FakeApi()
        reconcile.run(api, fresh(), TUE_EVENING)
        self.assertEqual(api.writes, [("patch_deny", "x.com", {"active": False})])
        api.writes.clear()
        reconcile.run(api, fresh(), T(2026, 10, 14, 0, 0))  # midnight: blocked again
        self.assertEqual(api.writes, [("patch_deny", "x.com", {"active": True})])

    def test_pass_opens_services_and_denylist_then_restores(self):
        api = FakeApi()
        st = fresh()
        core.take_pass(st, TUE_NOON)
        reconcile.run(api, st, TUE_NOON)
        both = {"active": False, "recreation": True}
        self.assertCountEqual(api.writes, [("patch_service", "youtube", both),
                                           ("patch_service", "twitter", both),
                                           ("patch_deny", "x.com", {"active": False})])
        api.writes.clear()
        reconcile.run(api, st, TUE_NOON + 15 * 60)
        both = {"active": True, "recreation": True}
        self.assertCountEqual(api.writes, [("patch_service", "youtube", both),
                                           ("patch_service", "twitter", both),
                                           ("patch_deny", "x.com", {"active": True})])
        self.assertTrue(all(s["recreation"] for s in api.data["parentalControl"]["services"]))

    def test_block_now_turns_off_recreation(self):
        api = FakeApi()
        st = fresh()
        core.block_now(st, TUE_EVENING)
        reconcile.run(api, st, TUE_EVENING)
        off = {"active": True, "recreation": False}
        self.assertCountEqual(api.writes, [("patch_service", "youtube", off), ("patch_service", "twitter", off)])
        api.writes.clear()
        reconcile.run(api, st, TUE_EVENING + 91 * 60)
        on = {"active": True, "recreation": True}
        self.assertCountEqual(api.writes, [("patch_service", "youtube", on), ("patch_service", "twitter", on),
                                           ("patch_deny", "x.com", {"active": False})])

    def test_restores_wiped_schedule_lost_flags_and_bypass(self):
        api = FakeApi()
        pc = api.data["parentalControl"]
        pc["recreation"]["times"] = {}
        pc["services"][0]["recreation"] = False
        pc["blockBypass"] = False
        changes = reconcile.run(api, fresh(), TUE_NOON)
        self.assertEqual(len(changes), 3)
        self.assertEqual(reconcile.run(api, fresh(), TUE_NOON), [])
        self.assertEqual(pc["recreation"]["times"]["monday"], {"start": "17:00:00", "end": "23:59:00"})

    def test_bad_schedule_never_reaches_the_api(self):
        api = FakeApi()
        st = fresh()
        lst(st, "Social")["hours"]["monday"] = "5pm"
        with self.assertRaises(ScheduleError):
            reconcile.run(api, st, TUE_NOON)
        self.assertEqual(api.writes, [])
        self.assertFalse(st["last_reconcile"]["ok"])

    def test_schedule_is_reread_after_writing(self):
        api = FakeApi()
        api.data["parentalControl"]["recreation"]["times"]["monday"]["start"] = "18:00:00"
        real = api.patch_parental
        api.patch_parental = lambda body: None  # write "succeeds" but nothing sticks
        with self.assertRaises(ApiError) as c:
            reconcile.run(api, fresh(), TUE_NOON)
        self.assertIn("did not stick", str(c.exception))
        api.patch_parental = real
        reconcile.run(api, fresh(), TUE_NOON)
        self.assertEqual(api.data["parentalControl"]["recreation"]["times"]["monday"]["start"], "17:00:00")

    def test_added_and_removed_domains(self):
        api = FakeApi()
        st = fresh()
        core.add_domain(st, "reddit.com", TUE_NOON, "Social")
        reconcile.run(api, st, TUE_NOON)
        self.assertIn(("add_deny", "reddit.com", {"active": True}), api.writes)
        core.remove_domain(st, "reddit.com", TUE_EVENING)
        reconcile.run(api, st, TUE_EVENING)
        self.assertIn(("delete_deny", "reddit.com"), api.writes)
        self.assertEqual(st["pending_removals"], [])

    def test_hand_added_entries_left_alone(self):
        api = FakeApi()
        api.data["denylist"].append({"id": "tiktok.com", "active": True})
        reconcile.run(api, fresh(), TUE_EVENING)
        self.assertNotIn("tiktok.com", [w[1] for w in api.writes])

    def test_usage_summed(self):
        api = FakeApi()
        st = fresh()
        reconcile.run(api, st, TUE_NOON)
        self.assertEqual(st["usage"]["queries"], 8)
        self.assertEqual(st["usage"]["month"], "2026-10")


class TestActions(AppCase):
    def test_pass_schedules_restore_and_counts(self):
        self.cfg.systemd = True
        msg = self.app.take_pass()
        self.assertIn("open until 12:15", msg)
        self.assertIn("2 left today", msg)
        run = [c for c in self.calls if c[0] == "systemd-run"]
        self.assertEqual(len(run), 1)
        self.assertIn("--on-active=900s", run[0])
        self.assertEqual(run[0][-2:], ["focus", "reconcile"])
        self.assertEqual(self.st()["passes"][0]["minutes"], 15)

    def test_failed_pass_puts_back_what_changed(self):
        real = self.api.patch_deny

        def broken(d, body):
            raise ApiError("PATCH /denylist: HTTP 500", 500)
        self.api.patch_deny = broken  # services open, then the denylist write fails
        with self.assertRaises(core.Refused):
            self.app.take_pass()
        self.api.patch_deny = real
        self.assertTrue(all(s["active"] and s["recreation"] for s in self.api.data["parentalControl"]["services"]))
        self.assertEqual(self.st()["passes"], [])

    def test_pass_not_spent_when_nextdns_is_down(self):
        self.api.down = True
        with self.assertRaises(core.Refused):
            self.app.take_pass()
        self.assertEqual(self.st()["passes"], [])
        self.assertEqual(core.passes_left(self.st(), self.clock.t), 3)

    def test_queued_edit_writes_nothing_to_nextdns(self):
        self.app.reconcile()
        before = self.st()
        self.assertEqual(self.app.remove("x.com"), "Removes x.com at 17:00.")
        self.app.hours("Social", {"tuesday": "15:00"})
        self.app.hours("Video", {"tuesday": "07:00-09:00,17:00-24:00"})
        self.app.limits(passes=6)
        self.app.category("porn", False)
        after = self.st()
        self.assertEqual(after["settings"], before["settings"])
        self.assertEqual(len(after["pending"]), 5)
        self.assertEqual(self.api.writes, [])

    def test_loosening_never_applied_early_by_reconcile(self):
        self.app.remove("x.com")
        for t in (T(2026, 10, 13, 12, 5), T(2026, 10, 13, 16, 0), T(2026, 10, 13, 16, 59)):
            self.clock.t = t
            self.assertEqual(self.app.reconcile(), [])
            self.assertIn("x.com", lst(self.st(), "Social")["domains"])
            self.assertTrue(self.api._deny("x.com")["active"])
        self.assertEqual(self.api.writes, [])

    def test_reconcile_applies_queue_at_unlock(self):
        self.app.remove("x.com")
        self.app.hours("Social", {"wednesday": "09:00-24:00"})
        self.clock.t = T(2026, 10, 13, 17, 0)
        changes = self.app.reconcile()
        self.assertIn("applied queued change: remove x.com", changes)
        self.assertIn("remove x.com", changes)
        self.assertIn(("delete_deny", "x.com"), self.api.writes)
        self.assertEqual(self.api.data["parentalControl"]["recreation"]["times"]["wednesday"]["start"], "09:00:00")
        st = self.st()
        self.assertEqual((st["pending"], lst(st, "Social")["domains"], st["pending_removals"]), ([], [], []))
        self.assertIn("OnCalendar=Wed *-*-* 09:00:00", self.cfg.flip_dropin.read_text())

    def test_queue_survives_a_restart_and_follows_a_later_unlock(self):
        self.app.remove("x.com")
        self.clock.t = T(2026, 10, 13, 16, 30)
        self.app.block()                                   # 90 min: to 18:00
        again = Focus(self.cfg, api=self.api, runner=lambda c: (0, ""), clock=self.clock)  # a fresh process
        st = again.state()
        self.assertEqual([p["id"] for p in st["pending"]], ["remove:x.com"])
        self.assertEqual(st["pending"][0]["applies"], T(2026, 10, 13, 18, 0))
        self.clock.t = T(2026, 10, 13, 17, 0)
        again.reconcile()
        self.assertIn("x.com", lst(again.state(), "Social")["domains"])
        self.clock.t = T(2026, 10, 13, 18, 0)
        again.reconcile()
        self.assertEqual(lst(again.state(), "Social")["domains"], [])
        self.assertIsNone(self.api._deny("x.com"))

    def test_undo_through_the_app(self):
        self.app.remove("x.com")
        self.assertIn("Undone", self.app.undo("remove:x.com"))
        self.clock.t = TUE_EVENING
        self.app.reconcile()
        self.assertEqual(lst(self.st(), "Social")["domains"], ["x.com"])

    def test_block_until_schedules_the_restore(self):
        self.cfg.systemd = True
        self.clock.t = TUE_EVENING
        self.assertEqual(self.app.block(until_hhmm="20:00"), "Blocked until 20:00 (today).")
        run = [c for c in self.calls if c[0] == "systemd-run"]
        self.assertIn("--on-active=7200s", run[-1])
        self.assertTrue(self.api._svc("youtube")["active"])
        self.assertFalse(self.api._svc("youtube")["recreation"])

    def test_hours_edit_rewrites_flip_timer(self):
        self.cfg.systemd = True
        self.app.hours("Social", {"monday": "18:00"})
        self.app.hours("Video", {"monday": "18:00"})
        text = self.cfg.flip_dropin.read_text()
        self.assertIn("OnCalendar=Mon *-*-* 18:00:00 America/New_York", text)  # Monday: later open, tighter
        self.assertIn("OnCalendar=Tue,Wed,Thu,Fri *-*-* 17:00:00 America/New_York", text)
        self.assertIn(["systemctl", "--user", "daemon-reload"], self.calls)
        self.assertEqual(self.api.data["parentalControl"]["recreation"]["times"]["monday"]["start"], "18:00:00")

    def test_edit_saved_even_if_nextdns_down(self):
        self.api.down = True
        msg = self.app.add("reddit.com", "Social")
        self.assertIn("retried", msg)
        self.assertIn("reddit.com", lst(self.st(), "Social")["domains"])
        self.assertFalse(self.st()["last_reconcile"]["ok"])


if __name__ == "__main__":
    unittest.main()
