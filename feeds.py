"""
Social feed watcher -- a best-effort second pair of eyes.

Accounts like @Wario64 and @IGNDeals routinely post a restock link before
any scraper notices, so watching them is a genuinely useful hedge against
this bot's own detection being blocked or wrong.

The catch: X has no free API, and Nitter shut down in 2024 when X removed
guest accounts. What's left are RSSHub-style mirrors, which are
rate-limited, come and go, and will fail regularly. So this module is
built to fail quietly:

  * several mirrors per account, tried in order until one answers
  * a silent failure is NOT a health alert -- it's expected
  * it never blocks or slows the retailer checks

Treat it as a bonus. The reliable way to follow these accounts is to turn
on post notifications for them in the X app (see the README).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import requests

import config

TIMEOUT = 12

# {account: [mirror url templates, tried in order]}
ACCOUNTS = {
    "Wario64": [
        "https://rsshub.app/twitter/user/Wario64",
        "https://rsshub.rssforever.com/twitter/user/Wario64",
    ],
    "IGNDeals": [
        "https://rsshub.app/twitter/user/IGNDeals",
        "https://rsshub.rssforever.com/twitter/user/IGNDeals",
    ],
}

# A post must contain every ALL term and at least one ANY term.
MATCH_ALL = ("zelda",)
MATCH_ANY = ("40th", "anniversary", "switch 2", "switch2")

_TAG_RE = re.compile(r"<[^>]+>")


def _items(xml_text: str) -> list[tuple[str, str, str]]:
    """Return [(item_id, title_text, link)] from an RSS or Atom document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    out: list[tuple[str, str, str]] = []

    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue

        title = link = ident = ""
        for child in item:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag in ("title", "description", "summary", "content") and child.text:
                title += " " + _TAG_RE.sub(" ", child.text)
            elif ctag == "link":
                link = child.get("href") or (child.text or "")
            elif ctag in ("guid", "id") and child.text:
                ident = child.text

        ident = ident or link or title[:120]
        if ident:
            out.append((ident.strip(), re.sub(r"\s+", " ", title).strip(), link.strip()))
    return out


def _matches(text: str) -> bool:
    low = text.lower()
    return all(t in low for t in MATCH_ALL) and any(t in low for t in MATCH_ANY)


def poll(seen: list[str]) -> tuple[list[dict], list[str]]:
    """
    Check every account. Returns (new_matching_posts, updated_seen_ids).

    `seen` is a rolling list of post ids we've already alerted on, so a
    post that stays at the top of the feed doesn't re-alert every minute.
    """
    seen_set = set(seen)
    fresh: list[dict] = []
    newly_seen: list[str] = []

    for account, mirrors in ACCOUNTS.items():
        for url in mirrors:
            try:
                resp = requests.get(
                    url,
                    timeout=TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/rss+xml, application/xml"},
                )
                resp.raise_for_status()
            except requests.RequestException:
                continue  # mirror down; try the next one

            items = _items(resp.text)
            if not items:
                continue

            for ident, title, link in items:
                newly_seen.append(ident)
                if ident in seen_set or not _matches(title):
                    continue
                fresh.append({"account": account, "text": title, "link": link, "id": ident})
            break  # this account answered; don't hit its other mirrors

    # Keep the rolling window bounded, newest first.
    merged = newly_seen + [s for s in seen if s not in set(newly_seen)]
    return fresh, merged[:400]
