# focus-dns

A self-hosted alternative to website blockers like [Freedom](https://freedom.to), built on NextDNS and Tailscale.

Distracting sites sit in named **lists** (for example *Video*: YouTube; *Social*: Twitter and X). Each list has its
own weekly hours, and outside those hours the sites are blocked on every device you own, at the DNS level. Low quality websites of various types are automatically banned at all times. A small web page and a CLI, running on one always-on machine
at home, let you take a short **pass**, **block now**, and edit the lists and hours.

The design rule is that **friction is the product**. There is no off switch. A pass (15 minutes, 3 a day by default)
is the only way to open things early. Any edit that would loosen the rules during a block is queued until that
list's unlock rather than applied, so you can't buy your way out in a weak moment.

> Status: personal software that runs every day for one household. It is shared so others can fork it and build
> their own version, not as a polished product.

---

## The tools, and why each one

| Tool | What it is | Why it's here |
|---|---|---|
| **[NextDNS](https://nextdns.io)** | A hosted DNS resolver with a firewall: blocklists, per-site "services", parental-control categories, schedules ("Recreation Time"), logs and an HTTP API. Free up to 300,000 lookups a month. | **It does the blocking.** When a blocked name is looked up, NextDNS answers `0.0.0.0`, so the site fails to load in any app or browser. Because enforcement lives in the cloud, blocking keeps working even when the home server is off. Its API lets the server change the rules on a schedule. |
| **NextDNS device profiles** | NextDNS's per-OS setup: a configuration profile on iOS and macOS, an app or encrypted-DNS setting on Windows, Private DNS on Android. | They send **every** DNS lookup on the device to your NextDNS profile over encrypted DNS. That is what makes the rules apply on cellular and on any Wi‑Fi, not only at home. |
| **[Tailscale](https://tailscale.com)** | A mesh VPN built on WireGuard. Your devices join a private network (a "tailnet") and reach each other at stable `100.x.y.z` addresses, with no port forwarding. Free for personal use. | **It's the only way into the control page from your phone.** The server listens on its tailnet address and nowhere public, so nothing is exposed to the internet. A second, optional use: setting NextDNS as the tailnet's DNS server, so any device with Tailscale on is filtered even before its own profile is installed. |
| **Python 3 (standard library only)** | `http.server`, `urllib`, `sqlite3`, `zoneinfo`, `unittest`. No `pip install` (the optional `qrcode` package only prints the phone login link as a QR code). | Nothing to update, no supply chain to audit, and it runs on any Linux box. The whole program is under 4,000 lines you can read in an afternoon. |
| **systemd user units and timers** | The Linux service manager, run per user (`systemctl --user`), with no root needed. | They keep the page running and run the jobs: make NextDNS match local state every 5 minutes, flip the rules at every window edge, pull usage, and write a weekly report. Timers catch up after a reboot. `systemd-run` schedules the end of each pass. |
| **An always-on machine** | Any Linux host you leave on: a mini PC, a Raspberry Pi, a home server, or WSL2 on a Windows PC that stays awake. | It runs the page, the CLI and the timers. If it's off, NextDNS still enforces the coarse daily schedule and the always-blocked categories (see [How it works](docs/how-it-works.md#when-the-server-is-down)). |

## How the pieces fit

```
 phone / laptop / desktop                      always-on server (Linux, systemd --user)
 ┌────────────────────────┐   every DNS lookup  ┌──────────────┐  API (X-Api-Key)  ┌──────────────────────┐
 │ NextDNS device profile │ ──────────────────▶ │   NextDNS    │ ◀──────────────── │ focus                │
 │ (encrypted DNS)        │ ◀── 0.0.0.0 if ──── │   profile    │ ────────────────▶ │  reconcile (5 min)   │
 └────────────────────────┘     blocked         └──────────────┘   logs (1 h)      │  flip timer (edges)  │
            │                                                                       │  usage pull (5 min)  │
            │  Tailscale (100.64.0.0/10 only)                                       │  page :4861 + CLI    │
            └────────────────────────────────────────────────────────────────────▶ └──────────────────────┘
                     control page: pass, block now, lists, hours (device cookie)
```

The server keeps the **desired state** in a local JSON file: lists, hours, passes, and queued changes. Every 5
minutes, and at every window edge, `reconcile` works out what NextDNS should look like at that moment, applies the
difference, then reads the profile back and fails loudly if anything doesn't match. NextDNS is never the source of
truth, only the enforcer.

## Quick start

Full steps, with checks after each one, are in **[docs/setup.md](docs/setup.md)**. In short:

1. Create a NextDNS profile and an API key. Turn on *Block Bypass Methods* under Parental Control.
2. Install Tailscale on the server and on your phone.
3. On the server:
   ```bash
   git clone <this repo> ~/focus-dns && cd ~/focus-dns
   python3 -m unittest                          # 128 tests, no network
   mkdir -p ~/.config/focus
   cp config/nextdns.env.example ~/.config/focus/nextdns.env && chmod 600 ~/.config/focus/nextdns.env
   $EDITOR ~/.config/focus/nextdns.env          # profile id + API key
   bin/install-unit                             # units, timers, phone token, ~/.local/bin/focus
   focus doctor                                 # checks bind, page, API, resolver answers, Tailscale DNS
   ```
4. Install the NextDNS profile on each device. Then run `focus link` and open the link once on your phone
   (with Tailscale on) to log it in. Add the page to your home screen.

## Using it

```
focus status                  where things stand, and whether NextDNS matches
focus pass [MIN]              open every list for a few minutes (default 15, 3 a day)
focus block [MIN|HH:MM]       block every list now (90 min), or until a time today; never shortens a block
focus list / list add NAME    the lists; add one
focus add DOMAIN --list NAME  block a site (applies at once)
focus remove DOMAIN           during its list's block: queued for the unlock
focus hours LIST DAY RULE     e.g. `focus hours video mon 07:00-09:00,17:00-24:00`, `focus hours social sun blocked`
focus category porn on|off    the always-blocked categories (off is queued during a block)
focus undo [ID]               see or cancel queued changes
focus usage [last]            estimated time per site and device, this week or last
focus doctor                  diagnose everything
```

The page offers the same actions through the same code path (`focus/actions.py`), so the rules can't differ
between phone and terminal.

## Documentation

| Doc | For |
|---|---|
| [docs/setup.md](docs/setup.md) | Installing from zero: NextDNS, Tailscale, the server, each device |
| [docs/how-it-works.md](docs/how-it-works.md) | The model: lists, windows, lock rules, passes, reconcile, timers, usage, failure modes |
| [docs/api-notes.md](docs/api-notes.md) | NextDNS API behaviour verified against a live profile, including traps that wipe settings |
| [docs/security.md](docs/security.md) | What the server exposes, how secrets are kept, what stays private |
| [docs/build-your-own.md](docs/build-your-own.md) | Forking and changing it, with or without an AI coding assistant; ideas for variants |
| [CLAUDE.md](CLAUDE.md) | Rules for an AI coding agent working in this repo (the invariants that must not break) |

## Staying on the free NextDNS plan

NextDNS's free plan covers 300,000 lookups a month, and it fills faster than you'd guess. A phone, a laptop and
a desktop can each make 10,000 or more lookups a day, and most of them aren't you browsing. They're background
chatter: OS updates and telemetry, browser services, password-manager sync, game launchers, chat apps, and
certificate checks. Blocked trackers count too, and apps often retry them over and over. Past the cap, NextDNS
keeps answering lookups but **quits filtering for the rest of the month**, without telling you, so every block
opens up.

If your devices resolve through Tailscale (step 3 of [setup](docs/setup.md): NextDNS as the tailnet's global
nameserver with *Override DNS servers* on), you can send known-harmless domains to a plain public resolver with
Tailscale's **split DNS**. Everything else still goes to NextDNS. Those lookups then don't count against the
quota, and the blocking is unchanged. The other option is NextDNS Pro (about $20 a year, unlimited lookups),
which is simpler if the numbers don't come down far enough.

**1. Find the noise.** In NextDNS, open *Analytics → Domains* and filter by device, or read the logs for an
hour or a day. Look for the registrable domains (`example.com`, not `cdn3.eu.example.com`) that make the most
lookups and that you'd never want blocked.

**2. Choose carefully.** Anything routed around NextDNS is no longer filtered or logged there, and ad and
tracker blocking for it goes away too. A domain is safe to route only if:

- it doesn't serve anything you block. Watch for apps that live under a big provider's shared domain: a blocked
  site's mobile app often talks to an API host under its parent company's general API domain. Route only what
  you've checked;
- it isn't a general CDN or cloud host (the shared networks that serve millions of sites). Those can serve
  anything, including the categories you block;
- it doesn't contain a bypass. Some large domains include a public DNS-over-HTTPS server or a privacy relay
  that NextDNS's *Block Bypass Methods* blocks today. Check the likely names with the resolver test below. If
  one is blocked, either leave the whole domain on NextDNS, or add a second, more specific route that sends
  just that name back to NextDNS. Tailscale uses the longest matching route, so the specific one wins.

**3. Add the routes.** In the Tailscale admin console, go to **DNS → Nameservers → Add nameserver → Custom**,
enter a plain resolver (for example `1.1.1.1`), turn on **Restrict to domain**, and enter the domain. Repeat
for each domain. For a carve-out, add a route for that exact name that points at your NextDNS address (the
one shown under *Global nameservers*). With many domains, one call to the Tailscale API is quicker:

```
PATCH https://api.tailscale.com/api/v2/tailnet/-/dns/split-dns
{"example.com": ["1.1.1.1", "1.0.0.1"], "doh.example.com": ["<your NextDNS address>"]}
```

Use an access token with a short expiry, and revoke it when you're done. Setting a domain to `null` in that call deletes its route.
*Block Bypass Methods* blocks `api.tailscale.com` itself. The admin console still works, or you can allowlist
that one name in NextDNS while you make the change and take it off the allowlist afterwards.

**4. Check it.** Blocked sites must still answer `0.0.0.0`. `focus doctor` tests every target. You can also test
any name against the profile directly:

```
curl -s -H 'accept: application/dns-json' 'https://dns.nextdns.io/<profile-id>?name=<name>&type=A'
```

On a device with Tailscale on, the routed names should resolve normally, and they should drop out of NextDNS's
logs within a few minutes. Compare the next full day's count with the day before.

What it doesn't cover: a device with Tailscale off sends everything to NextDNS through its own profile, so those
lookups still count, and nothing is less safe. A server or build machine that doesn't need filtering at all can
use a plain resolver for everything.

## Limits, honestly

DNS blocking is a speed bump, not a wall. Someone determined can get around it with a VPN, another device, a
hotspot or by removing the profile. NextDNS's *Block Bypass Methods* closes the easy holes (public DoH resolvers,
VPN and proxy domains). On iOS, Screen Time's *Content & Privacy Restrictions* can make a profile harder to remove
casually. The aim is to make the impulsive visit fail, and to make undoing the rules slow and deliberate.

DNS caching means a change can take a few minutes to bite, and a video that's already playing keeps playing.

## Layout

```
focus/        the program (CLI, server, page, reconcile, schedule, lock rules, NextDNS client, usage)
tests/        unittest suite against a fake NextDNS (tests/fake.py) seeded from a real profile's shape
systemd/      user units and timers (@REPO@ is filled in by bin/install-unit)
bin/          focus (CLI wrapper), install-unit
config/       nextdns.env.example
docs/         setup, how it works, API notes, security, build your own
```

## License

MIT. See [LICENSE](LICENSE).
