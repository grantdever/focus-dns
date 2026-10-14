"""focus: sites stay blocked during their list's hours; a pass is the only valve.

  focus status              where things stand, per list, and whether NextDNS matches
  focus pass [MIN]          open every list for MIN minutes (default and max: the pass length)
  focus block [MIN|HH:MM]   block every list now for MIN minutes (default 90) or until HH:MM today;
                            never shortens a block
  focus list                the lists, their sites and their hours
  focus list add NAME       a new, empty list (focus list remove NAME takes an empty one away)
  focus add DOMAIN --list NAME    block another domain on that list (applies at once)
  focus remove DOMAIN       take a domain off its list (during that list's block: queued for its unlock)
  focus hours [LIST [DAY RULE]]   show the week, or set DAY (mon..sun, weekdays, weekend, all) of LIST to
                            blocked, open, or open windows: HH:MM-HH:MM[,HH:MM-HH:MM...]
                            (HH:MM alone means open to midnight)
  focus hours tz ZONE       set the timezone (during a block: queued for the unlock)
  focus category [ID on|off]      the always-blocked categories (off: queued during a block)
  focus limits [--passes N] [--minutes M]   passes per day and pass length
  focus undo [ID]           list queued changes, or cancel one by its ID

  focus report [--save]     lookups this month and passes this week
  focus reconcile [--dry-run]   make NextDNS match (the timers run this)
  focus doctor              tailnet bind and DNS, API, and what the resolver answers for each target
  focus link                the one-time login link for the phone (over Tailscale)

Tightening edits apply at once. During a list's block, loosening edits to it (remove a domain,
wider or extra open windows) wait for that list's unlock; more or longer passes, a new timezone
and turning a category off wait until no list is blocked. `focus status` lists what is queued.
"""
from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core, report, timers, usage
from . import schedule as sch
from .actions import Focus
from .api import ApiError
from .config import Config, QUERY_CAP

SERVICE_HOSTS = {"youtube": "youtube.com", "twitter": "twitter.com"}
CATEGORY_PROBES = {"porn": "pornhub.com", "gambling": "bet365.com"}


def _hours_table(st: dict, L: dict) -> str:
    q = {p["day"]: p for p in core.pending(st)
         if p.get("id", "").startswith("hours:") and (p.get("list") or "").lower() == L["name"].lower()}
    rows = [f"{L['name']}:"]
    for d in sch.DAYS:
        row = f"  {sch.ABBR[d]}  {sch.describe_rule(L['hours'][d])}"
        if d in q:
            row += f"   (queued: {sch.describe_rule(q[d]['rule'])})"
        rows.append(row)
    return "\n".join(rows)


def _all_hours(st: dict) -> str:
    return "\n".join(_hours_table(st, L) for L in core.lists(st)) + f"\n(times are {st['settings']['timezone']})"


def _pending_lines(st: dict, ts: float) -> list[str]:
    items = core.pending(st)
    if not items:
        return []
    whens = [core.pending_when(st, ts, p) for p in items]
    if len(set(whens)) == 1:
        out = [f"Queued for {whens[0]}:"]
        for p in items:
            out.append(f"  {core.describe_pending(st, p):<40} undo: focus undo {p['id']}")
    else:
        out = ["Queued:"]
        for p, w in zip(items, whens):
            out.append(f"  {core.describe_pending(st, p):<40} at {w:<14} undo: focus undo {p['id']}")
    return out


def _sites(L: dict) -> str:
    parts = [f"{s} (service)" for s in L["services"]] + L["domains"]
    return ", ".join(parts) or "no sites yet"


def cmd_status(app: Focus, a) -> int:
    st = app.state()
    ts = app.clock()
    info = core.describe(st, ts)
    print(info["headline"] + ".")
    print(info["next"])
    s = st["settings"]
    print(f"Passes left today: {info['passes_left']} of {s['passes_per_day']} ({s['pass_minutes']} min each).")
    for L, p in zip(core.lists(st), info["lists"]):
        state = "blocked" if p["blocked"] else "open"
        print(f"List {L['name']}: {state} until {p['until']}; {_sites(L)}; {sch.summarize_week(L['hours'])}.")
    cats = core.categories(st)
    if cats:
        print("Always blocked: " + ", ".join(
            f"{core.category_name(c['id'])}{'' if c['active'] else ' (off)'}" for c in cats) + ".")
    for line in _pending_lines(st, ts):
        print(line)
    if a.local:
        print(report.usage_line(st, ts))
        return 0
    try:
        diffs = app.reconcile(dry_run=True)
        print("NextDNS: in sync." if not diffs else "NextDNS differs (the next reconcile fixes it): " + "; ".join(diffs))
        print(report.usage_line({**st, "usage": app.refresh_usage()}, ts))
    except ApiError as e:
        print(f"NextDNS: not reachable ({e}).")
        print(report.usage_line(st, ts))
    return 0


