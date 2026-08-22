#!/usr/bin/env python3
"""
Pulls the count of orders that are sold but not yet shipped from eBay and
Etsy, and rewrites the "Ready to ship" tile + card in index.html between
the RTS_TILE_START/END and RTS_CARD_START/END HTML comment markers.

Required secrets (set in GitHub repo Settings > Secrets and variables > Actions):
  EBAY_CLIENT_ID       - eBay app Client ID (App ID)
  EBAY_CLIENT_SECRET   - eBay app Client Secret (Cert ID)
  EBAY_REFRESH_TOKEN   - eBay user refresh token (from the developer portal's
                          "Sign in to Production" token tool)
  ETSY_KEYSTRING       - Etsy app API key ("keystring")
  ETSY_REFRESH_TOKEN   - Etsy user refresh token (from the manual PKCE
                          authorization-code exchange -- see SETUP_GUIDE.md)
  ETSY_SHOP_NAME       - the name in your shop's URL, e.g. "theredboneforge"
                          for theredboneforge.etsy.com. Used to look up your
                          shop id via Etsy's public shop-search endpoint
                          instead of a user-scoped lookup (which needs a
                          broader permission than plain order-reading does).
  GH_PAT               - a fine-grained GitHub personal access token, scoped
                          to just this repo, with "Secrets: write" permission.
                          Etsy's refresh token rotates every time it's used --
                          this lets the script save the new one back to this
                          repo's secrets so the next run still works.

eBay's refresh token is long-lived and reused as-is, so it never needs to be
written back anywhere. Etsy's does not work that way: every refresh call
invalidates the old refresh token and hands back a brand new one, so this
script must persist it via the GitHub API or the automation will die after
the very first Etsy run.

eBay and Etsy are fetched independently -- if one fails (bad token, API
hiccup, etc.) the other still updates normally, and the card shows an
"couldn't check" message for just the one that failed instead of leaving
the whole dashboard stale.
"""
import base64
import datetime
import re
import os
import sys

import requests

INDEX_PATH = "index.html"


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

    # Count orders that are sold but not fully shipped yet -- this is what
    # "Ready to ship" should actually mean, rather than "sold today" (which
    # would keep counting an order after you've already shipped it).
    orders_resp = requests.get(
        "https://api.ebay.com/sell/fulfillment/v1/order",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "filter": "orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}",
            "limit": 50,
        },
        timeout=20,
    )
    orders_resp.raise_for_status()
    return orders_resp.json().get("total", 0)


