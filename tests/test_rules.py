"""Lock rules, passes, Block now, schedule validation. Pure functions, fixed clocks."""
import unittest

from focus import core, schedule as sch
from focus.schedule import ScheduleError

from .helpers import SAT_NOON, T, TUE_EVENING, TUE_NOON, fresh, lst, week


class TestSchedule(unittest.TestCase):
    def test_valid_rules(self):
        self.assertEqual(sch.normalize_rule("17:00"), "17:00-24:00")
        self.assertEqual(sch.normalize_rule(" Blocked "), "blocked")
        self.assertEqual(sch.normalize_rule("00:00"), "open")
        self.assertEqual(sch.normalize_rule("09:00-17:00"), "09:00-17:00")
        self.assertEqual(sch.normalize_rule("09:00 - 17:00"), "09:00-17:00")
        for midnight in ("10:00-24:00", "10:00-00:00", "10:00-23:59"):
            self.assertEqual(sch.normalize_rule(midnight), "10:00-24:00")
        self.assertEqual(sch.make_rule("10:00", ""), "10:00-24:00")
        self.assertEqual(sch.normalize_rule("00:00-24:00"), "open")

    def test_rejects_non_hhmm(self):
        for bad in ("5pm", "17", "17:0", "7:00", "24:00", "17:60", "17:00:00", "", "1700", "17h00", 17,
                    "17:00-5pm", "17:00-17:00", "17:00-09:00", "24:00-24:00", "09:00-24:01", "-17:00", "09:00-17:00-18:00",
                    "09:00-10:00,", ",09:00-10:00", "09:00-10:00,,11:00-12:00", "09:00-10:00,5pm", ["09:00-10:00", 9]):
            with self.assertRaises(ScheduleError, msg=repr(bad)):
                sch.normalize_rule(bad)

    def test_both_ends_map_exactly_onto_recreation(self):
        s = week(monday="09:00-12:30", tuesday="00:00-08:00", wednesday="18:00-00:00")
        times = sch.to_recreation(s)["times"]
        self.assertEqual(times["monday"], {"start": "09:00", "end": "12:30"})
        self.assertEqual(times["tuesday"], {"start": "00:00", "end": "08:00"})
        self.assertEqual(times["wednesday"], {"start": "18:00", "end": "23:59"})  # midnight as NextDNS holds it

    def test_both_ends_blocked_outside_the_window(self):
        s = week(tuesday="09:00-12:30")
        self.assertTrue(sch.sched_blocked(s, T(2026, 10, 13, 8, 59)))
        self.assertFalse(sch.sched_blocked(s, T(2026, 10, 13, 9, 0)))
        self.assertFalse(sch.sched_blocked(s, T(2026, 10, 13, 12, 29)))
        self.assertTrue(sch.sched_blocked(s, T(2026, 10, 13, 12, 30)))
        self.assertEqual(sch.next_sched_open(s, T(2026, 10, 13, 7, 0)), T(2026, 10, 13, 9, 0))
        self.assertIn(T(2026, 10, 13, 12, 30), sch.boundaries(s, T(2026, 10, 13, 7, 0)))

    def test_tighter_and_overlap(self):
        self.assertTrue(sch.is_tighter("18:00-24:00", "17:00-24:00"))
        self.assertTrue(sch.is_tighter("17:00-22:00", "17:00-24:00"))
        self.assertTrue(sch.is_tighter("blocked", "open"))
        self.assertFalse(sch.is_tighter("16:00-24:00", "17:00-24:00"))
        self.assertFalse(sch.is_tighter("open", "17:00-24:00"))
        self.assertFalse(sch.is_tighter("16:00-22:00", "17:00-24:00"))  # shifted: wider at one end
        self.assertEqual(sch.intersect("16:00-22:00", "17:00-24:00"), "17:00-22:00")
        self.assertEqual(sch.intersect("09:00-12:00", "17:00-24:00"), "blocked")

    def test_to_recreation_matches_live_profile(self):
        rec = sch.to_recreation(week())
        self.assertEqual(rec["timezone"], "America/New_York")
        self.assertEqual(rec["times"]["monday"], {"start": "17:00", "end": "23:59"})
        self.assertEqual(rec["times"]["sunday"], {"start": "00:00", "end": "23:59"})

    def test_blocked_day_is_left_out(self):
        s = week(wednesday="blocked")
        self.assertNotIn("wednesday", sch.to_recreation(s)["times"])

    def test_to_recreation_validates_before_anything_is_written(self):
        s = week(monday=1700)
        with self.assertRaises(ScheduleError):
            sch.to_recreation(s)
        s["hours"]["monday"] = "17:00"
        s["timezone"] = "Mars/Olympus"
        with self.assertRaises(ScheduleError):
            sch.to_recreation(s)

    def test_sched_blocked(self):
        s = week()
        self.assertTrue(sch.sched_blocked(s, TUE_NOON))
        self.assertTrue(sch.sched_blocked(s, T(2026, 10, 13, 16, 59)))
        self.assertFalse(sch.sched_blocked(s, T(2026, 10, 13, 17, 0)))
        self.assertFalse(sch.sched_blocked(s, SAT_NOON))
        self.assertTrue(sch.sched_blocked(s, T(2026, 10, 19, 0, 0)))  # Monday midnight


