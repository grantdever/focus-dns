"""Host / Origin / token checks, bind refusal, and the page itself."""
import http.client
import threading
import unittest
from urllib.parse import urlencode

from focus import server
from focus.server import LOOPBACK_NET, Listener, TAILNET_NET

from .helpers import AppCase, TUE_EVENING, lst

TOKEN = "t" * 43


class ServerCase(AppCase):
    role = "local"

    def setUp(self):
        super().setUp()
        self.cfg.port = 0
        tip = self.cfg.tailnet_ip
        if self.role == "local":
            self.lst = server.listeners_for(self.cfg)[0]
            self.host = "127.0.0.1"
        else:
            # Same checks as the real tailnet listener, served on loopback so tests need no tailscale0.
            self.lst = Listener("tailnet", "127.0.0.1", (tip,), LOOPBACK_NET, True)
            self.host = tip
        self.srv = server.make_server(self.cfg, self.lst, self.app, token_getter=lambda: TOKEN)
        self.cfg.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, host=None, origin=None, cookie=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.cfg.port, timeout=5)
        h = {"Host": host if host is not None else f"{self.host}:{self.cfg.port}"}
        if origin:
            h["Origin"] = origin
        if cookie:
            h["Cookie"] = cookie
        data = None
        if body is not None:
            data = urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read().decode())
        c.close()
        return out

    @property
    def origin(self):
        return f"http://{self.host}:{self.cfg.port}"