def refresh_etsy_token():
    """Exchange the current Etsy refresh token for a new access token AND a
    new refresh token (Etsy rotates the refresh token on every use)."""
    client_id = os.environ["ETSY_KEYSTRING"]
    refresh_token = os.environ["ETSY_REFRESH_TOKEN"]

    resp = requests.post(
        "https://api.etsy.com/v3/public/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["access_token"], data["refresh_token"]


def get_etsy_shop_id(client_id, shop_name):
    """Public shop-search endpoint -- only needs the app's client id, not a
    user access token, so it isn't gated by whatever scope the OAuth grant
    has. Sidesteps a 403 we hit looking shops up via the user-scoped
    endpoint, which apparently wants more than "transactions_r"."""
    resp = requests.get(
        "https://api.etsy.com/v3/application/shops",
        headers={"x-api-key": client_id},
        params={"shop_name": shop_name},
        timeout=20,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise RuntimeError(f"No Etsy shop found named {shop_name!r}")
    return results[0]["shop_id"]


def get_etsy_count(access_token):
    client_id = os.environ["ETSY_KEYSTRING"]
    shop_name = os.environ["ETSY_SHOP_NAME"]
    headers = {"x-api-key": client_id, "Authorization": f"Bearer {access_token}"}

    shop_id = get_etsy_shop_id(client_id, shop_name)

    receipts_resp = requests.get(
        f"https://api.etsy.com/v3/application/shops/{shop_id}/receipts",
        headers=headers,
        params={"was_shipped": "false", "limit": 100},
        timeout=20,
    )
    receipts_resp.raise_for_status()
    return receipts_resp.json().get("count", 0)


def update_github_secret(secret_name, secret_value):
    """Encrypt secret_value with the repo's public key (libsodium sealed
    box, per GitHub's API) and store it as a repo Actions secret."""
    from nacl import encoding, public

    repo = os.environ["GITHUB_REPOSITORY"]  # "owner/repo", auto-set by Actions
    pat = os.environ["GH_PAT"]
    api = f"https://api.github.com/repos/{repo}/actions/secrets"
    headers = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}

    key_resp = requests.get(f"{api}/public-key", headers=headers, timeout=20)
    key_resp.raise_for_status()
    key_data = key_resp.json()

    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    put_resp = requests.put(
        f"{api}/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
        timeout=20,
    )
    put_resp.raise_for_status()


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


def build_row(label, url, count, empty_text, error_text):
    if count is None:
        return f'    <div class="dash-empty">{error_text}</div>'
    if count > 0:
        noun = "order" if count == 1 else "orders"
        return (
            f'    <a class="dash-item" href="{url}" target="_blank" rel="noopener">\n'
            '      <div class="dot2 ok"></div>\n'
            '      <div class="dash-item-body">\n'
            f'        <p class="dash-item-title">{label} &rarr;</p>\n'
            f'        <p class="dash-item-meta">{count} {noun} awaiting shipment</p>\n'
            "      </div>\n"
            "    </a>"
        )
    return f'    <div class="dash-empty">{empty_text}</div>'


def build_card(ebay_count, etsy_count):
    rows = "\n".join(
        [
            build_row(
                "eBay",
                "https://www.ebay.com/sh/ovw",
                ebay_count,
                "Nothing waiting to ship on eBay.",
                "Couldn't check eBay just now.",
            ),
            build_row(
                "Etsy",
                "https://www.etsy.com/your/shops/me/order-list?tab=open",
                etsy_count,
                "Nothing waiting to ship on Etsy.",
                "Couldn't check Etsy just now.",
            ),
        ]
    )
    return (
        '\n  <div class="dash-section-label">Ready to ship</div>\n'
        f'  <div class="dash-card">\n{rows}\n  </div>\n  '
    )


def main():
    ebay_count = None
    etsy_count = None

    try:
        ebay_count = get_ebay_count()
    except Exception as e:  # noqa: BLE001
        print(f"eBay fetch failed: {e}", file=sys.stderr)

    try:
        old_refresh_token = os.environ["ETSY_REFRESH_TOKEN"]
        access_token, new_refresh_token = refresh_etsy_token()
        etsy_count = get_etsy_count(access_token)
        if new_refresh_token != old_refresh_token:
            update_github_secret("ETSY_REFRESH_TOKEN", new_refresh_token)
            print("Etsy refresh token rotated and saved to GitHub secrets.")
    except Exception as e:  # noqa: BLE001
        print(f"Etsy fetch failed: {e}", file=sys.stderr)

    if ebay_count is None and etsy_count is None:
        print("Both eBay and Etsy fetches failed -- leaving index.html unchanged.", file=sys.stderr)
        sys.exit(1)

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    total = (ebay_count or 0) + (etsy_count or 0)
    html = replace_between(html, "<!--RTS_TILE_START-->", "<!--RTS_TILE_END-->", build_tile(total))
    html = replace_between(html, "<!--RTS_CARD_START-->", "<!--RTS_CARD_END-->", build_card(ebay_count, etsy_count))

    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%b %-d, %-I:%M %p UTC")
    html = re.sub(
        r'(document\.getElementById\("dashUpdated"\)\.textContent = ")[^"]*(";)',
        rf"\g<1>Updated {now_str}\g<2>",
        html,
        count=1,
    )

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"eBay: {ebay_count if ebay_count is not None else 'failed'}, Etsy: {etsy_count if etsy_count is not None else 'failed'}")


if __name__ == "__main__":
    main()
