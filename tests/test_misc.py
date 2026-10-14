"""Flip-timer rendering, the report, and the no-way-out rule for docs and help."""
import contextlib
import io
import re
import unittest
from pathlib import Path

from focus import cli, core, report, timers

from .helpers import AppCase, T, TUE_NOON, fresh, lst

REPO = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"\bstop\b|\bdisabl|\bkill|uninstall|--remove|state\.json|\bmask\b|unblock[- ]all|\bunload\b",
                       re.I)


class TestTimers(unittest.TestCase):
    def test_render_default(self):
        text = timers.render_dropin(fresh()["settings"])
        self.assertIn("OnCalendar=\n", text)  # resets the defaults in the base unit
        self.assertIn("OnCalendar=*-*-* 00:00:00 America/New_York", text)
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 17:00:00 America/New_York", text)
        self.assertEqual(text.count("OnCalendar="), 3)

    def test_render_both_edges(self):
        st = fresh()
        for name in ("Video", "Social"):
            lst(st, name)["hours"].update({"monday": "09:00-12:00", "tuesday": "00:00-08:00", "saturday": "10:00-17:00"})
        text = timers.render_dropin(st["settings"])
        self.assertIn("OnCalendar=Wed,Thu,Fri,Sat *-*-* 17:00:00 America/New_York", text)  # Sat closes at 17:00
        self.assertIn("OnCalendar=Mon *-*-* 09:00:00 America/New_York", text)
        self.assertIn("OnCalendar=Mon *-*-* 12:00:00 America/New_York", text)
        self.assertIn("OnCalendar=Tue *-*-* 08:00:00 America/New_York", text)
        self.assertIn("OnCalendar=Sat *-*-* 10:00:00 America/New_York", text)
        self.assertNotIn("Sun", text)

    def test_render_groups_times(self):
        st = fresh()
        for name in ("Video", "Social"):
            lst(st, name)["hours"].update({"friday": "15:30", "saturday": "10:00", "sunday": "blocked"})
        text = timers.render_dropin(st["settings"])
        self.assertIn("OnCalendar=Fri *-*-* 15:30:00 America/New_York", text)
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu *-*-* 17:00:00 America/New_York", text)
        self.assertIn("OnCalendar=Sat *-*-* 10:00:00 America/New_York", text)
        self.assertNotIn("Sun", text)


class TestReport(unittest.TestCase):
    def test_report_counts(self):
        st = fresh()
        core.take_pass(st, TUE_NOON)
        st["usage"] = {"at": TUE_NOON, "month": "2026-10", "queries": 12345}
        text = report.render(st, TUE_NOON + 3600)
        self.assertIn("12,345 of 300,000", text)
        self.assertIn("Passes used this week (since Mon 10-12): 1", text)
        self.assertIn("This week: Mon 0, Tue 1, Wed 0, Thu 0, Fri 0, Sat 0, Sun 0", text)
        self.assertEqual(report.passes_this_week(st, TUE_NOON), 1)

    def test_week_is_monday_to_sunday_not_rolling(self):
        st = fresh()
        st["passes"] = [{"at": T(2026, 10, 11, 12, 0), "minutes": 15},   # last Sunday: not this week
                        {"at": T(2026, 10, 12, 9, 0), "minutes": 15},    # Monday
                        {"at": T(2026, 10, 18, 23, 0), "minutes": 15}]   # Sunday
        week = report.passes_by_day(st, T(2026, 10, 18, 23, 30))
        self.assertEqual([d.strftime("%a") for d, _ in week], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        self.assertEqual(week[0][0].isoformat(), "2026-10-12")
        self.assertEqual(report.passes_this_week(st, T(2026, 10, 18, 23, 30)), 2)
        self.assertEqual(report.passes_this_week(st, T(2026, 10, 19, 0, 0)), 0)  # Monday: a new week
        st["passes"] = st["passes"][:2]
        self.assertEqual(report.passes_this_week(st, T(2026, 10, 12, 9, 1)), 1)          # Sunday before is out


class TestCliLists(AppCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args), app=self.app)
        return code, out.getvalue() + err.getvalue()

    def test_lists_from_the_cli(self):
        code, out = self.run_cli("status", "--local")
        self.assertIn("List Video: blocked until 17:00; youtube (service); Mon–Fri 17:00–00:00 · Sat–Sun all day.", out)
        self.assertIn("List Social: blocked until 17:00; twitter (service), x.com;", out)
        self.assertIn("Always blocked: Porn, Gambling.", out)
        self.assertEqual(self.run_cli("list", "add", "Reading")[0], 0)
        self.assertEqual(self.run_cli("add", "reddit.com")[0], 2)  # which list?
        code, out = self.run_cli("add", "reddit.com", "--list", "reading")
        self.assertEqual((code, out.strip()), (0, "Added reddit.com to Reading."))
        code, out = self.run_cli("hours", "Reading", "weekdays", "07:00-09:00,17:00")
        self.assertIn("Longer open hours for Reading Mon, Tue, Wed, Thu, Fri apply at 17:00.", out)
        self.assertIn("(queued: open from 07:00 to 09:00 and from 17:00 to midnight)", out)
        self.assertEqual(self.run_cli("hours", "Nope", "mon", "open")[0], 2)
        code, out = self.run_cli("list")
        self.assertIn("Reading: reddit.com", out)
        code, out = self.run_cli("category", "porn", "off")
        self.assertEqual(out.strip(), "Porn goes off at 17:00.")
        code, out = self.run_cli("category")
        self.assertIn("porn         blocked always  (off at 17:00; undo: focus undo category:porn)", out)
        code, out = self.run_cli("undo")
        self.assertIn("Queued for 17:00:", out)


