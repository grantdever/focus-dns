"""The focus page on 127.0.0.1:4861 and the tailnet address (FOCUS_TAILNET_IP):4861, nothing else.

Routes
  GET  /            the page
  GET  /login?t=..  sets the device cookie (tailnet), then back to /
  GET  /healthz     "ok"
  POST /pass /block /add /remove /undo /hours /limits /list /category   form posts, then back to /

Checks on every request: the client address must belong to the listener (loopback on 127.0.0.1,
100.64.0.0/10 on the tailnet address) and the Host header must name that listener (blocks DNS
rebinding). Tailnet requests also need the token cookie. POSTs need a same-origin Origin header.
The cookie gives no extra powers: every edit goes through the same lock rules as the CLI.
"""
from __future__ import annotations

import hmac
import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import core, page
from .actions import Focus
from .schedule import DAYS, MAX_WINDOWS, ScheduleError, make_windows

COOKIE = "focus_token"
MAX_BODY = 8192
# The only script allowed is the page's LIGHT toggle, pinned by its hash. Fonts come from Google Fonts.
CSP = ("default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; script-src " + page.SCRIPT_HASH + "; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")
LOOPBACK_NET = ipaddress.ip_network("127.0.0.0/8")


@dataclass(frozen=True)
class Listener:
    role: str                 # "local" or "tailnet"
    bind: str
    hosts: tuple[str, ...]    # Host header names accepted on this listener
    clients: object           # ip_network the peer address must be in
    needs_token: bool


def listeners_for(cfg) -> list[Listener]:
    return [
        Listener("local", "127.0.0.1", ("127.0.0.1", "localhost"), LOOPBACK_NET, False),
        Listener("tailnet", cfg.tailnet_ip, (cfg.tailnet_ip,), TAILNET_NET, True),
    ]


