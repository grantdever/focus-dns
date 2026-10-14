# Working in this repo (for AI coding agents)

focus-dns is a DNS-based site blocker: local desired state on an always-on Linux server, enforced by a NextDNS
profile, controlled from a page reachable only over Tailscale. Python 3 standard library only.

Read first: `README.md`, `docs/how-it-works.md`, `docs/api-notes.md` (NextDNS calls that wipe settings).

## Commands

```bash
python3 -m unittest                 # all tests, no network; must pass before and after every change
python3 -m focus reconcile --dry-run   # what would change at NextDNS (needs ~/.config/focus/nextdns.env)
python3 -m focus doctor             # end-to-end checks on a live install
```

## Invariants

- **State is the truth; NextDNS is the enforcer.** Edits change `state.json` through `focus/actions.py`, then
  `reconcile` applies them. Never call the NextDNS API from a new feature directly.
- **Re-read after every write** and fail on mismatch (`reconcile.apply`).
- **Service and category PATCHes send both `active` and `recreation`.** One alone resets the other.
- **Recreation Time times are `HH:MM` strings**, validated before writing; a bad write returns 500 and wipes the
  schedule. A blocked day is an omitted day. Midnight is written as `23:59`.
- **Lock rules:** tightening applies at once; loosening during a list's block is queued for that list's unlock;
  global loosening (more/longer passes, timezone, category off) waits until no list is blocked. Tests in
  `tests/test_rules.py` and `tests/test_lists.py` pin this.
- **Bind only 127.0.0.1 and `FOCUS_TAILNET_IP`.** Keep the Host, peer-range, cookie and Origin checks in
  `focus/server.py`.
- **The page's one script is pinned by hash** in the CSP (`SCRIPT_HASH` is computed from it); add no other scripts or external loads.
- **No new dependencies.** Standard library only (`qrcode` stays optional).
- **New NextDNS behaviour goes in three places:** `docs/api-notes.md`, `tests/fake.py` (so the fake has the quirk)
  and a test.

## Secrets and privacy

- Never print, log or commit the API key, the token, `state.json`, `usage.sqlite` or a real profile export.
- Test fixtures are trimmed shapes with placeholder names and ids (`abc123`, `100.64.0.1`, "phone", "laptop").