def cmd_pass(app, a):
    print(app.take_pass(a.minutes))
    return 0


def cmd_block(app, a):
    v = (a.when or "").strip()
    if ":" in v:
        print(app.block(until_hhmm=v))
    elif v:
        try:
            print(app.block(int(v)))
        except ValueError:
            raise core.Refused("usage: focus block [MINUTES | HH:MM]") from None
    else:
        print(app.block())
    return 0


def cmd_undo(app, a):
    if not a.id:
        lines = _pending_lines(app.state(), app.clock())
        print("\n".join(lines) if lines else "Nothing queued.")
        return 0
    print(app.undo(a.id))
    return 0


def cmd_add(app, a):
    print(app.add(a.domain, a.list))
    return 0


def cmd_list(app, a):
    args = a.args
    if not args:
        st = app.state()
        for L in core.lists(st):
            print(f"{L['name']}: {_sites(L)}")
            print(f"  {sch.summarize_week(L['hours'])}")
        return 0
    verb, rest = args[0].lower(), " ".join(args[1:])
    if verb == "add" and rest:
        print(app.list_add(rest))
    elif verb == "remove" and rest:
        print(app.list_remove(rest))
    else:
        raise core.Refused("usage: focus list | focus list add NAME | focus list remove NAME")
    return 0


def cmd_category(app, a):
    if not a.args:
        st = app.state()
        for c in core.categories(st):
            p = core.find_pending(st, f"category:{c['id']}")
            q = f"  (off at {core.pending_when(st, app.clock(), p)}; undo: focus undo {p['id']})" if p else ""
            print(f"{c['id']:<12} {'blocked always' if c['active'] else 'off'}{q}")
        return 0
    if len(a.args) != 2 or a.args[1].lower() not in ("on", "off"):
        raise core.Refused("usage: focus category ID on|off")
    print(app.category(a.args[0], a.args[1].lower() == "on"))
    return 0


def cmd_remove(app, a):
    print(app.remove(a.domain))
    return 0


def cmd_hours(app, a):
    args = list(a.args)
    st = app.state()
    if not args:
        print(_all_hours(st))
        return 0
    if args[0].lower() in ("tz", "timezone"):
        if len(args) != 2:
            raise core.Refused("usage: focus hours tz ZONE")
        print(app.timezone(args[1]))
        return 0
    if len(args) == 2 and args[0].lower() in sch.DAY_ALIASES and len(core.lists(st)) == 1:
        args = [core.lists(st)[0]["name"]] + args
    L = core.get_list(st, args[0])
    if len(args) == 1:
        print(_hours_table(st, L))
        return 0
    if len(args) != 3:
        raise core.Refused("usage: focus hours LIST DAY RULE   (RULE: blocked, open, or 07:00-09:00,17:00-24:00)")
    key, val = args[1].lower(), args[2]
    days = sch.DAY_ALIASES.get(key)
    if not days:
        raise core.Refused(f"Unknown day {key!r}: use mon..sun, weekdays, weekend or all.")
    print(app.hours(L["name"], {d: val for d in days}))
    st = app.state()
    print(_hours_table(st, core.get_list(st, L["name"])))
    return 0


def cmd_limits(app, a):
    if a.passes is None and a.minutes is None:
        st = app.state()
        s = st["settings"]
        print(f"{s['passes_per_day']} passes a day, {s['pass_minutes']} min each.")
        for line in _pending_lines(st, app.clock()):
            print(line)
        return 0
    print(app.limits(passes=a.passes, minutes=a.minutes))
    return 0


def cmd_report(app, a):
    try:
        app.refresh_usage()
    except ApiError as e:
        print(f"(NextDNS not reachable: {e}; using the last count)")
    ts = app.clock()
    st = app.state()
    text = report.render(st, ts)
    if usage.db_path(app.cfg).exists():
        monday = sch.local(st["settings"], ts).weekday() == 0
        text += "\n" + report.usage_section(app.cfg, st, ts, last_week=a.last_week or (a.save and monday))
    print(text, end="")
    if a.save:
        d = app.cfg.reports_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{sch.local(app.state()['settings'], ts):%Y-%m-%d}.txt"
        p.write_text(text)
        print(f"saved {p}")
        prune_reports(d, ts)
    return 0