class TestLocal(ServerCase):
    def test_page_renders_with_only_the_theme_script(self):
        import base64, hashlib, re
        code, h, body = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Blocked until 17:00.", body)
        self.assertIn("Pass · 15 min", body)
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", body, flags=re.S)
        self.assertEqual(len(scripts), 1)
        self.assertIn('getElementById("theme")', scripts[0])
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        csp = h["Content-Security-Policy"]
        self.assertIn(f"script-src 'sha256-{digest}';", csp)  # the hash pins exactly this script
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp)
        self.assertIn("font-src https://fonts.gstatic.com", csp)
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertNotIn("http://", body)  # no plain-http loads or links
        self.assertEqual(set(re.findall(r'(?:href|src)="(https://[^/"]+)', body)),
                         {"https://fonts.googleapis.com", "https://fonts.gstatic.com"})
        self.assertRegex(body, "of 300,000|not fetched yet")

    def test_page_uses_the_design_tokens(self):
        import re
        _, _, body = self.req("GET", "/")
        css = re.search(r"<style>(.*?)</style>", body, flags=re.S).group(1)
        for token in ("--bg:#0f0d14", "--accent:var(--wisteria)", "--wisteria:#a78bfa", "--fill:var(--amethyst)",
                      ":root.light{--bg:#f9f8fc"):
            self.assertIn(token, css)
        tokens_end = css.index("::selection")
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", css[tokens_end:]), [])  # no colors outside the tokens
        self.assertNotIn("prefers-color-scheme", body)
        self.assertNotIn("border-radius:1", css)
        self.assertIn('id="theme">LIGHT</button>', body)
        self.assertIn("Geist", body)

    def test_one_accent_pass_or_state_line(self):
        _, _, body = self.req("GET", "/")
        self.assertRegex(body, r'class="btn big fill">Pass · 15 min')
        self.assertNotIn('class="now lead"', body)
        self.clock.t = TUE_EVENING  # nothing to pass: the state line carries the accent
        _, _, body = self.req("GET", "/")
        self.assertNotIn("btn big fill", body)
        self.assertIn('class="now lead"', body)

    def test_page_never_offers_a_way_out(self):
        import re
        _, _, body = self.req("GET", "/")
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)>.*?</\1>", "", body, flags=re.S)).lower()
        for word in ("stop", "disable", "unblock all", "uninstall", "kill", "pause", "turn off"):
            self.assertNotIn(word, text)
        actions = set(re.findall(r'action="([^"]+)"', body))
        self.assertEqual(actions, {"/pass", "/block", "/add", "/remove", "/hours", "/limits", "/list", "/category"})

    def test_localhost_name_ok(self):
        self.assertEqual(self.req("GET", "/", host=f"localhost:{self.cfg.port}")[0], 200)

    def test_bad_host_refused(self):
        for h in ("evil.example", f"evil.example:{self.cfg.port}", f"127.0.0.1:{self.cfg.port + 1}",
                  f"100.64.0.1:{self.cfg.port}", ""):
            self.assertEqual(self.req("GET", "/", host=h)[0], 403, h)

    def test_post_needs_same_origin(self):
        self.assertEqual(self.req("POST", "/add", body={"domain": "reddit.com", "list": "Social"})[0], 403)
        self.assertEqual(self.req("POST", "/add", origin="http://evil.example", body={"domain": "reddit.com", "list": "Social"})[0], 403)
        self.assertEqual(self.req("POST", "/add", origin=f"http://localhost:{self.cfg.port}", body={"domain": "reddit.com", "list": "Social"})[0], 403)
        self.assertEqual(self.req("POST", "/add", origin="null", body={"domain": "reddit.com", "list": "Social"})[0], 403)
        self.assertNotIn("reddit.com", lst(self.st(), "Social")["domains"])
        code, h, _ = self.req("POST", "/add", origin=self.origin, body={"domain": "reddit.com", "list": "Social"})
        self.assertEqual(code, 303)
        self.assertIn("reddit.com", lst(self.st(), "Social")["domains"])

    def test_lock_rules_on_the_page(self):
        code, h, _ = self.req("POST", "/remove", origin=self.origin, body={"domain": "x.com"})
        self.assertEqual(code, 303)
        self.assertIn("Removes%20x.com%20at%2017%3A00", h["Location"])
        self.assertIn("x.com", lst(self.st(), "Social")["domains"])
        _, _, body = self.req("GET", "/")
        self.assertIn("Removes at 17:00", body)
        self.assertIn('form="undo-remove-x-com">Undo</button>', body)
        self.assertIn('<form method="post" action="/undo" id="undo-remove-x-com"><input type="hidden" name="id" value="remove:x.com">', body)
        code, h, _ = self.req("POST", "/undo", origin=self.origin, body={"id": "remove:x.com"})
        self.assertIn("Undone", h["Location"])
        self.assertEqual(self.st()["pending"], [])
        _, _, body = self.req("GET", "/")
        self.assertNotIn("Removes at", body)

    def test_block_until_on_the_page(self):
        self.clock.t = TUE_EVENING
        _, h, _ = self.req("POST", "/block", origin=self.origin, body={"until": "20:00"})
        self.assertIn("Blocked%20until%2020%3A00%20%28today%29", h["Location"])
        _, _, body = self.req("GET", "/")
        self.assertIn("Blocked until 20:00 (today).", body)
        _, h, _ = self.req("POST", "/block", origin=self.origin, body={"until": "19:00"})  # never shortens
        self.assertEqual(self.st()["block_until"], TUE_EVENING + 2 * 3600)
        _, h, _ = self.req("POST", "/block", origin=self.origin, body={})                  # plain Block now: 90 min
        self.assertEqual(self.st()["block_until"], TUE_EVENING + 2 * 3600)

    def test_week_row_on_the_page(self):
        self.app.take_pass()
        _, _, body = self.req("GET", "/")
        self.assertIn('Passes used this week: <span class="num">1</span>', body)
        import re
        days = re.findall(r'<li class="[a-z]*"><span class="mono">(\w+)</span>', body)
        self.assertEqual(days, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    def test_hours_form(self):
        wk = ("monday", "tuesday", "wednesday", "thursday", "friday")
        form = {f"rule_{d}": "from" for d in wk}
        form.update({f"start_{d}": "17:00" for d in wk})
        form.update({f"end_{d}": "00:00" for d in wk})
        form.update({"rule_saturday": "open", "rule_sunday": "blocked", "start_monday": "17:30",
                     "rule_thursday": "from", "end_thursday": "22:00", "end_friday": "", "list": "Social"})
        code, h, _ = self.req("POST", "/hours", origin=self.origin, body=form)
        self.assertEqual(code, 303)
        self.assertNotIn("e=1", h["Location"])
        hours = lst(self.st(), "Social")["hours"]
        self.assertEqual((hours["monday"], hours["thursday"], hours["friday"], hours["sunday"]),
                         ("17:30-24:00", "17:00-22:00", "17:00-24:00", "blocked"))
        # NextDNS holds the coarse span across lists: Video still opens Thu to midnight.
        self.assertEqual(self.api.data["parentalControl"]["recreation"]["times"]["thursday"],
                         {"start": "17:00:00", "end": "23:59:00"})
        _, _, body = self.req("GET", "/")
        self.assertIn('name="end_thursday_0" value="22:00"', body)
        self.assertIn('name="end_friday_0" value="00:00"', body)
        for bad in ({"start_monday": "5pm"}, {"end_monday": "16:00"}, {"start_monday": ""}):
            _, h, _ = self.req("POST", "/hours", origin=self.origin, body={**form, **bad})
            self.assertIn("e=1", h["Location"], bad)

    def test_wider_hours_are_queued_and_shown_in_place(self):
        form = {f"rule_{d}": "from" for d in ("monday", "tuesday", "wednesday", "thursday", "friday")}
        form.update({f"start_{d}": "17:00" for d in ("monday", "tuesday", "wednesday", "thursday", "friday")})
        form.update({"rule_saturday": "open", "rule_sunday": "open", "start_tuesday": "15:00", "list": "Social"})
        _, h, _ = self.req("POST", "/hours", origin=self.origin, body=form)
        self.assertIn("apply%20at%2017%3A00", h["Location"])
        self.assertEqual(lst(self.st(), "Social")["hours"]["tuesday"], "17:00-24:00")
        _, _, body = self.req("GET", "/")
        self.assertIn("At 17:00: open from 15:00 to midnight", body)
        self.req("POST", "/hours", origin=self.origin, body={**form, "start_tuesday": "17:00"})  # resubmit: kept
        self.assertEqual(self.st()["pending"][0]["id"], "hours:social:tuesday")

    def test_windows_plus_and_remove_on_the_page(self):
        self.clock.t = TUE_EVENING  # outside every block: edits apply at once
        wk = ("monday", "tuesday", "wednesday", "thursday", "friday")
        form = {"list": "Video", "rule_saturday": "open", "rule_sunday": "open"}
        form.update({f"rule_{d}": "from" for d in wk})
        form.update({f"start_{d}_0": "17:00" for d in wk})
        form.update({f"end_{d}_0": "00:00" for d in wk})
        _, h, _ = self.req("POST", "/hours", origin=self.origin, body={**form, "plus": "monday"})
        self.assertIn("more=Video%3Amonday", h["Location"])
        self.assertTrue(h["Location"].endswith("#hours-video"))
        self.assertNotIn("msg=", h["Location"])  # nothing changed, so no message
        _, _, body = self.req("GET", "/?more=Video:monday")
        self.assertIn('name="start_monday_1" value=""', body)
        form.update({"start_monday_1": "07:00", "end_monday_1": "09:00"})
        _, h, _ = self.req("POST", "/hours", origin=self.origin, body=form)
        self.assertNotIn("e=1", h["Location"])
        self.assertEqual(lst(self.st(), "Video")["hours"]["monday"], "07:00-09:00,17:00-24:00")
        self.assertEqual(lst(self.st(), "Social")["hours"]["monday"], "17:00-24:00")
        self.assertEqual(self.api.data["parentalControl"]["recreation"]["times"]["monday"],
                         {"start": "07:00:00", "end": "23:59:00"})  # the coarse span
        _, _, body = self.req("GET", "/")
        self.assertIn('name="drop" value="monday:0"', body)
        self.assertIn('name="start_monday_0" value="07:00"', body)  # the page lists windows in time order
        _, h, _ = self.req("POST", "/hours", origin=self.origin, body={**form, "drop": "monday:1"})  # 07:00-09:00 here
        self.assertEqual(lst(self.st(), "Video")["hours"]["monday"], "17:00-24:00")

    def test_new_list_and_category_on_the_page(self):
        _, h, _ = self.req("POST", "/list", origin=self.origin, body={"name": "Reading"})
        self.assertIn("New%20list%20Reading", h["Location"])
        _, h, _ = self.req("POST", "/add", origin=self.origin, body={"domain": "x.com", "list": "Reading"})
        self.assertIn("e=1", h["Location"])  # one list per domain
        _, _, body = self.req("GET", "/")
        self.assertIn('id="list-reading"', body)
        _, h, _ = self.req("POST", "/category", origin=self.origin, body={"id": "porn", "on": "0"})
        self.assertIn("Porn%20goes%20off%20at%2017%3A00", h["Location"])
        self.assertTrue(self.st()["settings"]["categories"][0]["active"])
        self.assertTrue(self.api._cat("porn")["active"])

    def test_pass_button_greys_out(self):
        for _ in range(3):
            self.app.take_pass()
            self.clock.t += 16 * 60
        _, _, body = self.req("GET", "/")
        self.assertIn("Pass · 0 left", body)
        self.assertRegex(body, r'class="btn big" disabled>Pass · 0 left')

    def test_no_token_needed_on_loopback(self):
        self.assertEqual(self.req("GET", "/")[0], 200)


class TestTailnet(ServerCase):
    role = "tailnet"

    def test_needs_cookie(self):
        self.assertEqual(self.req("GET", "/")[0], 403)
        self.assertEqual(self.req("GET", "/", cookie="focus_token=wrong")[0], 403)
        self.assertEqual(self.req("GET", "/", cookie=f"focus_token={TOKEN}")[0], 200)
        self.assertEqual(self.req("GET", "/healthz")[0], 200)

    def test_login_sets_cookie(self):
        self.assertEqual(self.req("GET", "/login?t=nope")[0], 403)
        code, h, _ = self.req("GET", f"/login?t={TOKEN}")
        self.assertEqual(code, 303)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])

    def test_loopback_host_refused_on_tailnet_listener(self):
        self.assertEqual(self.req("GET", "/", host=f"127.0.0.1:{self.cfg.port}", cookie=f"focus_token={TOKEN}")[0], 403)

    def test_post_needs_cookie_and_origin(self):
        c = f"focus_token={TOKEN}"
        self.assertEqual(self.req("POST", "/add", origin=self.origin, body={"domain": "a.com", "list": "Social"})[0], 403)
        self.assertEqual(self.req("POST", "/add", cookie=c, body={"domain": "a.com", "list": "Social"})[0], 403)
        self.assertEqual(self.req("POST", "/add", cookie=c, origin=self.origin, body={"domain": "a.com", "list": "Social"})[0], 303)

    def test_cookie_gives_no_extra_powers(self):
        c = f"focus_token={TOKEN}"
        _, h, _ = self.req("POST", "/remove", cookie=c, origin=self.origin, body={"domain": "x.com"})
        self.assertIn("Removes%20x.com%20at%2017%3A00", h["Location"])
        self.assertIn("x.com", lst(self.st(), "Social")["domains"])  # queued like anywhere else, not applied


