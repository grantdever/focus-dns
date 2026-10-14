# How it works

This is the model behind focus: what is stored, what NextDNS is told, when, and what happens when something
fails. If you are changing the program, read this and [api-notes.md](api-notes.md) first.

## Desired state and reconcile

All the rules live in one JSON file on the server (`~/.local/state/focus/state.json`, written under a lock by
`focus/state.py`). It holds the lists, their weekly hours, the always-blocked categories, pass limits, passes
taken, any Block now, and queued (pending) changes.

NextDNS is the **enforcer, never the source of truth.** `focus/reconcile.py` does three things:

1. **Desired:** from the state and the current time, work out what the profile should look like right now:
   which services are active and in Recreation Time, which denylist domains are active, the Recreation Time
   schedule, the categories.
2. **Plan and apply:** read the live profile, diff, and send only the changes.
3. **Verify:** read the profile back and fail loudly if it differs. This matters because some NextDNS writes
   quietly reset fields (see [api-notes.md](api-notes.md)).

Reconcile runs every 5 minutes (`focus-reconcile.timer`), at every window edge (`focus-flip.timer`), and after
every edit. It is idempotent: running it twice changes nothing the second time.

## Two kinds of NextDNS rule

| Rule | What it covers | How time works |
|---|---|---|
| **Services** (`youtube`, `twitter`, …) | NextDNS's curated domain sets for a whole app | Can follow **Recreation Time**, NextDNS's own daily schedule, so they keep flipping even if the server is off |
| **Denylist domains** (`x.com`, anything you add) | One domain and its subdomains | Ignore Recreation Time. The server's timers switch them on and off at each window edge |

The `twitter` service doesn't cover `x.com`, which is why `x.com` sits on the denylist in the default
Social list. Check with `focus doctor` after adding a service.

## Timers

| Unit | When | Job |
|---|---|---|
| `focus.service` | always | the page on 127.0.0.1:4861 and the tailnet address |
| `focus-reconcile.timer` | every 5 min | make NextDNS match the state; closes a pass whose one-shot was lost |
| `focus-flip.timer` | every edge of every list's windows, and midnight | reconcile at the exact moment; its `OnCalendar=` lines are generated from the hours into a drop-in (`focus timers --write`) |
| `focus-usage.timer` | every 5 min | pull new lookups from NextDNS logs into `usage.sqlite` |
| `focus-report.timer` | Mondays 09:00 | weekly report saved under `reports/` in the state folder |

A pass schedules its own close with `systemd-run --user --on-active=…`. Timers use `Persistent=true` where it
matters, so they catch up after a reboot.

## When the server is down

NextDNS keeps enforcing on its own:

- services follow the coarse Recreation Time window (open from the earliest open to the latest close of their
  lists that day);
- the always-blocked categories stay blocked;
- denylist domains, and any gap inside the coarse window, keep their last state until the server is back.

So the failure is "a little stricter or a little looser at the edges for a while", never "everything opens".
The page and passes are unavailable while the server is off, which errs on the side of blocked.

## Lists