KEEP_REPORTS_DAYS = 8 * 7


def prune_reports(d, ts: float) -> None:
    for f in d.glob("*.txt"):
        try:
            if ts - f.stat().st_mtime > KEEP_REPORTS_DAYS * 86400:
                f.unlink()
        except OSError:
            pass


RETENTION = {"1h": 3600, "6h": 21600, "1d": 86400, "7d": 604800, "30d": 2592000, "90d": 7776000}


def cmd_usage(app, a):
    if a.what == "collect":
        print(app.collect_usage())
        return 0
    if a.what == "retention":
        if a.value and a.value not in RETENTION:
            raise core.Refused("usage: focus usage retention [" + "|".join(RETENTION) + "]")
        cur = app.logs_retention(RETENTION[a.value] if a.value else None)
        name = {v: k for k, v in RETENTION.items()}.get(cur.get("retention"), f'{cur.get("retention")} s')
        print(f"NextDNS keeps logs for {name} (logging {'on' if cur.get('enabled') else 'off'}, "
              f"stored in {str(cur.get('location', '?')).upper()}).")
        return 0
    st = app.state()
    print(report.usage_section(app.cfg, st, app.clock(), last_week=a.what == "last"), end="")
    return 0


def cmd_reconcile(app, a):
    changes = app.reconcile(dry_run=a.dry_run)
    if not changes:
        print("Nothing to change.")
    else:
        print(("Would change: " if a.dry_run else "Changed: ") + "; ".join(changes))
    return 0


def _page_answers(host: str, port: int) -> str:
    try:
        c = http.client.HTTPConnection(host, port, timeout=3)
        c.request("GET", "/healthz", headers={"Host": f"{host}:{port}"})
        r = c.getresponse()
        return "answers" if r.status == 200 else f"HTTP {r.status}"
    except OSError as e:
        return f"not answering ({e.strerror or e})"


