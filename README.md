# Zelda Switch 2 Console + Controller Stock Monitor

Watches the **Nintendo Switch 2 — The Legend of Zelda 40th Anniversary
Edition console** ($519.99) and matching **Switch 2 Pro Controller** ($99.99),
both releasing 10/29/2026. An alert goes out the moment either becomes
buyable — in stock *or* a pre-order window reopening.

Target, GameStop and Nintendo run as independent 25-second workers for each
item. Walmart and Best Buy run as independent 60-second workers, so a retailer
bot wall cannot delay any other check. Walmart uses exact-item state from its
ID search and reports a blocked/error result if Walmart challenges the request;
it never opens a browser or trusts recommendation-rail metadata. Best Buy is
also HTTP-only. Costco remains disabled by default.

Console and controller channels each get their own quiet live-status message,
refreshed every minute with the age and latency of every check. Availability
creates a clearly labeled `@here` alert with a prominent product/cart link;
confirmation and screenshots happen afterward. Failed deliveries are persisted
in `notification_outbox.json` and retried.

## Install on an always-on Mac

An old Mac on your home internet is the preferred unattended host. It keeps the
residential network path used by the retailer checks and does not depend on your
Windows laptop being powered on.

Open Terminal on the Mac and run:

```bash
cd "$HOME"
git clone https://github.com/EviIius/zelda-stock-bot.git
cd zelda-stock-bot
./install_macos_service.sh
```

The installer finds Python 3.11+, creates an isolated `.venv`, installs
Playwright and Chromium into the repository, runs the project self-tests,
securely prompts for the console
and controller Discord webhooks, sends a test to each channel, and installs
`com.eviiius.zelda-stock-monitor` as a system `launchd` daemon. The daemon:

- starts automatically at boot and runs as the user who installed it;
- restarts automatically after a crash;
- prevents idle sleep while healthy, without permanently changing power settings;
- writes timestamped output to a 5 MB rotating log under `logs/` (five backups); and
- uses a process lock to prevent duplicate monitors.

The Mac must remain powered and connected to the internet. A MacBook must also
remain open; macOS still sleeps when its lid is closed. Clone directly under
your home folder as shown above—Documents, Desktop, and Downloads have macOS
privacy restrictions that can block unattended agents.

Useful commands after installation:

```bash
./macos_service.sh status
./macos_service.sh logs
./macos_service.sh restart
./macos_service.sh test
```

To stop or completely unregister the daemon:

```bash
./macos_service.sh stop
./macos_service.sh uninstall
```

Uninstalling preserves `.env`, logs, state, and the virtual environment. Never
copy `.env` into Git or send its contents to anyone; the installer creates it
with user-only permissions.

---

## What changed in this version

The first version had four problems that would have caused it to miss the
drop entirely. Worth understanding, because they shape how this one works:

1. **It could almost never report "in stock".** `check_stock()` scanned
   the whole raw HTML for `"out of stock"` and returned `False` on a hit —
   but every one of these product pages contains that phrase in
   recommendation rails, Q&A sections and "similar items" carousels, for
   *other* products, even when this item is buyable. The out-of-stock
   check ran first and short-circuited, so the answer was `False`
   essentially always. `test_detect.py` reproduces this.

2. **It treated a pre-order as bad news.** `"coming soon"` was in the
   out-of-stock list and `"pre-order"` wasn't in the in-stock list. Since
   this console is a pre-order item, the exact event you're waiting for
   would have been read as "still unavailable".

3. **It couldn't tell "blocked" from "sold out".** A bot-detection
   interstitial contains no buy language, so it scored as out of stock.
   The bot could have been fully blocked for weeks while looking healthy.

4. **`*/5 * * * *` is not every five minutes.** GitHub's scheduler is
   explicitly best-effort and runs late under load — 10–25 minutes is
   normal, and it's worst at the top of the hour when everyone's crons
   fire at once.