class TestNoWayOut(AppCase):
    def test_repo_docs_never_explain_a_way_out(self):
        for name in ("README.md", "STATUS.md", "bin/install-unit"):
            p = REPO / name
            if not p.exists():
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                self.assertIsNone(FORBIDDEN.search(line), f"{name}:{n}: {line}")

    def test_help_has_no_way_out(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIsNone(FORBIDDEN.search(buf.getvalue()))
        cmds = set(cli.build_parser()._subparsers._group_actions[0].choices)
        self.assertFalse(cmds & {"stop", "off", "disable", "unblock", "pause", "kill"})

    def test_cli_status_and_refusal_exit_code(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["status", "--local"], app=self.app), 0)
        self.assertIn("Blocked until 17:00.", buf.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["remove", "x.com"], app=self.app), 0)
            cli.main(["status", "--local"], app=self.app)
        self.assertIn("Removes x.com at 17:00.", out.getvalue())
        self.assertIn("Queued for 17:00:", out.getvalue())
        self.assertIn("focus undo remove:x.com", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["undo", "nothing"], app=self.app), 2)
            self.assertEqual(cli.main(["block", "11:00"], app=self.app), 2)
        self.assertIn("has passed today", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["undo", "remove:x.com"], app=self.app), 0)
            cli.main(["undo"], app=self.app)
            cli.main(["block", "16:30"], app=self.app)
        self.assertIn("Nothing queued.", out.getvalue())
        self.assertIn("Blocked until 17:00 (today).", out.getvalue())
        self.assertEqual(self.st()["pending"], [])


if __name__ == "__main__":
    unittest.main()


class TestLink(AppCase):
    def test_link_prints_url_and_qr(self):
        self.cfg.token_file.write_text("tok123tok123tok123tok123\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["link"], app=self.app)
        self.assertEqual(rc, 0)
        self.assertIn(f"http://{self.cfg.tailnet_ip}:{self.cfg.port}/login?t=tok123tok123tok123tok123", out.getvalue())
        try:
            import qrcode  # noqa: F401
        except ImportError:
            self.assertIn("No QR", out.getvalue())
        else:
            self.assertIn("█", out.getvalue())

    def test_link_page_and_taildrop_argv(self):
        url = "http://100.1.1.1:4861/login?t=abc"
        self.assertIn(f"href='{url}'", cli.link_page(url))
        self.assertEqual(cli.taildrop_cmd(Path("/tmp/focus.html"), "iphone"),
                         ["tailscale", "file", "cp", "/tmp/focus.html", "iphone:"])
        st = {"Peer": {"a": {"HostName": "localhost", "DNSName": "my-phone.example.ts.net.", "OS": "iOS"},
                       "b": {"HostName": "laptop", "DNSName": "laptop.example.ts.net.", "OS": "macOS"}}}
        self.assertEqual(cli.phone_peers(st), ["my-phone"])