def _addr_is_local(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def cmd_doctor(app, a):
    cfg = app.cfg
    st = app.state()
    ts = app.clock()
    ok = True
    tip = cfg.tailnet_ip
    local = _addr_is_local(tip)
    print(f"Tailnet address {tip}: {'on this machine (tailscale0)' if local else 'NOT on this machine; is Tailscale up?'}")
    ok &= local
    for h in ("127.0.0.1", tip):
        r = _page_answers(h, cfg.port)
        print(f"Page http://{h}:{cfg.port}/: {r}")
        ok &= r == "answers"
    try:
        prof = app.api.profile_data()
        print(f"NextDNS API: answers (profile {app.api.profile}, \"{prof.get('name', '?')}\")")
        diffs = app.reconcile(dry_run=True)
        print("NextDNS settings: in sync with focus" if not diffs else "NextDNS settings differ: " + "; ".join(diffs))
    except ApiError as e:
        print(f"NextDNS API: FAILED ({e})")
        ok = False
    prof_id = getattr(app.api, "profile", "?") if _has_env(app) else "?"
    print(f"Resolver dns.nextdns.io/{prof_id} (what focus expects in brackets):")
    checks = []
    for L in core.lists(st):
        want = "blocked" if core.list_blocked(st, L, ts) else "open"
        checks += [(SERVICE_HOSTS.get(x, f"{x}.com"), want, L["name"]) for x in L["services"]]
        checks += [(d, want, L["name"]) for d in L["domains"]]
    for c in core.categories(st):
        if c["id"] in CATEGORY_PROBES:
            checks.append((CATEGORY_PROBES[c["id"]], "blocked" if c["active"] else "open", core.category_name(c["id"])))
    checks.append(("example.com", "open", "control"))
    for n, want, label in checks:
        try:
            ips = app.api.resolve(n)
            got = "blocked" if ips and all(i == "0.0.0.0" for i in ips) else "open"
            print(f"  {n:<16} {', '.join(ips[:2]) or '(no A record)':<28} {got:<8} [{label}: {want}]"
                  f"{'' if got == want else '  <- differs'}")
        except ApiError as e:
            print(f"  {n:<16} lookup failed ({e})")
            ok = False
    for line in tailscale_dns_lines(_tailscale_dns_status(), prof_id):
        print(line)
    if cfg.systemd:
        units = ["focus.service", "focus-reconcile.timer", "focus-flip.timer", "focus-report.timer"]
        p = subprocess.run(["systemctl", "--user", "is-active", *units], capture_output=True, text=True)
        states = p.stdout.split()
        print("Units: " + ", ".join(f"{u} {s}" for u, s in zip(units, states)))
        ok &= all(s == "active" for s in states)
    try:
        fresh = cfg.flip_dropin.read_text() == timers.render_dropin(st["settings"])
    except OSError:
        fresh = False
    print(f"Flip timer schedule: {'matches the hours' if fresh else 'missing or stale (the next reconcile rewrites it)'}")
    lr = st.get("last_reconcile")
    if lr:
        print(f"Last reconcile: {core.when(st, lr['at'], ts)}, " + ("ok" if lr.get("ok") else f"failed: {lr.get('error')}"))
    print(report.usage_line(st, ts))
    return 0 if ok else 1


def _has_env(app) -> bool:
    try:
        app.api
        return True
    except ApiError:
        return False


def _tailscale_dns_status() -> str | None:
    try:
        p = subprocess.run(["tailscale", "dns", "status"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def tailscale_dns_lines(text: str | None, profile: str) -> list[str]:
    """Two facts from `tailscale dns status`: is a NextDNS resolver the tailnet's global nameserver,
    and does this node accept tailnet DNS. Reports only; changes nothing."""
    if text is None:
        return ["Tailscale DNS: `tailscale dns status` did not answer."]
    accept = re.search(r"Tailscale DNS:\s*(enabled|disabled)", text)
    resolvers: list[str] = []
    m = re.search(r"Resolvers \(in preference order\):\n(.*?)(?:\n\s*\n|\Z)", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if line and not line.startswith("("):
                resolvers.append(line)
    nextdns = [r for r in resolvers if "nextdns" in r.lower() or (profile not in ("", "?") and profile in r)]
    if nextdns:
        ns = "Tailnet global nameserver: NextDNS (" + ", ".join(nextdns) + ")"
        if len(nextdns) < len(resolvers):
            ns += "; also " + ", ".join(r for r in resolvers if r not in nextdns)
    elif resolvers:
        ns = "Tailnet global nameserver: not NextDNS (" + ", ".join(resolvers) + ")"
    else:
        ns = "Tailnet global nameserver: none set (devices use their own resolver)"
    if accept:
        acc = ("This node accepts tailnet DNS (accept-dns on)" if accept.group(1) == "enabled"
               else "This node does not accept tailnet DNS (accept-dns off)")
    else:
        acc = "This node: could not tell whether it accepts tailnet DNS"
    return [ns + ".", acc + "."]


def link_url(cfg, tok: str) -> str:
    return f"http://{cfg.tailnet_ip}:{cfg.port}/login?t={tok}"


def link_qr(url: str) -> str | None:
    """The login link as a terminal QR code, or None when the qrcode module is missing."""
    try:
        import qrcode  # pure Python; python3 -m pip install --user qrcode
    except ImportError:
        return None
    import io
    q = qrcode.QRCode(border=1)
    q.add_data(url)
    q.make()
    out = io.StringIO()
    q.print_ascii(out=out, invert=True)
    return out.getvalue()


def link_page(url: str) -> str:
    """A one-link page to Taildrop to the phone (tap the link, it opens in Safari)."""
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Focus</title><body style='font:20px system-ui;padding:24px;background:#111;color:#eee'>"
            f"<p><a href='{url}' style='color:#b39cff'>Open Focus and log in</a></p>"
            "<p>Then Share, Add to Home Screen.</p>")


def phone_peers(status_json: dict) -> list[str]:
    """Hostnames of iOS peers from `tailscale status --json`."""
    names = []
    for p in (status_json.get("Peer") or {}).values():
        if p.get("OS") != "iOS":
            continue
        # iPhones report HostName "localhost"; the tailnet name is the usable target.
        name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName", "")
        if name:
            names.append(name)
    return sorted(names)


def taildrop_cmd(path: Path, device: str) -> list[str]:
    return ["tailscale", "file", "cp", str(path), f"{device}:"]


def cmd_link(app, a):
    tok = app.cfg.read_token()
    if not tok:
        raise core.Refused(f"No token yet at {app.cfg.token_file}; run bin/install-unit.")
    url = link_url(app.cfg, tok)
    print(f"Open once on the phone, over Tailscale, then Add to Home Screen:\n  {url}")
    if not a.no_qr:
        qr = link_qr(url)
        print("\n" + qr if qr else "\n(No QR: python3 -m pip install --user qrcode)")
    if a.send is not None:
        device = a.send
        if not device:
            st = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, check=True).stdout)
            phones = phone_peers(st)
            if len(phones) != 1:
                raise core.Refused(f"Say which device: --send NAME (iOS peers: {', '.join(phones) or 'none'})")
            device = phones[0]
        page = Path(tempfile.gettempdir()) / "focus.html"
        page.write_text(link_page(url))
        r = subprocess.run(taildrop_cmd(page, device), capture_output=True, text=True)
        page.unlink(missing_ok=True)
        if r.returncode != 0:
            raise core.Refused(f"Taildrop to {device} failed: {(r.stderr or r.stdout).strip()}")
        print(f"\nSent focus.html to {device} over Tailscale (open it from the Tailscale app, tap the link).")
    return 0


def cmd_serve(app, a):
    from .server import serve
    serve(app.cfg, app, tailnet=not a.no_tailnet)
    return 0


def cmd_timers(app, a):
    st = app.state()
    if a.write:
        changed = timers.sync_dropin(app.cfg, st["settings"])
        print(f"{'wrote' if changed else 'unchanged'} {app.cfg.flip_dropin}")
    else:
        print(timers.render_dropin(st["settings"]), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="focus", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = sub.add_parser("status", help="where things stand")
    p.add_argument("--local", action="store_true", help="skip the NextDNS check")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("pass", help="open the targets for a few minutes")
    p.add_argument("minutes", nargs="?", type=int)
    p.set_defaults(fn=cmd_pass)
    p = sub.add_parser("block", help="block now (90 min, MINUTES, or until HH:MM today)")
    p.add_argument("when", nargs="?", metavar="MINUTES|HH:MM")
    p.set_defaults(fn=cmd_block)
    p = sub.add_parser("add", help="block a domain on a list")
    p.add_argument("domain")
    p.add_argument("--list", metavar="NAME", help="the list it joins (needed when there is more than one)")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list", help="show the lists, or add one")
    p.add_argument("args", nargs="*", metavar="add NAME")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("category", help="the always-blocked categories")
    p.add_argument("args", nargs="*", metavar="ID on|off")
    p.set_defaults(fn=cmd_category)
    p = sub.add_parser("remove", help="take a domain off its list (during its block: queued for the unlock)")
    p.add_argument("domain")
    p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("hours", help="show or set the weekly hours")
    p.add_argument("args", nargs="*", metavar="LIST DAY RULE")
    p.set_defaults(fn=cmd_hours)
    p = sub.add_parser("undo", help="list queued changes, or cancel one")
    p.add_argument("id", nargs="?", metavar="ID")
    p.set_defaults(fn=cmd_undo)
    p = sub.add_parser("limits", help="show or set pass count and length")
    p.add_argument("--passes", type=int)
    p.add_argument("--minutes", type=int)
    p.set_defaults(fn=cmd_limits)
    p = sub.add_parser("report", help="lookups and passes")
    p.add_argument("--save", action="store_true", help="also write it under the state folder's reports/")
    p.add_argument("--last-week", action="store_true", help="time online for last week, not this one")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("usage", help="estimated time online this week (or last), kept on this machine")
    p.add_argument("what", nargs="?", default="week", choices=["week", "last", "collect", "retention"])
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("reconcile", help="make NextDNS match")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_reconcile)
    sub.add_parser("doctor", help="check the bind, tailnet DNS, the API and the resolver").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("link", help="print the phone login link as a URL and a QR code")
    p.add_argument("--send", nargs="?", const="", default=None, metavar="DEVICE",
                   help="also Taildrop a one-link page to the phone (default: the tailnet's only iOS device)")
    p.add_argument("--no-qr", action="store_true")
    p.set_defaults(fn=cmd_link)
    p = sub.add_parser("serve", help="run the page (the focus.service unit does this)")
    p.add_argument("--no-tailnet", action="store_true", help="loopback only")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("timers", help="print (or --write) the generated flip timer schedule")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_timers)
    return ap


def main(argv=None, app: Focus | None = None) -> int:
    a = build_parser().parse_args(argv)
    app = app or Focus(Config())
    try:
        return a.fn(app, a)
    except core.Refused as e:
        print(str(e), file=sys.stderr)
        return 2
    except (ApiError, sch.ScheduleError) as e:
        print(f"focus: {e}", file=sys.stderr)
        return 1
