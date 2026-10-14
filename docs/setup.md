# Setup

From nothing to blocked, about an hour. Each step ends with a check, so problems show up where they start.

You need: a NextDNS account (free tier is fine), a Tailscale account (free personal plan), an always-on Linux
machine with Python 3.10+ and systemd (WSL2 works if `systemd=true` is set in `/etc/wsl.conf` and the PC doesn't
sleep), and the devices you want filtered.

## 1. NextDNS profile

1. Sign up at <https://my.nextdns.io> and create a profile. Its id is the six characters in the URL
   (`https://my.nextdns.io/<id>/setup`).
2. **Parental Control:**
   - turn on **Block Bypass Methods** (blocks public DoH resolvers, VPN and proxy domains);
   - add the services you want scheduled (for example YouTube, Twitter). Leave them on; focus manages them;
   - add the categories you want blocked at all hours (for example Porn, Gambling).
3. **Settings → Logs:** keep logs on, with **1 hour** retention, and pick a storage location. focus pulls the
   log every 5 minutes and keeps only a summary locally (see [how-it-works.md](how-it-works.md#usage-kept-locally)).
   Turn logs off if you don't want usage numbers; blocking doesn't need them.
4. **Account → API:** create an API key.

Check: `curl -s -H "X-Api-Key: $KEY" https://api.nextdns.io/profiles | head` lists your profile. (If you get a 403
from Python but not curl, it's the User-Agent; `focus/api.py` already sets one.)

## 2. Tailscale

1. Install Tailscale on the server and on your phone, and log both into the same tailnet.
2. On the server: `tailscale ip -4` prints its `100.x.y.z` address.
3. Optional, recommended: in the admin console, **DNS → Global nameservers → Add → Custom**, enter
   `https://dns.nextdns.io/<profile-id>`, and turn on **Override DNS servers**. Every device then resolves through
   NextDNS whenever Tailscale is on.

Check: from the phone, the Tailscale app shows the server as connected.

## 3. The server

```bash
git clone <this repo> ~/focus-dns && cd ~/focus-dns
python3 -m unittest                                   # 128 tests, no network
mkdir -p ~/.config/focus
cp config/nextdns.env.example ~/.config/focus/nextdns.env
chmod 600 ~/.config/focus/nextdns.env
$EDITOR ~/.config/focus/nextdns.env                   # NEXTDNS_PROFILE, NEXTDNS_API_KEY
```

Before installing, decide your starting lists. The defaults (`focus/state.py`) are **Video** (YouTube service) and
**Social** (Twitter service + `x.com`), open Mon–Fri 17:00–midnight and all weekend, timezone `America/New_York`.
You can change all of it later with the CLI, but set the timezone first:

```bash
bin/install-unit          # units + timers, phone token, links ~/.local/bin/focus, fills FOCUS_TAILNET_IP
focus hours tz Europe/London    # your zone (applies at once outside a block)
focus reconcile --dry-run       # what it would change at NextDNS
focus reconcile
focus doctor                    # exit 0 = bind, page, API, resolver answers and timers all good
```

If `focus` isn't found, add `~/.local/bin` to `PATH`. On WSL2, for the page to be reachable from Windows browsers,
localhost forwarding (on by default) is enough.

Check: `focus status` says "NextDNS: in sync", and `focus doctor` shows `0.0.0.0` for each blocked target during
its block and a real address for `example.com`.

## 4. Each device

Install the NextDNS profile for your profile id (my.nextdns.io → Setup shows each method):

| Device | How |
|---|---|
| iPhone / iPad | Download the configuration profile from <https://apple.nextdns.io>, then Settings → General → VPN & Device Management → install. Optional: Screen Time → Content & Privacy Restrictions to make removal harder. |
| Mac | The same configuration profile, or the NextDNS app. See the Tailscale note in [how-it-works.md](how-it-works.md#tailscales-two-jobs). |
| Windows | The NextDNS app, or Settings → Network → DNS over HTTPS with your profile's DoH template. |
| Android | Settings → Network → Private DNS → `<profile-id>.dns.nextdns.io`. |
| Browsers | Turn off the browser's own "secure DNS" if it's set to a different provider, or it bypasses the OS setting. |

Name each device in the profile (the setup page shows how), so usage shows "phone" rather than "unnamed". Machines
that aren't people (the server, a dev box) go in `FOCUS_EXCLUDE_DEVICES`.

Check: during a block, open a blocked site on each device. It should fail to load within a few minutes (DNS
caches). `focus status` should show lookups arriving.

## 5. Phone page

```bash
focus link                 # prints the one-time login link (and a QR code if `pip install --user qrcode`)
```

Open it on the phone with Tailscale on. It sets a device cookie and redirects to the page. Then Share → Add to
Home Screen.

Check: the page loads from the home-screen icon. With Tailscale off it doesn't load, and that's expected.

## Upgrading

`git pull && python3 -m unittest && bin/install-unit`. The installer is safe to re-run; it keeps the token and
state. State files from older versions are migrated on read.