Every blocked site is in exactly one list, and each list has its own week. The seed (edit it in `focus/state.py`) is
**Video** = YouTube (service), **Social** = Twitter (service) + x.com (denylist), both open
Mon–Fri 17:00–midnight and all weekend. A domain already on one list cannot be added to another; take
it off the first (a loosening edit, so it waits for that list's unlock). Block now and Block until
cover every list; a pass opens every list at once.

## Always blocked

Parental Control categories `porn` and `gambling` are on the profile with no recreation: blocked at all
hours, and a pass never opens them. `reconcile` asserts them like the services (`active`, and
`recreation: false`). Turning one off is a loosening edit like any other (queued while any list is
blocked, at once otherwise); turning it on applies at once. The page lists them under "Always blocked"
with an Off control.

## Passes

15 minutes, 3 a day; the count resets at midnight. A pass is refused when nothing is blocked,
while another pass is running, or when the day's passes are used. Taking one opens every list
at NextDNS at once (never the always-blocked categories) and schedules the close with `systemd-run --user --on-active=…`; the 5-minute
reconcile closes it too if that one-shot is lost (a reboot mid-pass just ends the pass). If NextDNS
does not answer, the pass is not spent. DNS caching means a block or pass can take a few minutes
to bite, and a video that is already playing keeps playing.

## Lock rules (CLI and page alike)

The rules are per list. While a list's block is in force (its hours, Block now or Block until):

- **Apply at once (tightening):** add a domain, a shorter or later window, removing a window, a blocked
  day, fewer passes, shorter passes, Block now, Block until, turning a category on.
- **Queued for the unlock (loosening):** remove a domain, adding a window, a wider or earlier window,
  opening a blocked day. These wait for **that list's** unlock (its next open after any Block now).
  More passes, longer passes, a new timezone and turning a category off are not one list's, so they wait
  until **no list** is blocked. The change is saved as pending with the time it applies and shown in
  place: "Removes at 17:00 · Undo" on the page, "Queued for 17:00:" in `focus status`. Undo cancels it.
  `reconcile` applies each one at or after its unlock and never before, so if a tightening edit or
  Block now pushes the unlock later, the queued change waits for the later time.
- A window shifted both ways (say 17:00–24:00 to 16:00–22:00) applies its overlap at once (17:00–22:00)
  and queues the rest.
- A list with no sites has nothing to loosen: its hours apply at once.
- One queued change per field: a later edit to the same field replaces it (a tighter one applies at once
  and drops it). Re-adding a domain cancels its queued removal.

Queued changes live in the state file, so they survive restarts. A running pass does not lift the lock.
Outside a block every edit applies at once.

## Hours and windows

Per list and weekday, one of: **Blocked all day**, **Open all day**, or **Open during** one or more
windows (`HH:MM-HH:MM`, up to six a day; on the page a "+ Window" button adds one and Remove takes one
away). The end defaults to midnight; a blank end, 00:00, 23:59 and 24:00 all mean midnight. Windows do
not cross midnight; overlapping or touching windows merge. Times must be `HH:MM`; anything else is
refused before it reaches NextDNS, because a malformed write there wipes the whole schedule.

NextDNS Recreation Time holds one window a day for the whole profile. focus writes the **coarse span**
there: from the earliest open to the latest close across all lists that use services (a blocked day is
left out; midnight is stored as `23:59`, the way the profile already had it). That is the floor that
holds if the server is down. The per-service `recreation` flag stays true, except inside a gap: while a
service's own list is blocked but the coarse window is open, `reconcile` sets that service's
`recreation` to false, and back at the list's next open. Denylist domains follow their list's windows
directly. The flip timer gets every edge of every list's windows. After writing, `reconcile` re-reads
the schedule and fails loudly if it differs.


## Usage, kept locally

Set NextDNS to keep logs for **1 hour** (`focus usage retention` shows or sets it); the server keeps its own summary. Every 5 minutes
`focus-usage.timer` pulls the new lookups (the raw log, so the counts match NextDNS's) and folds them into
`~/.local/state/focus/usage.sqlite` (mode 600). The lookups themselves are never written down. What is kept
(`focus/usage.py` has the details):

- **sessions**: device, site and first/last lookup. Sites are registrable domains with CDNs folded in (twimg → x.com,
  googlevideo → youtube.com). A 5-minute gap ends a session; a session counts once it has 3 lookups, from first to last
  lookup plus 1 minute. OS, CDN and analytics hosts make no sessions, nor do devices listed in `FOCUS_EXCLUDE_DEVICES`. Kept 14 days.
- **daily counts** per device, and **blocked tries** per site and rule; porn and gambling hits are stored by category
  name only, ad and tracker blocklist hits as "(background)". Kept 35 days.
- **gaps**: when the server was off for over an hour, those lookups are gone; the report says how many hours.

No subdomains, IPs or URLs; deleted rows are overwritten (`secure_delete`). Weekly reports are kept 8 weeks.

DNS gives the moment of a lookup, never how long a page stayed open, so minutes are estimates, right to within a
few minutes a visit. `focus usage` shows this week, `focus usage last` last week; Monday's saved report adds last
week's. The page's Insights now come from this file (no NextDNS analytics calls), with "Time online this week" and
top sites; the monthly count toward the cap is the larger of NextDNS's and the local one.


## Tailscale's two jobs

1. **Carrying the page.** focus binds exactly two addresses: 127.0.0.1 and this machine's tailnet address
   (`FOCUS_TAILNET_IP`). The tailnet listener accepts only 100.64.0.0/10 peers, only a `Host` header naming
   that IP and port, and only with the device cookie. Use the IP, not the MagicDNS name, because the page
   checks `Host` against the IP.
2. **Optional: NextDNS as the tailnet's DNS.** In the Tailscale admin console (DNS → Global nameservers), add
   `https://dns.nextdns.io/<profile-id>` and turn on *Override DNS servers*. Every device with Tailscale on then
   resolves through your profile, even before its own NextDNS profile is installed. `focus doctor` reports
   whether this is set and whether this node accepts tailnet DNS.

Gotchas:

- **macOS with Tailscale on and no tailnet nameserver** can resolve through Tailscale and skip the NextDNS
  profile (tailscale/tailscale#15512). Either set NextDNS as the tailnet nameserver (job 2), or on that Mac run
  `tailscale set --accept-dns=false`.
- **Tailscale off on the phone** means no page and no pass, and everything stays blocked. The device's NextDNS
  profile works on its own.
- On a dev box, `tailscale set --accept-dns=false` keeps build and agent traffic off the NextDNS lookup count.

## The page

One screen, phone width first, plain HTML forms. The only script is a light/dark toggle, pinned in the CSP by
its hash. It shows:

- the current state and the next change in words ("Blocked until 20:00 (today).");
- **Pass**, **Block now** (90 min) and **Block until [time]**;
- one block per list: its sites (add, remove, or "Removes at 17:00 · Undo" during a block) and its hours;
- **Always blocked**, with Off (queued during a block);
- passes per day, pass length and passes used this week;
- **Insights**: lookups this month against the 300,000 free-tier cap, lookups per day for 30 days (one inline
  SVG bar chart with a table view), blocked share, top blocked domains, and estimated time online per device.

Phone setup, once: run `focus link` on the server, open that link on the phone with Tailscale on, then Add to
Home Screen. The link sets a cookie for that device; it gives no extra powers. `focus link --send` Taildrops a
one-link page to the tailnet's iOS device.

## Code map

```
focus/config.py     paths, port, settings (FOCUS_*)      focus/schedule.py  windows, coarse span, HH:MM validation
focus/state.py      state file + lock, migrations        focus/core.py      lists, lock rules, passes, Block now
focus/api.py        NextDNS client (urllib)              focus/reconcile.py desired state, plan, apply, verify
focus/actions.py    the one edit path (CLI + page)       focus/timers.py    flip drop-in, pass one-shots
focus/server.py     two listeners + request checks       focus/page.py      the page (HTML, CSS, chart)
focus/usage.py      log pull → sessions + counts         focus/report.py    weekly numbers
focus/insights.py   Insights data + 5-min cache          focus/cli.py       the CLI and doctor
```

Tests (`python3 -m unittest`) run against `tests/fake.py`, an in-memory NextDNS that copies the real API's
quirks, including the ones that wipe data, so a regression shows up as a failing test rather than a broken
profile.