This version fixes all four: layered detection that prefers structured
data, pre-order treated as alertable, an explicit `blocked` state with
health alerting, and a long-running loop instead of relying on cron
frequency.

---

## How detection works

For each retailer it tries signals in order and stops at the first
trustworthy one:

| Layer | Signal | Reliability |
|---|---|---|
| 1 | Official retailer API (when configured) | Definitive |
| 2 | `schema.org` JSON-LD `offers.availability` | High |
| 3 | Embedded app state (`__NEXT_DATA__`) | Good |
| 4 | Rendered DOM via a persistent Playwright browser | Definitive |
| 5 | Scoped page-text phrases | Negative evidence only |

**Layer 5 can never report "in stock".** It may say "definitely not
buyable" or "I don't know", nothing more. This is the most important rule
in the codebase, and it was learned the hard way — measured on Target's
page for this product:

```
raw HTML:  "add to cart" x1,  "out of stock" x0,  0 JSON-LD blocks
rendered:  <button data-test="Preorder.Disabled" disabled>Preorder</button>
           "Pickup Not available / Shipping Not available / Coming October 29"
```

The HTML Python receives contains *no availability information at all*,
so phrase matching alerted "IN STOCK" on a sold-out pre-order. A false
negative costs one missed alert and trips the health warning. A false
positive teaches you to ignore the one notification that matters. Positive
claims therefore require structured evidence: an API, JSON-LD, app state,
or a real rendered DOM.

### Why Playwright matters here

Target is unreadable without it — you'll get `unknown` forever. Install it
for local runs:

```
pip install -r requirements-browser.txt
playwright install chromium
```

It renders the page like a real browser and reads whether the buy button
is actually enabled, which is the thing that flips on a restock. Detection
falls back to the HTTP layers automatically if it isn't installed.

Statuses: `in_stock` and `preorder` alert you. `out_of_stock` is quiet.
`blocked`, `error` and `unknown` mean the check learned nothing — they
never overwrite the last known-good status, and if one persists for 90
minutes you get a low-priority heads-up so a dead check can't stay
invisible.

---

## Setup — Discord alerts, step by step

### 1. Get a server you control

If you already have one, skip ahead. Otherwise, in the Discord desktop or
mobile app: **+** in the left server rail → **Create My Own** → **For me
and my friends** → name it anything. A server you're the only member of is
completely fine and is the usual setup for this.

### 2. Make a dedicated channel

Right-click the server name → **Create Channel** → Text → name it
`stock-alerts`. A dedicated channel matters, because in step 5 you'll set
this one channel to notify you for everything without turning your whole
server loud.

### 3. Create the webhook

A webhook is a URL that lets a script post into one specific channel. It
can only post messages — it can't read anything or touch the rest of your
account.

1. Right-click the `stock-alerts` channel → **Edit Channel**
2. **Integrations** → **Webhooks** → **New Webhook**
3. Optionally rename it "Zelda Bot"
4. **Copy Webhook URL**

It looks like `https://discord.com/api/webhooks/123.../abc...`.

For clean separation, repeat steps 2–3 with a `controller-alerts` channel and
save its URL as `CONTROLLER_DISCORD_WEBHOOK_URL`. If that second URL is ever
missing, controller alerts fall back to the main webhook rather than vanish.

**Treat that URL like a password** — anyone who has it can post into your
channel. Don't commit it, and don't paste it into a screenshot or a chat.
If it ever leaks, delete the webhook in Discord and create a new one;
that instantly invalidates the old URL. `.env`, runtime state, delivery queues,
heartbeats and logs are gitignored so they can't be committed by accident.

*Webhooks are a desktop/browser feature. On mobile, use the browser
version of Discord to do this part.*

### 4. Test it before you rely on anything else

In PowerShell, from the repo folder:

```powershell
copy .env.example .env
notepad .env          # paste your webhook after DISCORD_WEBHOOK_URL=
pip install -r requirements.txt
python stock_monitor.py --test-alert
```

