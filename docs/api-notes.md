# NextDNS API notes

Verified in October 2026 against a live profile. NextDNS's public API docs are thin, so these notes record
what the API actually did, including the calls that silently wipe settings. Read this before changing
`focus/api.py` or `focus/reconcile.py`.

Base `https://api.nextdns.io`, header `X-Api-Key`. Writes return 204 with an empty body; errors are
`{"errors":[{"code":...}]}`. All of this was exercised for real.

| Need | Call | Result |
|---|---|---|
| List profiles | `GET /profiles` | works |
| Services | `POST /profiles/{id}/parentalControl/services` `{"id":"youtube","active":true}` | ids `youtube`, `twitter` valid; `x` invalid |
| Toggle a service | `PATCH .../parentalControl/services/{sid}` `{"active":false}` | 204 |
| Per-service recreation flag | `PATCH .../parentalControl/services/{sid}` `{"recreation":true}` | 204. true = allowed during the recreation window |
| Recreation schedule | `PATCH /profiles/{id}/parentalControl` `{"recreation":{"timezone":"America/New_York","times":{"monday":{"start":"17:00","end":"23:59"},...}}}` | 204; `HH:MM` or `HH:MM:SS`. Integers or arrays give a 500 **and wipe `times`**. `/parentalControl/recreation` is 404 |
| Block bypass | `PATCH .../parentalControl` `{"blockBypass":true}` | 204 |
| Denylist | `POST .../denylist` `{"id":"x.com","active":true}`; `PATCH .../denylist/{domain}` `{"active":false}`; `DELETE .../denylist/{domain}` | all 204 |
| Query count | `GET .../analytics/status?from=-30d` | returns `data: []` on an empty profile; shape unverified until traffic exists |
| Resolver check | `GET https://dns.nextdns.io/{id}?name=x.com&type=A` with `accept: application/dns-json` | blocked = `0.0.0.0`. Propagation under a minute |

## Findings that change the design

1. **The `twitter` service does not cover x.com.** `twitter.com` was 0.0.0.0, `x.com` resolved normally. Fix applied: `x.com` on the denylist. Denylist entries ignore Recreation Time, so x.com needs the server timers (17:00 off, 00:00 on, weekdays) to follow the schedule. Same for every domain added from the page.
2. **Recreation Time is one schedule per profile**, with a per-service `recreation` boolean opting a service into it. Writing the schedule once from the API is fine, but a service PATCH after a bad schedule write can lose flags: `reconcile` must assert both the schedule and every service's `recreation: true`, not just `active`.
3. A malformed schedule write returns 500 and clears the schedule. `reconcile` must validate times as `HH:MM` strings before writing and re-read afterwards.

## Reference profile

The shape `tests/fixtures/profile.json` copies: services youtube + twitter, both `active: true, recreation: true`;
`blockBypass: true`; Recreation Time Mon–Fri 17:00–23:59 and Sat–Sun 00:00–23:59; `x.com` on the denylist;
categories porn + gambling `active: true, recreation: false`. Logs on, with 1-hour retention (see Logs below).

## Found while building

- **User-Agent required.** `api.nextdns.io` (Cloudflare) answers 403 to Python urllib's default
  User-Agent even with a valid key. `focus/api.py` sends `User-Agent: focus/1`.
- **A blocked day is a day left out of `times`.** PATCHing `recreation.times` without `sunday` returned 204;
  the read-back had six days, and service flags were untouched. The original seven-day schedule was then
  written back and re-read (identical). focus maps "blocked all day" to omission.
- **Analytics shape now known.** `GET .../analytics/status?from=2026-10-01T00:00:00-04:00` returns
  `{"data":[{"status":"blocked","queries":7},{"status":"default","queries":1}]}`; ISO dates with an offset
  and plain `YYYY-MM-01` both work. focus sums `queries` over all statuses from the first of the month.
- Reads return times as `HH:MM:SS`; focus compares on `HH:MM`.
- The DoH JSON answer for a blocked name carries `EDE: 17 (Filtered): (Blocked by NextDNS: service:youtube)`
  in `Additional`, with `TTL 300` on the `0.0.0.0` answer.
- **A service PATCH resets the fields it leaves out.** `PATCH .../parentalControl/services/youtube` with
  only `{"active":false}` returned 204 and left `recreation: false`. focus always sends `{"active":…, "recreation":…}` together.
- dns.nextdns.io once answered a real address for `youtube.com` and once for `www.youtube.com`
  while the service was blocked; every other lookup that hour was `0.0.0.0`. Possibly an edge
  still holding a pre-setup cache. `focus doctor` shows one lookup per target, so a single "differs" can be noise.

## Found later: caching and two-ended hours

