# Zelda Switch 2 Stock Monitor

Checks Target, Walmart, and Best Buy every 5 minutes and texts you the
moment one comes back in stock. Runs for free on GitHub's servers —
your computer doesn't need to be on.

## Setup (about 10 minutes)

### 1. Create a GitHub repo
- Go to https://github.com/new
- Name it anything (e.g. `zelda-stock-bot`)
- **Set it to Private** (recommended — no reason for this to be public)
- Upload all the files in this folder to it (drag-and-drop on the GitHub
  web UI works fine, or `git push` if you're comfortable with git)

### 2. Get a Gmail "App Password" (used to send the free text)
This is a special password just for apps — it's not your real Gmail
password, and you can revoke it anytime.
1. Turn on 2-Step Verification on your Google account if it isn't already:
   https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create a new app password (name it "stock bot"), copy the 16-character
   code it gives you

### 3. Find your carrier's email-to-text gateway
Your phone number + this domain = a free text message.

| Carrier      | Gateway domain          |
|--------------|--------------------------|
| Verizon      | vtext.com                |
| AT&T         | txt.att.net              |
| T-Mobile     | tmomail.net              |
| Sprint       | messaging.sprintpcs.com  |
| Boost Mobile | sms.myboostmobile.com    |
| Cricket      | sms.cricketwireless.net  |
| Google Fi    | msg.fi.google.com        |
| Visible      | vtext.com                |

(If yours isn't listed, search "[your carrier] email to text gateway".)

### 4. Add secrets to your GitHub repo
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:

| Secret name           | Value                                            |
|------------------------|--------------------------------------------------|
| `SMS_TO_NUMBER`         | Your 10-digit number, no dashes, e.g. `5551234567` |
| `SMS_CARRIER_GATEWAY`   | e.g. `vtext.com` (from the table above)          |
| `GMAIL_USER`            | Your Gmail address                                |
| `GMAIL_APP_PASSWORD`    | The 16-character app password from step 2        |

### 5. Turn it on
Go to the **Actions** tab in your repo → click into "Stock Monitor" →
you may need to click "I understand my workflows, enable them" the
first time. It'll then run automatically every 5 minutes.

You can also click **"Run workflow"** to trigger it manually right now
and make sure it works.

## Important things to know

- **GitHub pauses scheduled runs after 60 days of repo inactivity.**
  If you haven't pushed any commits in 60 days, go re-enable it in the
  Actions tab.
- **This can't complete checkout for you.** It only alerts you — you
  still click through and buy it yourself. Retailers explicitly ban
  automated-purchase bots in their terms of service, and their
  bot-detection would likely flag/block an account that tried it
  anyway.
- **Best Buy and Nintendo Store are the least reliable to detect.**
  Target and Walmart show "Out of Stock" directly in the page's raw
  HTML, so the simple text-matching in this script works well for them.
  Best Buy's and Nintendo's pages render more of their content with
  JavaScript after load and use stronger bot-detection, so those two
  checks are rougher heuristics — they may occasionally report
  "unknown" instead of a clear answer, or (for Nintendo especially) get
  blocked outright by anti-bot protection rather than served the real
  page. If that becomes a recurring problem, the fix is switching those
  checks to a headless browser (Playwright) instead of a plain HTTP
  request — let me know if you want that added.
- **Costco doesn't have a product page for this edition yet** — as of
  now they're not carrying it. So instead of a stock check, the Costco
  entry watches their general Switch 2 listing page
  (costco.com/nintendo-switch-2.html) for the phrase "40th anniversary"
  to show up anywhere on it. If Costco starts selling it, that phrase
  should appear and you'll get a text. This is a broader/rougher signal
  than the other checks (it's watching for a new product to appear, not
  restocking an existing one), so treat a Costco alert as "go check
  manually" rather than "definitely buyable this second."
- **Retailers may rate-limit or block frequent automated requests.**
  Every-5-minutes checking is a normal, light monitoring frequency,
  but if you notice the script suddenly can't reach a site at all,
  that retailer may be temporarily blocking the request pattern —
  stretching the interval to 10–15 min usually resolves it.

## Alternative: skip building this yourself

If you'd rather not manage a GitHub repo, free third-party services do
the same thing with a simpler setup:
- **Distill.io** (free tier) — visual "watch this page" alerts, can
  connect to SMS via Zapier
- **NowInStock.net** — community-run tracker specifically for hot
  electronics restocks, has Discord alerts
- Following **@Wario64** and **@IGNDeals** on X — they post restocks
  across all major retailers within minutes, often faster than any
  script

## Want more reliable SMS?

The Gmail-gateway method above is free but can be slightly delayed and
some carriers occasionally filter it as spam. For guaranteed instant
delivery, swap in [Twilio](https://www.twilio.com/) instead (a paid
service, but SMS costs about $0.0079 each — a few cents a month at
this check frequency). Ask me and I'll swap the script over to it.