You should get a Discord message within a second or two. Don't skip this —
the worst possible failure is a monitor that detects the restock perfectly
and has no working way to tell you.

### 5. Make it actually wake you up

This is the step people skip, and it's the one that decides whether you
hear about it during a workday.

**On your phone:**

1. Long-press the `stock-alerts` channel → **Notifications** → **All
   Messages**
2. Long-press the server → **Notification Settings** → make sure
   **Mute Server** is off
3. Phone settings → Discord → Notifications → allow, and set it to break
   through Focus / Do Not Disturb

The bot prefixes stock alerts with `@here`, which triggers a push even in
channels set to mentions-only — but "All Messages" plus an unmuted server
is the belt-and-braces version.

**Test it for real:** put your phone on your desk, lock it, and run
`--test-alert` again. If it doesn't light up the lock screen, fix that
now rather than finding out during the drop.

### 6. Run it

Locally — copy `.env.example` to `.env`, open `.env` in Notepad, and paste
your webhook after the `=`:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/123.../abc...
CONTROLLER_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/456.../def...
```

Then double-click `run_local.bat`. There is nothing to edit in the `.bat`
itself; it reads `.env`, sends a test alert, and refuses to start the loop
if that test fails.

Quotes around the value are optional and stray ones are stripped, as is
trailing whitespace and a Notepad byte-order mark. This indirection exists
on purpose: putting secrets directly in a `.bat` file is a trap, because
batch treats quotes you type as part of the value, and a paste landing on
the wrong side of an existing quote produces a mangled variable with a
confusing error. `.env` has none of those rules. It's also gitignored, so
your webhook can't be committed by accident.

On GitHub Actions — **Settings → Secrets and variables → Actions → New
repository secret**, add `DISCORD_WEBHOOK_URL` and
`CONTROLLER_DISCORD_WEBHOOK_URL`. Then
**Actions** tab → enable workflows → **Stock Monitor** → **Run workflow**.

Running both at once is supported, though the supervised local service below
is the primary monitor.

---

## Where to run it — read this before relying on it

**GitHub Actions runners are Azure datacenter IPs, and Target, Walmart and
Best Buy block those hard.** You will likely see `blocked` for those three
from Actions while the exact same code works fine from your house. Two
consequences:

**Run it locally too.** On Windows, use the supervised task below. On macOS,
use `install_macos_service.sh`. A home connection is generally more reliable
than a cloud datacenter for retailer pages; whichever machine hosts it must be
awake and online. Running the cloud fallback too is fine because delivery state
is independent.

**If you keep the repo private, watch your Actions minutes.** GitHub Free
gives 2,000 minutes/month for private repos. Continuous monitoring burns
about 44,000. You'd hit the cap in roughly a day and a half. Options:

- **Make the repo public** — Actions minutes are unlimited and free on
  public repos. Your secrets stay secret; they live in the Actions secrets
  store, not the code. This is the easy answer.
- **Keep it private and rely mainly on `run_local.bat`**, using Actions
  only as a backup.

### Install the supervised Windows service

Run this once from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_windows_tasks.ps1
```

The installer verifies Python 3.11+, installs the Python/browser dependencies
and Chromium, runs the project self-tests, then installs two hidden scheduled
tasks. `Zelda Stock Monitor` runs Python directly through Task Scheduler—there
is no BAT file, PowerShell wrapper, or console window—and restarts after
failures. `Zelda Stock Monitor Watchdog` runs independently every two minutes;
if the heartbeat is stale it restarts the task and reports the outcome to
Discord. Output goes to the bounded rotating log `logs/windows-monitor.log`,
and a Windows mutex prevents duplicate local loops from producing duplicate
alerts.

### Windows control panel

The Windows installer also creates a **Stock Watch** shortcut on the
desktop. It opens a native control panel—without a Command Prompt window—with
Start, Stop, and Restart controls and a live, color-coded feed of every retained
stock, pre-order, out-of-stock, blocked, and error result. Closing the panel
leaves monitoring active. **Stop Bot** disables both scheduled tasks so the
watchdog does not restart a monitor that you intentionally stopped.

