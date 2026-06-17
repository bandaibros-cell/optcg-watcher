#!/usr/bin/env python3
"""
One Piece TCG Watch Agent
-------------------------
Polls several sources and posts alerts to a Discord webhook:

  * releases  -> new posts on the official OP TCG news page
  * meta      -> new tournaments on Limitless One Piece
  * stock     -> product pages flipping from out-of-stock to in-stock
  * prices    -> watchlist cards crossing a price threshold (via a price API)

State is kept in state.json so you only get alerted once per event.
Run it on a schedule (GitHub Actions cron, or cron/Task Scheduler locally).

Usage:
  python watcher.py            # run all enabled watchers once
  python watcher.py --test     # send a test message to the webhook and exit
  python watcher.py --prime    # record current state WITHOUT sending alerts

Config lives in config.yaml. The Discord webhook and any API key can also be
supplied via environment variables (DISCORD_WEBHOOK, and whatever you name in
prices.api_key_env) so you never commit secrets.
"""

import os
import re
import sys
import json
import time
import argparse
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.yaml")
UA = "Mozilla/5.0 (compatible; OPTCG-Watcher/1.0; +https://github.com/)"
TIMEOUT = 25


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# HTTP + Discord
# --------------------------------------------------------------------------- #
def fetch(url):
    """GET a URL and return (status_code, text). Never raises."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        return r.status_code, r.text
    except requests.RequestException as e:
        print(f"  ! request failed for {url}: {e}")
        return None, ""


def fetch_json(url, headers=None):
    try:
        r = requests.get(url, headers={"User-Agent": UA, **(headers or {})},
                          timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ! {url} returned HTTP {r.status_code}")
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  ! json fetch failed for {url}: {e}")
        return None


def discord(webhook, title, description, link=None, color=0x2B6CB0, footer=None):
    """Post a single embed to the Discord webhook. Returns True on success."""
    if not webhook:
        print("  ! no Discord webhook configured; would have sent:", title)
        return False
    embed = {"title": title[:256], "description": description[:4000], "color": color}
    if link:
        embed["url"] = link
    if footer:
        embed["footer"] = {"text": footer[:2048]}
    try:
        r = requests.post(webhook, json={"embeds": [embed]}, timeout=TIMEOUT)
        # Discord returns 204 No Content on success
        if r.status_code not in (200, 204):
            print(f"  ! Discord webhook HTTP {r.status_code}: {r.text[:200]}")
            return False
        time.sleep(0.6)  # be gentle with webhook rate limits
        return True
    except requests.RequestException as e:
        print(f"  ! Discord post failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# Watcher: new links on a page (used for releases + meta)
# --------------------------------------------------------------------------- #
def extract_links(html, base_url, link_contains):
    """Return [(absolute_url, text), ...] for anchors whose href contains
    `link_contains`. De-duplicated, preserving order."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if link_contains and link_contains not in href:
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        text = " ".join(a.get_text(" ", strip=True).split())
        out.append((absolute, text or absolute))
    return out


def watch_links(name, cfg, state, webhook, prime=False):
    print(f"[{name}] checking {cfg['url']}")
    status, html = fetch(cfg["url"])
    if not html:
        return
    links = extract_links(html, cfg["url"], cfg.get("link_contains", ""))
    if not links:
        print(f"  (no matching links found - selector may need tweaking)")
        return

    seen = set(state.get(name, []))
    current = [u for u, _ in links]

    if not seen or prime:
        state[name] = current
        print(f"  primed with {len(current)} items (no alerts sent)")
        return

    new_items = [(u, t) for (u, t) in links if u not in seen]
    print(f"  {len(new_items)} new item(s)")
    label = cfg.get("label", name)
    color = cfg.get("color", 0x2B6CB0)
    for url, text in reversed(new_items):  # oldest first
        discord(webhook, f"{label}: {text}", f"New item detected.\n{url}",
                link=url, color=color)

    # keep the union, capped so the file doesn't grow forever
    state[name] = (current + list(seen))[:500]