class TestLockRules(unittest.TestCase):
    def test_in_block_loosening_is_queued_not_applied(self):
        st = fresh()
        self.assertEqual(core.remove_domain(st, "x.com", TUE_NOON), "Removes x.com at 17:00.")
        self.assertIn("x.com", lst(st, "Social")["domains"])
        core.set_hours(st, "Social", {"tuesday": "16:00"}, TUE_NOON)
        core.set_hours(st, "video", {"wednesday": "open"}, TUE_NOON)
        core.set_hours(st, "Video", {"thursday": "07:00-09:00,17:00-24:00"}, TUE_NOON)  # an extra window
        core.set_limits(st, TUE_NOON, passes=4)
        core.set_limits(st, TUE_NOON, minutes=20)
        core.set_timezone(st, "America/Los_Angeles", TUE_NOON)
        core.set_category(st, "porn", False, TUE_NOON)
        self.assertEqual(st["settings"], fresh()["settings"])  # nothing applied
        self.assertEqual({p["id"] for p in st["pending"]},
                         {"remove:x.com", "hours:social:tuesday", "hours:video:wednesday", "hours:video:thursday",
                          "passes", "minutes", "timezone", "category:porn"})
        self.assertTrue(all(p["applies"] == T(2026, 10, 13, 17, 0) for p in st["pending"]))

    def test_shifted_window_applies_the_tight_part_and_queues_the_rest(self):
        st = fresh()
        msg = core.set_hours(st, "Social", {"saturday": "open", "monday": "blocked", "friday": "12:00-20:00"}, TUE_NOON)
        h = lst(st, "Social")["hours"]
        self.assertEqual(h["monday"], "blocked")            # tighter: at once
        self.assertEqual(h["friday"], "17:00-20:00")        # the overlap: at once
        self.assertEqual(h["saturday"], "open")             # unchanged, nothing queued
        self.assertEqual([(p["id"], p["rule"]) for p in st["pending"]], [("hours:social:friday", "12:00-20:00")])
        self.assertIn("Social Fri apply at 17:00", msg)
        self.assertEqual(lst(st, "Video")["hours"]["monday"], "17:00-24:00")  # the other list is untouched

    def test_queued_change_applies_at_unlock_never_before(self):
        st = fresh()
        core.remove_domain(st, "x.com", TUE_NOON)
        core.set_hours(st, "Social", {"wednesday": "12:00"}, TUE_NOON)
        core.set_limits(st, TUE_NOON, passes=5)
        for t in (TUE_NOON + 60, T(2026, 10, 13, 16, 59)):
            self.assertEqual(core.apply_due(st, t), [])
            self.assertIn("x.com", lst(st, "Social")["domains"])
            self.assertEqual(st["settings"]["passes_per_day"], 3)
        done = core.apply_due(st, T(2026, 10, 13, 17, 0))
        self.assertEqual(len(done), 3)
        self.assertEqual(lst(st, "Social")["domains"], [])
        self.assertEqual(st["pending_removals"], ["x.com"])
        self.assertEqual(lst(st, "Social")["hours"]["wednesday"], "12:00-24:00")
        self.assertEqual(st["settings"]["passes_per_day"], 5)
        self.assertEqual(st["pending"], [])

    def test_queued_change_follows_a_later_unlock(self):
        st = fresh()
        core.remove_domain(st, "x.com", TUE_NOON)
        core.block_now(st, T(2026, 10, 13, 16, 30))                     # to 18:00
        self.assertEqual(st["pending"][0]["applies"], T(2026, 10, 13, 18, 0))
        self.assertEqual(core.apply_due(st, T(2026, 10, 13, 17, 0)), [])
        core.set_hours(st, "Social", {"tuesday": "19:00"}, T(2026, 10, 13, 17, 30))  # tightening pushes it to 19:00
        core.refresh_pending(st, T(2026, 10, 13, 17, 30))
        self.assertEqual(st["pending"][0]["applies"], T(2026, 10, 13, 19, 0))
        self.assertEqual(core.apply_due(st, T(2026, 10, 13, 18, 30)), [])
        self.assertIn("x.com", lst(st, "Social")["domains"])
        self.assertEqual(len(core.apply_due(st, T(2026, 10, 13, 19, 0))), 1)
        self.assertNotIn("x.com", lst(st, "Social")["domains"])

    def test_undo_and_re_adding_cancel_a_queued_removal(self):
        st = fresh()
        core.remove_domain(st, "x.com", TUE_NOON)
        self.assertIn("Undone", core.undo(st, "remove:x.com", TUE_NOON))
        self.assertEqual(st["pending"], [])
        with self.assertRaises(core.Refused):
            core.undo(st, "remove:x.com", TUE_NOON)
        core.remove_domain(st, "x.com", TUE_NOON)
        self.assertIn("stays on Social", core.add_domain(st, "x.com", TUE_NOON, "social"))
        self.assertEqual(st["pending"], [])
        self.assertEqual(core.apply_due(st, TUE_EVENING), [])
        self.assertEqual(lst(st, "Social")["domains"], ["x.com"])

    def test_tightening_the_same_field_replaces_its_queued_change(self):
        st = fresh()
        core.set_limits(st, TUE_NOON, passes=5)
        core.set_limits(st, TUE_NOON, passes=3, minutes=15)   # unchanged values (the form posts both): kept
        self.assertEqual(core.find_pending(st, "passes")["value"], 5)
        core.set_limits(st, TUE_NOON, passes=2)                # tighter: applies, drops the queued 5
        self.assertEqual(st["settings"]["passes_per_day"], 2)
        self.assertIsNone(core.find_pending(st, "passes"))
        core.set_hours(st, "Social", {"tuesday": "16:00"}, TUE_NOON)
        core.set_hours(st, "Social", {"tuesday": "18:00"}, TUE_NOON)
        self.assertEqual(lst(st, "Social")["hours"]["tuesday"], "18:00-24:00")
        self.assertIsNone(core.find_pending(st, "hours:social:tuesday"))

    def test_in_block_tightening_applies(self):
        st = fresh()
        self.assertEqual(core.add_domain(st, "https://www.Reddit.com/r/x", TUE_NOON, "Social"), "Added reddit.com to Social.")
        core.set_hours(st, "Social", {"tuesday": "18:30", "saturday": "12:00", "sunday": "blocked"}, TUE_NOON)
        self.assertEqual(lst(st, "Social")["hours"]["tuesday"], "18:30-24:00")
        core.set_hours(st, "Social", {"wednesday": "17:00-22:00"}, TUE_NOON)       # earlier close: tighter
        self.assertEqual(lst(st, "Social")["hours"]["wednesday"], "17:00-22:00")
        core.set_hours(st, "Social", {"wednesday": "17:00-18:00,20:00-22:00"}, TUE_NOON)  # a gap cut in: tighter
        self.assertEqual(lst(st, "Social")["hours"]["wednesday"], "17:00-18:00,20:00-22:00")
        core.set_hours(st, "Social", {"wednesday": "20:00-22:00"}, TUE_NOON)       # a window removed: tighter
        self.assertEqual(lst(st, "Social")["hours"]["wednesday"], "20:00-22:00")
        self.assertEqual(st["pending"], [])
        core.set_limits(st, TUE_NOON, passes=2, minutes=10)
        self.assertEqual((st["settings"]["passes_per_day"], st["settings"]["pass_minutes"]), (2, 10))

    def test_out_of_block_everything_applies(self):
        for ts in (TUE_EVENING, SAT_NOON):
            st = fresh()
            core.remove_domain(st, "x.com", ts)
            core.set_hours(st, "Video", {"monday": "open"}, ts)
            core.set_limits(st, ts, passes=5, minutes=30)
            core.set_timezone(st, "America/Chicago", ts)
            core.set_category(st, "gambling", False, ts)
            self.assertEqual(lst(st, "Social")["domains"], [])
            self.assertEqual(st["pending_removals"], ["x.com"])
            self.assertEqual(st["pending"], [])
            self.assertEqual(lst(st, "Video")["hours"]["monday"], "open")
            self.assertFalse(st["settings"]["categories"][1]["active"])
            self.assertEqual(st["settings"]["timezone"], "America/Chicago")

    def test_block_now_locks_outside_hours(self):
        st = fresh()
        until = core.block_now(st, TUE_EVENING)
        self.assertEqual(until, TUE_EVENING + 90 * 60)
        self.assertEqual(core.remove_domain(st, "x.com", TUE_EVENING + 60), "Removes x.com at 19:30.")
        self.assertEqual(core.apply_due(st, until - 1), [])
        core.apply_due(st, until)
        self.assertEqual(lst(st, "Social")["domains"], [])

    def test_unlock_chains_block_now_into_schedule(self):
        st = fresh()
        core.block_now(st, T(2026, 10, 12, 23, 30), minutes=60)  # Mon 23:30 -> Tue 00:30, then Tue is blocked to 17:00
        self.assertEqual(core.unlock_at(st, T(2026, 10, 12, 23, 45)), T(2026, 10, 13, 17, 0))

    def test_pass_does_not_lift_the_lock(self):
        st = fresh()
        core.take_pass(st, TUE_NOON)
        core.remove_domain(st, "x.com", TUE_NOON + 60)
        self.assertEqual(core.apply_due(st, TUE_NOON + 120), [])  # pass running, block still in force
        self.assertIn("x.com", lst(st, "Social")["domains"])

    def test_all_days_blocked_says_so(self):
        st = fresh()
        core.set_hours(st, "Video", {d: "blocked" for d in sch.DAYS}, TUE_NOON)
        msg = core.set_limits(st, TUE_NOON, passes=9)
        self.assertIn("none this week", msg)
        self.assertEqual(st["settings"]["passes_per_day"], 3)
        self.assertIsNone(st["pending"][0]["applies"])

    def test_bad_input_refused(self):
        st = fresh()
        for bad in ("localhost", "not a domain", "a..com", "-x.com", ""):
            with self.assertRaises(core.Refused, msg=bad):
                core.add_domain(st, bad, TUE_NOON, "Social")
        with self.assertRaises(core.Refused):
            core.set_hours(st, "Social", {"tuesday": "5pm"}, TUE_EVENING)
        with self.assertRaises(core.Refused):
            core.set_hours(st, "Nope", {"tuesday": "17:00"}, TUE_EVENING)
        with self.assertRaises(core.Refused):
            core.set_limits(st, TUE_EVENING, passes=-1)


