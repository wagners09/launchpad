#!/usr/bin/env python3
"""
Pulls today's sold-order count from eBay and rewrites the "Ready to ship"
tile + card in index.html between the RTS_TILE_START/END and
RTS_CARD_START/END HTML comment markers.

Required secrets (set in GitHub repo Settings > Secrets and variables > Actions):
  EBAY_CLIENT_ID       - eBay app Client ID (App ID)
  EBAY_CLIENT_SECRET   - eBay app Client Secret (Cert ID)
  EBAY_REFRESH_TOKEN   - eBay user refresh token (from the developer portal's
                          "Sign in to Production" token tool)

eBay's refresh tokens are long-lived and reused as-is -- they don't rotate on
every call, so unlike Etsy there's no need to write anything back to GitHub
secrets. This keeps the whole setup a lot simpler.
"""
import base64
import datetime
import re
import os
import sys

import requests

INDEX_PATH = "index.html"


def today_window_utc():
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


def get_ebay_count():
    client_id = os.environ["EBAY_CLIENT_ID"]
    client_secret = os.environ["EBAY_CLIENT_SECRET"]
    refresh_token = os.environ["EBAY_REFRESH_TOKEN"]

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    tok_resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
        },
        timeout=20,
    )
    tok_resp.raise_for_status()
    access_token = tok_resp.json()["access_token"]

    start, end = today_window_utc()
    filt = (
        "creationdate:["
        f"{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}.."
        f"{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}]"
    )
    orders_resp = requests.get(
        "https://api.ebay.com/sell/fulfillment/v1/order",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"filter": filt, "limit": 50},
        timeout=20,
    )
    orders_resp.raise_for_status()
    return orders_resp.json().get("total", 0)


def replace_between(html, start_marker, end_marker, new_inner):
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL
    )
    replacement = start_marker + new_inner + end_marker
    new_html, n = pattern.subn(replacement, html, count=1)
    if n != 1:
        raise RuntimeError(f"Marker pair {start_marker}...{end_marker} not found exactly once")
    return new_html


def build_tile(total):
    return f'<div class="dash-tile ship"><div class="num">{total}</div><div class="label">Ready to ship</div></div>'


def build_card(ebay_count):
    if ebay_count > 0:
        noun = "item" if ebay_count == 1 else "items"
        rows = (
            '    <a class="dash-item" href="https://www.ebay.com/sh/ovw" target="_blank" rel="noopener">\n'
            '      <div class="dot2 ok"></div>\n'
            '      <div class="dash-item-body">\n'
            '        <p class="dash-item-title">eBay &rarr;</p>\n'
            f'        <p class="dash-item-meta">{ebay_count} {noun} sold today</p>\n'
            "      </div>\n"
            "    </a>"
        )
    else:
        rows = '    <div class="dash-empty">Nothing sold on eBay yet today.</div>'

    return (
        '\n  <div class="dash-section-label">Ready to ship &middot; sold today &middot; eBay</div>\n'
        f'  <div class="dash-card">\n{rows}\n  </div>\n  '
    )


def main():
    try:
        ebay_count = get_ebay_count()
    except Exception as e:  # noqa: BLE001
        print(f"eBay fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    html = replace_between(html, "<!--RTS_TILE_START-->", "<!--RTS_TILE_END-->", build_tile(ebay_count))
    html = replace_between(html, "<!--RTS_CARD_START-->", "<!--RTS_CARD_END-->", build_card(ebay_count))

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%b %-d, %-I:%M %p UTC")
    html = re.sub(
        r'(document\.getElementById\("dashUpdated"\)\.textContent = ")[^"]*(";)',
        rf"\g<1>Updated {now_str}\g<2>",
        html,
        count=1,
    )

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"eBay: {ebay_count} sold today")


if __name__ == "__main__":
    main()