# --------------------------------------------------------------------------- #
# Watcher: stock / restock
# --------------------------------------------------------------------------- #
def watch_stock(name, cfg, state, webhook, prime=False):
    store = state.setdefault(name, {})
    label = cfg.get("label", "Restock")
    for product in cfg.get("products", []):
        pname = product["name"]
        url = product["url"]
        oos_text = product.get("out_of_stock_text", "")
        in_text = product.get("in_stock_text", "")
        print(f"[{name}] {pname}")
        status, html = fetch(url)
        if not html:
            continue
        lower = html.lower()

        # Determine availability. Prefer an explicit in-stock signal if given,
        # otherwise infer from the absence of the out-of-stock signal.
        if in_text:
            in_stock = in_text.lower() in lower
        elif oos_text:
            in_stock = oos_text.lower() not in lower
        else:
            print("  ! product needs out_of_stock_text or in_stock_text; skipping")
            continue

        prev = store.get(url)
        store[url] = in_stock
        if prev is None or prime:
            print(f"  primed (in_stock={in_stock})")
            continue
        if in_stock and not prev:
            discord(webhook, f"{label}: {pname} is BACK IN STOCK",
                    f"Was out of stock, now available.\n{url}",
                    link=url, color=0x2F9E44)
            print("  -> RESTOCK alert sent")
        elif not in_stock and prev:
            print("  went out of stock (no alert)")
        else:
            print(f"  no change (in_stock={in_stock})")


# --------------------------------------------------------------------------- #
# Watcher: prices (generic price-API client)
# --------------------------------------------------------------------------- #
def dig(obj, dotted):
    """Pull a nested value out of dicts/lists by a dotted path like
    'data.0.market_price'. Returns None if the path doesn't resolve."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def watch_prices(name, cfg, state, webhook, prime=False):
    api_base = cfg.get("api_base", "").rstrip("/")
    if not api_base:
        print(f"[{name}] no api_base set; skipping price watcher")
        return
    key = os.environ.get(cfg.get("api_key_env", ""), "")
    headers = {}
    if key:
        # Most of these APIs accept a bearer token or x-api-key. Adjust if needed.
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key

    store = state.setdefault(name, {})
    label = cfg.get("label", "Price")
    # JSON path (relative to the search response) where the price lives.
    price_path = cfg.get("price_path", "data.0.market_price")
    search_tmpl = cfg.get("search_url", api_base + "/search?game={game}&q={query}")
    game = cfg.get("game_slug", "one-piece")

    for item in cfg.get("watchlist", []):
        query = item["query"]
        url = search_tmpl.format(game=game, query=requests.utils.quote(query))
        print(f"[{name}] {query}")
        data = fetch_json(url, headers=headers)
        if data is None:
            continue
        raw = dig(data, price_path)
        try:
            price = float(raw)
        except (TypeError, ValueError):
            print(f"  ! could not read price at path '{price_path}' "
                  f"(check your provider's response shape). Got: {raw!r}")
            continue
        print(f"  market = {price}")

        below = item.get("below")
        above = item.get("above")
        triggered = (below is not None and price < below) or \
                    (above is not None and price > above)

        prev_triggered = store.get(query, {}).get("triggered", False)
        store[query] = {"price": price, "triggered": triggered}

        if prime:
            continue
        if triggered and not prev_triggered:
            cond = f"below ${below:.2f}" if (below is not None and price < below) \
                   else f"above ${above:.2f}"
            discord(webhook, f"{label}: {query}",
                    f"Market price **${price:.2f}** is now {cond}.",
                    color=0xE8590C,
                    footer="One Piece TCG price alert")
            print("  -> PRICE alert sent")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
WATCHERS = {
    "releases": watch_links,
    "meta": watch_links,
    "stock": watch_stock,
    "prices": watch_prices,
}


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_webhook(cfg):
    return os.environ.get("DISCORD_WEBHOOK") or cfg.get("discord_webhook", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="send a test message to Discord and exit")
    ap.add_argument("--prime", action="store_true",
                    help="record current state without sending alerts")
    args = ap.parse_args()

    cfg = load_config()
    webhook = get_webhook(cfg)

    if args.test:
        ok = discord(webhook, "✅ OP TCG Watcher test",
                     "If you can read this, your webhook works.")
        sys.exit(0 if ok else 1)

    state = load_state()
    for name, fn in WATCHERS.items():
        section = cfg.get(name)
        if not section or not section.get("enabled", False):
            continue
        try:
            fn(name, section, state, webhook, prime=args.prime)
        except Exception as e:  # never let one watcher kill the rest
            print(f"[{name}] ERROR: {e}")
    save_state(state)
    print("done.")


if __name__ == "__main__":
    main()