class TestPasses(unittest.TestCase):
    def test_three_a_day_then_refused(self):
        st = fresh()
        t = TUE_NOON
        for i in range(3):
            end = core.take_pass(st, t)
            self.assertEqual(end, t + 15 * 60)
            t = end + 60
        self.assertEqual(core.passes_left(st, t), 0)
        with self.assertRaises(core.Refused) as c:
            core.take_pass(st, t)
        self.assertIn("No passes left today", str(c.exception))

    def test_counter_resets_at_midnight(self):
        st = fresh()
        t = T(2026, 10, 13, 9, 0)
        for _ in range(3):
            t = core.take_pass(st, t) + 60
        self.assertEqual(core.passes_left(st, T(2026, 10, 13, 23, 59)), 0)
        self.assertEqual(core.passes_left(st, T(2026, 10, 14, 0, 0)), 3)
        core.take_pass(st, T(2026, 10, 14, 0, 5))
        self.assertEqual(core.passes_left(st, T(2026, 10, 14, 0, 6)), 2)

    def test_refused_outside_block_and_while_running(self):
        st = fresh()
        with self.assertRaises(core.Refused) as c:
            core.take_pass(st, TUE_EVENING)
        self.assertEqual(str(c.exception), "Nothing is blocked right now.")
        with self.assertRaises(core.Refused):
            core.take_pass(st, SAT_NOON)
        core.take_pass(st, TUE_NOON)
        with self.assertRaises(core.Refused) as c:
            core.take_pass(st, TUE_NOON + 60)
        self.assertIn("already running until 12:15", str(c.exception))

    def test_length_capped_by_setting(self):
        st = fresh()
        with self.assertRaises(core.Refused):
            core.take_pass(st, TUE_NOON, 16)
        self.assertEqual(core.take_pass(st, TUE_NOON, 1), TUE_NOON + 60)

    def test_pass_allowed_during_block_now_and_block_now_ends_pass(self):
        st = fresh()
        core.block_now(st, TUE_EVENING)
        core.take_pass(st, TUE_EVENING + 60)
        self.assertFalse(core.effective_blocked(st, TUE_EVENING + 120))
        core.block_now(st, TUE_EVENING + 180)
        self.assertTrue(core.effective_blocked(st, TUE_EVENING + 181))

    def test_block_until_today_never_shortens(self):
        st = fresh()
        self.assertEqual(core.block_until(st, "20:00", TUE_EVENING), T(2026, 10, 13, 20, 0))
        self.assertEqual(core.describe(st, TUE_EVENING)["headline"], "Blocked until 20:00 (today)")
        self.assertEqual(core.block_until(st, "19:00", TUE_EVENING), T(2026, 10, 13, 20, 0))  # never shorter
        core.block_now(st, TUE_EVENING, minutes=180)                                       # to 21:00
        self.assertEqual(core.block_until(st, "20:30", TUE_EVENING), T(2026, 10, 13, 21, 0))
        self.assertEqual(core.block_until(st, "22:15", TUE_EVENING), T(2026, 10, 13, 22, 15))
        for bad in ("17:59", "8pm", "", "24:00"):
            with self.assertRaises(core.Refused, msg=bad):
                core.block_until(st, bad, TUE_EVENING)
        self.assertEqual(st["block_until"], T(2026, 10, 13, 22, 15))

    def test_block_until_ends_a_pass(self):
        st = fresh()
        core.take_pass(st, TUE_NOON)
        core.block_until(st, "13:00", TUE_NOON + 60)
        self.assertTrue(core.effective_blocked(st, TUE_NOON + 61))
        self.assertEqual(core.describe(st, TUE_NOON + 61)["headline"], "Blocked until 17:00")

    def test_block_now_inside_hours_runs_to_open(self):
        st = fresh()
        self.assertEqual(core.block_now(st, TUE_NOON), T(2026, 10, 13, 17, 0))
        self.assertEqual(core.block_now(st, T(2026, 10, 13, 16, 0)), T(2026, 10, 13, 17, 30))

    def test_describe(self):
        st = fresh()
        d = core.describe(st, TUE_NOON)
        self.assertEqual(d["headline"], "Blocked until 17:00")
        self.assertEqual(d["next"], "Then open from 17:00 to tomorrow 00:00.")
        d = core.describe(st, T(2026, 10, 16, 18, 0))  # Friday evening: open through the weekend
        self.assertEqual(d["headline"], "Open until Mon 00:00")


if __name__ == "__main__":
    unittest.main()
