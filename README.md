# Zelda Switch 2 Stock Monitor

Watches Target, Walmart, Best Buy, the Nintendo Store and Costco for the
**Nintendo Switch 2 — The Legend of Zelda 40th Anniversary Edition**
(Best Buy SKU `6691841`, $519.99, release 10/29/2026) and pushes an alert
the moment it becomes buyable — in stock *or* a pre-order window
reopening.

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
| 1 | Official retailer API (Best Buy; Target with a key) | Definitive |
| 2 | `schema.org` JSON-LD `offers.availability` | High |
| 3 | Embedded app state (`__NEXT_DATA__`) | Good |
| 4 | Scoped page-text phrases | Last resort |

Layer 4 **refuses to answer** when a page contains both buy and sold-out
language, reporting `unknown` instead of guessing. That's deliberate — a
confident wrong answer is worse than an honest "I don't know", because
you'd never know to go look.

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

**Treat that URL like a password** — anyone who has it can post into your
channel. Don't commit it, and don't paste it into a screenshot or a chat.
If it ever leaks, delete the webhook in Discord and create a new one;
that instantly invalidates the old URL. `.env` and `stock_state.json` are
gitignored so they can't be committed by accident.

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
repository secret**, name `DISCORD_WEBHOOK_URL`, paste the URL. Then
**Actions** tab → enable workflows → **Stock Monitor** → **Run workflow**.

Running both at once is fine and recommended; the alerts are independent.

---

## Where to run it — read this before relying on it

**GitHub Actions runners are Azure datacenter IPs, and Target, Walmart and
Best Buy block those hard.** You will likely see `blocked` for those three
from Actions while the exact same code works fine from your house. Two
consequences:

**Run it on your PC too.** Fill in the webhook in `run_local.bat` and
double-click it. Your residential IP is treated as a normal shopper. This
is the more reliable of the two — its only weakness is that your PC has to
be awake. Running both at once is fine; the alerts are independent.

**If you keep the repo private, watch your Actions minutes.** GitHub Free
gives 2,000 minutes/month for private repos. Continuous monitoring burns
about 44,000. You'd hit the cap in roughly a day and a half. Options:

- **Make the repo public** — Actions minutes are unlimited and free on
  public repos. Your secrets stay secret; they live in the Actions secrets
  store, not the code. This is the easy answer.
- **Keep it private and rely mainly on `run_local.bat`**, using Actions
  only as a backup.

To run local monitoring in the background at startup, point Windows Task
Scheduler at `run_local.bat` with trigger "At log on".

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

**Social feed watching** (already on) — see below.

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

## Tuning

Everything is in `config.py` or environment variables: `CHECK_INTERVAL`
(default 60s), `JITTER`, `REALERT_MINUTES` (default 20 — it keeps
reminding you while the item stays buyable, so one missed notification
doesn't cost you the console), `MAX_REALERTS`,
`HEALTH_ALERT_AFTER_MINUTES`.

Don't push the interval below ~30 seconds. It doesn't meaningfully improve
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

**`feeds.py` also does it automatically**, on by default, and forwards
matching posts to the same Discord channel. But be clear-eyed about it: X
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
