# focus-dns

A self-hosted alternative to website blockers like [Freedom](https://freedom.to), built on NextDNS and Tailscale.

Distracting sites sit in named **lists** (for example *Video*: YouTube; *Social*: Twitter and X). Each list has its
own weekly hours, and outside those hours the sites are blocked on every device you own, at the DNS level. A few
categories (porn, gambling) are blocked at all hours. A small web page and a CLI, running on one always-on machine
at home, let you take a short **pass**, **block now**, and edit the lists and hours.

The design rule is that **friction is the product**. There is no off switch. A pass (15 minutes, 3 a day by default)
is the only way to open things early. Any edit that would loosen the rules during a block is queued until that
list's unlock rather than applied, so you can't buy your way out in a weak moment.

> Status: personal software that runs every day for one household. It is shared so others can fork it and build
> their own version, not as a polished product. Python 3 standard library only, 128 tests, no network in tests.

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