class TestBind(unittest.TestCase):
    def test_refuses_other_hosts(self):
        from focus.config import Config
        cfg = Config(systemd=False, port=0, tailnet_ip="100.64.0.1")
        for bad in ("0.0.0.0", "::", "192.168.1.10", "100.64.0.2", "10.0.0.1", "localhost", ""):
            with self.assertRaises(SystemExit, msg=bad):
                server.make_server(cfg, Listener("x", bad, (), LOOPBACK_NET, True), app=None)
        cfg.tailnet_ip = "0.0.0.0"  # a bad config cannot widen the bind either
        with self.assertRaises(SystemExit):
            server.make_server(cfg, Listener("x", "0.0.0.0", (), LOOPBACK_NET, True), app=None)

    def test_allowed_binds(self):
        from focus.config import Config
        cfg = Config(systemd=False, tailnet_ip="100.64.0.1")
        self.assertTrue(server.bind_allowed(cfg, "127.0.0.1"))
        self.assertTrue(server.bind_allowed(cfg, "100.64.0.1"))
        self.assertEqual([l.bind for l in server.listeners_for(cfg)], ["127.0.0.1", "100.64.0.1"])

    def test_client_ranges(self):
        from focus.config import Config
        tail = server.listeners_for(Config(systemd=False, tailnet_ip="100.64.0.1"))[1]
        self.assertTrue(server.client_ok("100.101.2.3", tail))
        self.assertFalse(server.client_ok("127.0.0.1", tail))
        self.assertFalse(server.client_ok("192.168.1.5", tail))
        local = server.listeners_for(Config(systemd=False, tailnet_ip="100.64.0.1"))[0]
        self.assertFalse(server.client_ok("100.101.2.3", local))
        self.assertTrue(TAILNET_NET.overlaps(server.ipaddress.ip_network("100.64.0.1/32")))


if __name__ == "__main__":
    unittest.main()