def bind_allowed(cfg, host: str) -> bool:
    """Only 127.0.0.1 and the configured tailnet address, which must itself be a Tailscale address."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if str(ip) == "127.0.0.1":
        return True
    try:
        tip = ipaddress.ip_address(cfg.tailnet_ip)
    except ValueError:
        return False
    return ip == tip and tip in TAILNET_NET


def host_ok(host_header: str | None, lst: Listener, port: int) -> bool:
    h = (host_header or "").strip().lower()
    name, _, p = h.partition(":")
    return bool(name) and name in lst.hosts and (p == "" or p == str(port))


def client_ok(addr: str, lst: Listener) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip in lst.clients


def origin_ok(origin: str | None, host_header: str | None, lst: Listener, port: int) -> bool:
    if not origin:
        return False
    u = urlparse(origin)
    return (u.scheme == "http" and host_ok(u.netloc, lst, port)
            and u.netloc.lower() == (host_header or "").strip().lower())


def token_ok(cookie_header: str | None, token: str | None) -> bool:
    if not token or not cookie_header:
        return False
    try:
        c = SimpleCookie(cookie_header)
    except CookieError:
        return False
    m = c.get(COOKIE)
    return bool(m) and hmac.compare_digest(m.value.encode(), token.encode())


def parse_hours_form(form: dict) -> dict:
    """One list's week. rule_<day> = blocked | open | from; for "from", the windows
    start_<day>_<i> / end_<day>_<i> (i = 0, 1, ...; a blank end means midnight; a blank pair is
    skipped). start_<day> / end_<day> are read as window 0. A `drop` of "<day>:<i>" leaves that
    window out (no windows left: blocked all day)."""
    drop = (form.get("drop") or [""])[0]
    out = {}
    for d in DAYS:
        kind = (form.get(f"rule_{d}") or [""])[0]
        if kind in ("blocked", "open"):
            out[d] = kind
            continue
        if kind != "from":
            raise core.Refused(f"Missing rule for {d}.")
        pairs = []
        dropped = False
        for i in range(MAX_WINDOWS + 2):
            keys = (f"start_{d}_{i}", f"end_{d}_{i}") if i or f"start_{d}_0" in form else (f"start_{d}", f"end_{d}")
            start = (form.get(keys[0]) or [""])[0].strip()
            end = (form.get(keys[1]) or [""])[0].strip()
            if drop == f"{d}:{i}":
                dropped = True
                continue
            if not start and not end:
                continue
            if not start:
                raise core.Refused(f"{d.capitalize()}: give the time each window opens.")
            pairs.append((start, end))
        if not pairs and not dropped:
            raise core.Refused(f"{d.capitalize()}: give the time it opens.")
        try:
            out[d] = make_windows(pairs)
        except ScheduleError as e:
            raise core.Refused(f"{d.capitalize()}: {e}") from None
    return out


def make_handler(cfg, lst: Listener, app: Focus, token_getter=None):
    get_token = token_getter or cfg.read_token

    class Handler(BaseHTTPRequestHandler):
        server_version = "focus"
        sys_version = ""

        def log_message(self, fmt, *args):  # nothing: no paths with tokens, no headers
            pass

        def _send(self, code: int, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                         "X-Frame-Options": "DENY", "Content-Security-Policy": CSP, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _deny(self, msg: str):
            self._send(403, (msg + "\n").encode())
            return False

        def _guard(self, path: str) -> bool:
            if not client_ok(self.client_address[0], lst):
                return self._deny("forbidden")
            if not host_ok(self.headers.get("Host"), lst, cfg.port):
                return self._deny("forbidden host")
            if lst.needs_token and path not in ("/login", "/healthz"):
                if not token_ok(self.headers.get("Cookie"), get_token()):
                    return self._deny("This device is not signed in. Open the login link once on it.")
            return True

        def _redirect(self, where: str, extra=None):
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            if not self._guard(u.path):
                return
            if u.path == "/healthz":
                return self._send(200, b"ok\n")
            if u.path == "/login":
                t = (parse_qs(u.query).get("t") or [""])[0]
                tok = get_token()
                if not tok or not hmac.compare_digest(t.encode(), tok.encode()):
                    time.sleep(1)
                    return self._deny("bad login link")
                return self._redirect("/", {"Set-Cookie": f"{COOKIE}={tok}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"})
            if u.path == "/":
                q = parse_qs(u.query)
                msg = (q.get("msg") or [None])[0]
                err = (q.get("e") or ["0"])[0] == "1"
                more = (q.get("more") or [""])[0].lower().rpartition(":")
                more = (more[0], more[2]) if more[0] and more[2] in DAYS else None
                st = app.state()
                html = page.render(st, app.clock(), msg[:240] if msg else None, err,
                                   insights=app.insights(st["settings"]), more=more)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            self._send(404, b"not found\n")

        do_HEAD = do_GET

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._guard(path):
                return
            if not origin_ok(self.headers.get("Origin"), self.headers.get("Host"), lst, cfg.port):
                return self._deny("cross-origin post refused")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413, b"too large\n")
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace")) if n else {}
            one = lambda k: (form.get(k) or [""])[0]  # noqa: E731
            routes = {
                "/pass": lambda: app.take_pass(),
                "/block": lambda: app.block(until_hhmm=one("until").strip() or None),
                "/undo": lambda: app.undo(one("id")),
                "/add": lambda: app.add(one("domain"), one("list") or None),
                "/remove": lambda: app.remove(one("domain")),
                "/hours": lambda: app.hours(one("list") or None, parse_hours_form(form)),
                "/list": lambda: app.list_add(one("name")),
                "/category": lambda: app.category(one("id"), one("on") == "1"),
                "/limits": lambda: app.limits(passes=_int(one("passes")), minutes=_int(one("minutes"))),
            }
            if path not in routes:
                return self._send(404, b"not found\n")
            err = False
            try:
                msg = routes[path]()
            except core.Refused as e:
                msg, err = str(e), True
            except Exception as e:  # keep the page up; say what broke
                msg, err = f"Error: {type(e).__name__}: {str(e)[:150]}", True
            where = "/?msg=" + quote(msg) + ("&e=1" if err else "")
            plus = one("plus")
            if path == "/hours" and plus in DAYS:
                # "+ Window": the form was saved as typed; come back with a blank window on that day.
                lst_name = one("list")
                if not err and msg == "No change to the hours.":
                    where = "/?"
                where += ("&" if "?" in where and not where.endswith("?") else "") + "more=" + quote(f"{lst_name}:{plus}")
                where += "#hours-" + page.slug(lst_name)
            self._redirect(where)

    return Handler


def _int(v: str):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise core.Refused("Numbers only.") from None


class _FreebindServer(ThreadingHTTPServer):
    """Binds the tailnet address even if tailscale0 is not up yet (IP_FREEBIND); it answers once it is."""
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_IP, getattr(socket, "IP_FREEBIND", 15), 1)
        super().server_bind()


class _Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(cfg, lst: Listener, app: Focus, token_getter=None) -> ThreadingHTTPServer:
    if not bind_allowed(cfg, lst.bind):
        raise SystemExit(f"refusing to bind {lst.bind}: focus listens only on 127.0.0.1 and {cfg.tailnet_ip}")
    cls = _Server if lst.bind == "127.0.0.1" else _FreebindServer
    return cls((lst.bind, cfg.port), make_handler(cfg, lst, app, token_getter))


def serve(cfg, app: Focus, tailnet: bool = True) -> None:
    servers = []
    for lst in listeners_for(cfg):
        if lst.role == "tailnet" and not tailnet:
            continue
        try:
            servers.append(make_server(cfg, lst, app))
        except OSError as e:
            print(f"focus: could not bind {lst.bind}:{cfg.port}: {e.strerror}", flush=True)
    if not servers:
        raise SystemExit("focus: nothing bound")
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    print("focus page on " + ", ".join(f"http://{s.server_address[0]}:{s.server_address[1]}/" for s in servers), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