- **Cache boost.** With it on, `settings.performance` reads
  `{"ecs": true, "cacheBoost": true, "cnameFlattening": false}`. focus neither reads
  nor writes it; `reconcile` leaves `settings` alone. Cache boost raises the TTL that devices cache NextDNS
  answers for, so a pass or Block now can take longer to bite on a device that already holds an answer.
- **Two-ended hours map onto the same object.** focus now writes each day's open window as
  `{"start": "HH:MM", "end": "HH:MM"}` with any end, not only `23:59`. "To midnight" is still written as
  `23:59` (the form the profile already had), so the current profile reads back unchanged and the first
  reconcile after the change was "Nothing to change." An end other than `23:59` has not yet been written
  to the live profile; the fake API and tests cover it, and `reconcile` re-reads after every schedule write.

## Found later: lists, windows, categories, analytics

- **Categories.** `GET /profiles/{id}/parentalControl/categories` returns
  `{"data":[{"id":"porn","recreation":false,"active":true},{"id":"gambling","recreation":false,"active":true}]}`
  (also inside `GET /profiles/{id}` under `parentalControl.categories`).
  `recreation: false` means not allowed during Recreation Time, so they are blocked at all hours.
  `PATCH .../parentalControl/categories/porn` with `{"active":true,"recreation":false}` (the values it already
  had) returned 204 and read back unchanged. Whether a category PATCH resets the fields it leaves
  out, like a service PATCH does, was not tried on the live profile; focus always sends both fields.
  `POST .../parentalControl/categories` `{"id", "active", "recreation"}` is what reconcile uses if one goes
  missing (follows the services endpoint; not exercised live).
- **One Recreation Time window per day, for the whole profile.** focus writes the coarse span across the
  lists that use services and enforces the gaps inside it by flipping each service's `recreation` flag
  (false while its list is blocked inside the span). Recreation Time does not accept several windows a day
  (the times object is one `{start, end}` per day; arrays give a 500 and wipe it, see above).
- **Analytics shapes** (read live; the profile had 49–55 lookups, all today, one unnamed device):
  - `GET .../analytics/status?from=-30d` → `{"data":[{"status":"blocked","queries":33},{"status":"default","queries":16}],"meta":{"pagination":{"cursor":null}}}`.
  - `GET .../analytics/domains?from=-30d&status=blocked&limit=5` → `{"data":[{"domain":"youtube.com","tracker":"youtube","queries":12}, …,{"domain":"x.com","queries":6}],"meta":{"pagination":{"cursor":"6m"}}}` (`tracker` only on some rows).
  - `GET .../analytics/reasons?from=-30d` → `{"data":[{"id":"service:youtube","name":"YouTube","queries":19},{"id":"service:twitter","name":"X","queries":14},{"id":"denylist","name":"Denylist","queries":7}]}`.
    The `twitter` service is named "X" there.
  - `GET .../analytics/devices?from=-30d` → `{"data":[{"id":"__UNIDENTIFIED__","queries":49}]}`. Named devices
    should carry a `name`; focus shows `name`, else "unnamed" for `__UNIDENTIFIED__`, else the id.
  - **Time series:** `GET .../analytics/status;series?from=…&to=…&interval=86400` →
    `{"data":[{"status":"blocked","queries":[0,…,33]},…],"meta":{"series":{"times":["2026-09-14T04:00:00.000Z",…],"interval":86400}}}`.
    `interval=1d` is a 400 (`type`); it takes seconds. With only `from=-30d` the buckets are aligned to the
    current time of day (`…T20:13:04Z` each), not to midnight; with `from` and `to` both on local midnights
    (`2026-09-14T00:00:00-04:00` to `2026-10-14T00:00:00-04:00`) they are 30 local days. `alignment=start`
    returned an empty `data`, and `alignment=clock` is a 400 (`enum`). focus uses `from`/`to` on midnights.

## Logs (verified 2026-10-14)

- `GET /logs?from=ISO&sort=asc&limit=10..1000&cursor=…` pages oldest first; `meta.pagination.cursor` is null on the last page.
  `limit` below 10 is refused.
- **Without `raw=1` the log hides repeated lookups** (212 entries vs 462 counted by analytics over the same span). With
  `raw=1` the counts match `analytics/status`.
- Each entry: `timestamp`, `domain`, `root` (registrable domain), `device {id, name, model, localIp}`, `status`,
  `reasons [{id, name}]` (ids like `service:youtube`, `denylist`, `blocklist:nextdns-recommended`), `clientIp`,
  `client` ("tailscale" for tailnet devices), and sometimes `tracker`. **`tracker` is a company label** (YouTube's own
  lookups carry `"tracker": "youtube"`), not a sign of background traffic.
- `GET/PATCH /settings/logs` `{enabled, drop {ip, domain}, retention (seconds), location}`. Changing `location` from
  `us` to `ch` wiped the existing logs and analytics.