Select **Products** in the control panel to manage additional links without
editing Python:

- **Stock / preorder** watches a product page for a trustworthy structured or
  rendered buyable signal.
- **Listing goes live** watches a search or category page for a distinctive
  phrase, which is useful before a retailer publishes a product page.
- Each custom item can use the primary or secondary Discord webhook, its own
  polling interval, and an optional direct-cart URL.
- Saving, enabling, disabling, or removing an item safely restarts the service
  so the updated catalog takes effect immediately. Built-in Zelda monitors are
  kept separately and are never overwritten.

Custom links are stored locally in `custom_products.json`. The file is ignored
by Git so personal shopping links do not get committed accidentally. The
catalog accepts up to 50 items and rejects malformed/non-web URLs. Known
Target and Walmart identifiers are extracted automatically; other stores use
the conservative generic detector and will return unknown instead of sending a
weak or unverified stock alert.

---

## Optional upgrades

**Pushover** (~$5 one-time, no subscription) — the best answer to "I'm at
work". Stock alerts go out at priority 2, which overrides silent mode and
Do Not Disturb and re-alerts every 30 seconds for up to an hour until you
acknowledge it on your phone. Add secrets `PUSHOVER_TOKEN` and
`PUSHOVER_USER`; no code changes needed.

**Best Buy API key — not available.** Their developer portal rejects
registration with: *"Free email and .edu addresses are not allowed at this
time."* That covers Gmail, Outlook, Yahoo and any `.edu`. A custom-domain
address would pass, but that means owning a domain, and a work address
isn't an appropriate place to put a personal side project. The code path
is still there (`BESTBUY_API_KEY`), so if you ever do have a qualifying
address it activates with no code changes. Until then Best Buy falls back
to JSON-LD parsing, which works fine from a residential IP and is
unreliable from GitHub Actions.

**Social feed watching** (optional) — set `ENABLE_FEED_WATCH=1`; see below.

**Email-to-SMS** — still supported via the original four secrets, but it's
now a backup channel, not the primary. Gateway delivery runs 30 seconds to
several minutes and carriers increasingly filter it.

---

## Troubleshooting

```
python stock_monitor.py --debug     # one pass, showing every verdict's reasoning
python stock_monitor.py --status    # what the bot currently believes
python test_detect.py               # verify the detection logic still holds
```

`--debug` prints the layer that decided, the reason, the HTTP status and
the response size for each retailer. A body of a few hundred bytes plus
`blocked` means bot detection; a large body plus `unknown` means the page
structure moved and the phrase layer is correctly declining to guess.

---

## Trust and self-checking

This bot fired two false "IN STOCK" alerts during development. Everything
below exists because of that.

**Alert first, confirm second.** A strong positive reading sends the urgent
alert immediately. A second check and screenshot happen afterward, followed by
either a quiet confirmation or a correction. This preserves the seconds that
matter without pretending one transient page state is certain.
`CONFIRM_BEFORE_ALERT=0` skips the follow-up check.

**Screenshots.** Stock alerts carry a picture of the actual buy box, so you
can judge it yourself in one glance rather than trusting a verdict.
`ALERT_SCREENSHOTS=0` disables it.

**Check log.** Every reading is appended to `checks.csv` (gitignored), so
"was it briefly available overnight?" is answerable after the fact.

**`--probe`.** When a verdict looks wrong, this shows you exactly what the
detector saw — every buy control with its disabled state, the fulfilment
panel, and the reasoning:

```
python stock_monitor.py --probe target
python stock_monitor.py --probe bestbuy
python stock_monitor.py --probe controller_target
python stock_monitor.py --probe controller_bestbuy
```

## Daily check-in

