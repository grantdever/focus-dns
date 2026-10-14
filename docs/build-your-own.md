# Build your own

focus is small on purpose, and it's meant to be forked. This page covers how to adapt it, how to work on it with
an AI coding assistant, and which parts not to break.

## Start by deciding your rules

Write these down before touching code. They decide most of the design:

1. **What's blocked, and when?** Group sites into lists by the urge they feed (video, social, news, shopping), not
   by company. Give each list its own hours.
2. **What's the valve?** Here it's a 15-minute pass, 3 a day. Yours might be one a day, a pass that costs a typed
   sentence, a pass that needs a second person's approval, or no valve at all.
3. **How hard should loosening be?** Here a loosening edit during a block is queued until that list's unlock.
   Stricter: queue it for 24 hours. Looser: apply at once and just log it.
4. **Where's the control surface?** A page over Tailscale, a CLI, a Telegram bot, a Shortcut, an e-ink display.

## Common changes, and where they go

| Change | Where |
|---|---|
| Different default lists, hours, pass limits | `focus/state.py` (the seed), then `focus list`, `focus hours`, `focus limits` |
| A new NextDNS service (e.g. `reddit`, `tiktok`, `instagram`) | `focus/state.py` seed and `SERVICE_NAMES` in `focus/page.py`. Check with `focus doctor` that the service covers every domain the app uses (`twitter` doesn't cover `x.com`) |
| Another always-blocked category | `focus category <id> on`; add a probe domain to `CATEGORY_PROBES` in `focus/cli.py` so `doctor` checks it |
| How sites are folded for usage (CDNs, aliases, noise hosts) | `ALIASES` and the noise lists in `focus/usage.py` |
| Lock rules (what counts as tightening or loosening) | `focus/core.py`, with tests in `tests/test_rules.py` |
| Restyle the page | the `TOKENS` and `CSS` strings in `focus/page.py`. The CSP pins the one script by a hash computed from it (`SCRIPT_HASH`); a second script or an external load needs a CSP change in `focus/server.py` |
| A different port | `DEFAULT_PORT` in `focus/config.py`, the unit description, and the installer's message |
| Another control surface (bot, Shortcut, watch) | call `focus/actions.py`, never NextDNS directly, so the lock rules hold everywhere |

## Ideas for variants

- **Accountability:** email or message a friend whenever a pass is taken or a loosening edit is queued.
- **Cost per pass:** require typing a reason, logged in the weekly report.
- **Focus sessions:** a "deep work" button that blocks everything, including email, for 90 minutes.
- **Calendar-aware hours:** open lists only when the calendar is free.
- **Household:** one NextDNS profile per person, one state file per profile, shared server.
- **No server:** drop the timers and use only NextDNS Recreation Time. You lose denylist scheduling and passes,
  but there's nothing to keep running.

## Working with an AI coding assistant

This repo works well with an agent like Claude Code. Some practices that help:

1. **Have it read first.** Point it at `CLAUDE.md`, `docs/how-it-works.md` and `docs/api-notes.md` before any
   change. The API notes record calls that wipe your schedule. An agent that hasn't read them will re-discover
   them on your live profile.
2. **Describe the behaviour, not the code.** "During a block, adding a site applies at once but removing one waits
   for the unlock" is a better brief than "edit core.py".
3. **Tests before live.** Ask for tests against `tests/fake.py`, and run `python3 -m unittest`, before anything
   touches the real profile. If you learn a new NextDNS quirk, teach the fake that quirk too.
4. **Dry-run against the profile.** `focus reconcile --dry-run` shows what would change. Read it before the real
   run, and run `focus doctor` after.
5. **Keep secrets out of the conversation.** The agent never needs your API key in chat. It reads
   `~/.config/focus/nextdns.env` the way the program does.

A good first prompt for your fork:

> Read CLAUDE.md, README.md and docs/. Then propose, without writing code yet, how to add <your change>.
> Name the files you'd touch, the lock-rule cases it creates, and the tests you'd add.

## Invariants not to break

These are the properties that make it work. `CLAUDE.md` repeats them for agents.

- NextDNS is the enforcer, never the source of truth. Every change goes through state, then `reconcile`.
- `reconcile` re-reads after writing and fails loudly on any mismatch.
- Always send both `active` and `recreation` when PATCHing a service or category. Sending one resets the other.
- Validate times as `HH:MM` before writing Recreation Time. A malformed write wipes the whole schedule.
- One edit path (`focus/actions.py`) for every surface.
- The server binds loopback and its tailnet address, nothing else.
- Tightening applies at once. Loosening during a block is queued.