Each Discord channel's editable live-status message refreshes every minute. In
addition, once a day between 8am and 11am local, the bot posts a separate quiet
console/controller summary. It
reports the *current* reading and says plainly when that's stale — e.g.
`❓ unknown (last known: in_stock, last read 2h ago)` — because a check-in
that silently shows a days-old status is worse than none.

Without it, "no alerts" is ambiguous: still unavailable, or terminal closed
three days ago? The 90-minute health alert catches hard failures; this
catches the soft ones.

`HEARTBEAT_HOUR` moves it, `HEARTBEAT_WINDOW_HOURS` widens the window,
`HEARTBEAT_ENABLED=0` turns it off.

## Which retailers need what

| Retailer | Resolved by | Notes |
|---|---|---|
| GameStop | HTTP TLS profile + JSON-LD | Avoids intermittent Cloudflare blocks without launching Chrome |
| Target | Headless rendered DOM | Needs Playwright/Chromium — HTML carries no availability at all; no visible window opens |
| Walmart | HTTP exact-ID search state | Never launches Chromium; recommendation inventory is ignored and a bot challenge is reported rather than guessed |
| Best Buy | HTTP official Q&A SKU button or the API | Never launches Chromium; avoids the PDP's intermittent HTTP/2 resets and rejects optimistic JSON-LD |
| Nintendo | JSON-LD / app state | Usually fine |
| Costco | Phrase watch | Watching for the edition to appear at all |

## Tuning

Everything is in `config.py` or environment variables: `TARGET_INTERVAL`,
`GAMESTOP_INTERVAL`, `NINTENDO_INTERVAL`, the corresponding
`CONTROLLER_*_INTERVAL` values, `JITTER`,
`REALERT_MINUTES` (default 20 — it keeps
reminding you while the item stays buyable, so one missed notification
doesn't cost you the console), `MAX_REALERTS`,
`HEALTH_ALERT_AFTER_MINUTES`.

Don't push the interval below ~20 seconds. It doesn't meaningfully improve
your odds and it makes you look like exactly the traffic pattern these
sites block.

---

## About auto-adding to cart

The alert includes tap-once add-to-cart links where the retailer supports
them (Best Buy and Walmart both do). Combined with saved payment and
shipping details, that gets you from notification to placed order in a few
taps, which is realistically what decides these drops.

Going further — a script that logs in and checks out for you — is worth
being clear-eyed about. Every one of these retailers prohibits automated
purchasing in their terms, and their drop-time bot detection is tuned
specifically for it: the usual outcome is a cancelled order or a locked
account, which is a worse position than just being fast by hand. The
honest ranking is that a loud, fast alert plus pre-saved checkout details
beats a fragile cart bot most of the time.

## Watching @Wario64 (the fallback)

@Wario64 and @IGNDeals frequently post a restock link before any scraper
notices, so they're a genuinely useful hedge — especially given Best Buy's
API is closed to us and Actions gets blocked.

**Do this manually — it's the reliable version.** In the X app, open
[@Wario64](https://x.com/Wario64), tap the bell icon on the profile, and
choose **All posts**. Same for [@IGNDeals](https://x.com/IGNDeals). You
get a real push notification the moment they post, with no infrastructure
to break. Five seconds of setup, and it is more dependable than anything
in this repo.

**`feeds.py` can also do it automatically** and forwards
matching posts to the same Discord channel when `ENABLE_FEED_WATCH=1`. But be clear-eyed about it: X
has no free API and Nitter shut down in 2024, so this depends on RSSHub
mirrors that rate-limit and disappear. It tries several per account and
fails silently when they're all down — it will not warn you, because a
bonus signal going quiet shouldn't look like the retailer checks breaking.

It only forwards posts matching "zelda" plus one of "40th" / "anniversary"
/ "switch 2", so you aren't pinged for every deal they post. Adjust
`MATCH_ALL` / `MATCH_ANY` in `feeds.py`, or set `ENABLE_FEED_WATCH=0` to
turn it off.

Also worth joining a restock Discord — several relay Wario64
automatically, and it costs nothing to add another set of eyes.
