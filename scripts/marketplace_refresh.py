#!/usr/bin/env python3
"""
marketplace_refresh.py

Reads the three shared marketplace mailboxes under the iCloud account in
Apple Mail:
    - Orders to Ship
    - Offers to Consider
    - Messages to Reply To

...classifies each message by platform, pulls out a deep link to the
relevant page on that platform (from the first recognizable action button
in the email), works out a countdown for offers where an expiration is
stated, and rewrites three sections of index.html the same way
bambu_refresh.py rewrites the print card/tile.

Run with --dry-run first (see bottom of file) to see what it finds without
touching index.html or git. Once that looks right, run for real.

Only looks at messages received in the last MAX_AGE_DAYS days (default 90,
override with --max-age-days) - both to keep this fast and so old/misfiled
mail never shows up as if it were currently pending.

Use --debug to dump the first message's raw HTML source per mailbox to
debug_orders.txt / debug_offers.txt / debug_messages.txt, for inspecting
why the link-extraction logic did or didn't find a URL in your real mail.

NOTE: this is a first pass. Mail's AppleScript dictionary can behave
slightly differently machine to machine / macOS version to macOS version,
so if fetch_mailbox_messages() errors out or returns nothing, that's the
first thing to paste back so we can adjust it live.
"""

import subprocess
import re
import sys
import argparse
import email
from pathlib import Path
from datetime import datetime, timedelta
from html import unescape

REPO_DIR = Path.home() / "launchpad"
INDEX_HTML = REPO_DIR / "index.html"
ACCOUNT_NAME = "iCloud"  # must match the account name exactly as Mail.app shows it

MAILBOXES = {
    "orders": "Orders to Ship",
    "offers": "Offers to Consider",
    "messages": "Messages to Reply To",
}

# Anything older than this is ignored entirely - both to keep runtime bounded
# and so old/misfiled mail (marketing emails, years-old test rows, etc.)
# never shows up as if it were currently pending. Adjust freely.
MAX_AGE_DAYS = 90

FS = "\x1c"  # field separator (ASCII File Separator) - won't appear in real mail
RS = "\x1e"  # record separator (ASCII Record Separator)


# ---------------------------------------------------------------------------
# Mail.app bridge
# ---------------------------------------------------------------------------

def fetch_mailbox_messages(mailbox_name, max_age_days=MAX_AGE_DAYS, timeout=300):
    """
    Returns a list of dicts: {id, sender, subject, date_str, source}
    for messages in the given mailbox under the iCloud account that were
    received within the last `max_age_days` days.
    """
    script = f'''
    set FS to (ASCII character 28)
    set RS to (ASCII character 30)
    set output to ""
    set cutoffDate to (current date) - ({max_age_days} * days)
    tell application "Mail"
        set theAccount to account "{ACCOUNT_NAME}"
        set theMailbox to mailbox "{mailbox_name}" of theAccount
        set theMessages to (messages of theMailbox whose date received > cutoffDate)
        repeat with msg in theMessages
            set msgId to (id of msg) as string
            set msgSender to (sender of msg) as string
            set msgSubject to (subject of msg) as string
            set msgDate to (date received of msg) as string
            set msgSource to (source of msg) as string
            set output to output & msgId & FS & msgSender & FS & msgSubject & FS & msgDate & FS & msgSource & RS
        end repeat
    end tell
    return output
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        print(f"AppleScript error reading '{mailbox_name}': {result.stderr}", file=sys.stderr)
        return []

    messages = []
    for chunk in result.stdout.split(RS):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        parts = chunk.split(FS)
        if len(parts) != 5:
            print(f"  (skipped one malformed record in '{mailbox_name}' - {len(parts)} fields)", file=sys.stderr)
            continue
        msg_id, sender, subject, date_str, source = parts
        messages.append({
            "id": msg_id,
            "sender": sender,
            "subject": subject,
            "date_str": date_str,
            "source": source,
        })
    return messages


# ---------------------------------------------------------------------------
# MIME decoding
# ---------------------------------------------------------------------------

def decode_email_body(raw_source):
    """
    Parses the raw MIME source of an email (exactly what Mail.app's
    `source of msg` hands back) and returns the best-available *decoded*
    body - preferring the HTML part, falling back to plain text - with
    any Content-Transfer-Encoding (quoted-printable, base64, etc.) already
    removed.

    This matters because `source of msg` is the message exactly as
    transmitted: a quoted-printable part encodes every literal '=' as
    '=3D' and soft-wraps long lines with a trailing '=', so something
    like href="https://..." shows up in the raw source as
    href=3D"https://..." and silently fails to match any regex looking
    for a real href attribute. All downstream scanning (link extraction,
    platform detection, offer-expiration parsing) should run against the
    decoded body this function returns, never against raw `source`.
    """
    try:
        msg = email.message_from_string(raw_source)
    except Exception:
        return raw_source

    html_part = None
    text_part = None

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            content_type = part.get_content_type()
            if content_type == "text/html" and html_part is None:
                html_part = part
            elif content_type == "text/plain" and text_part is None:
                text_part = part
    else:
        if msg.get_content_type() == "text/html":
            html_part = msg
        else:
            text_part = msg

    chosen = html_part or text_part
    if chosen is None:
        return raw_source

    try:
        payload = chosen.get_payload(decode=True)
    except Exception:
        payload = None

    if payload is None:
        # Nothing to decode (e.g. the payload was already a plain str) -
        # use it as-is if we can, otherwise give up and return raw source.
        fallback = chosen.get_payload()
        return fallback if isinstance(fallback, str) else raw_source

    charset = chosen.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

PLATFORM_RULES = [
    ("Poshmark", re.compile(r"poshmark", re.I)),
    ("eBay", re.compile(r"ebay", re.I)),
    ("Depop", re.compile(r"depop", re.I)),
    ("Mercari", re.compile(r"mercari", re.I)),
    ("Etsy", re.compile(r"etsy", re.I)),
    ("Shopify", re.compile(r"redboneforge|shopify", re.I)),
]

def detect_platform(sender, subject, source):
    haystack = f"{sender} {subject} {source[:3000]}"
    for name, pattern in PLATFORM_RULES:
        if pattern.search(haystack):
            return name
    return "Unknown"


# ---------------------------------------------------------------------------
# Deep link extraction
# ---------------------------------------------------------------------------

HREF_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)

ACTION_PHRASES = [
    "view order", "view message", "reply", "view your listing",
    "view listing", "accept offer", "counter", "decline offer",
    "purchase shipping label", "download shipping label", "accept or counter",
]

SKIP_PHRASES = [
    "unsubscribe", "privacy", "terms", "app store", "google play",
    "download the", "manage subscriptions", "learn more", "help center",
]

# A link that's just the site's homepage (logo at the top of almost every
# marketing/notification email) - never what we actually want as a deep link.
ROOT_LINK_RE = re.compile(r'^https?://(?:www\.)?[^/?#]+/?(?:[?#].*)?$')

# Bare "https://..." URL, used only as a last-resort fallback for emails
# with no HTML <a> tags at all (plain-text-only messages).
BARE_URL_RE = re.compile(r'https?://[^\s<>"\']+')

def extract_deep_link(source):
    candidates = []
    for match in HREF_RE.finditer(source):
        href, inner_html = match.group(1), match.group(2)
        if not href.startswith("http"):
            continue
        if ROOT_LINK_RE.match(href):
            continue
        text = unescape(re.sub(r"<[^>]+>", "", inner_html)).strip().lower()
        candidates.append((text, href))

    for text, href in candidates:
        if any(p in text for p in ACTION_PHRASES):
            return href

    for text, href in candidates:
        if not any(p in text for p in SKIP_PHRASES):
            return href

    # Last resort: no <a href> tags at all (e.g. a plain-text-only email) -
    # fall back to the first bare URL in the body that isn't a root/homepage
    # link.
    for bare in BARE_URL_RE.finditer(source):
        href = bare.group(0).rstrip(').,;>')
        if not ROOT_LINK_RE.match(href):
            return href

    return None


# ---------------------------------------------------------------------------
# Thread deduplication
# ---------------------------------------------------------------------------
#
# eBay (and to a lesser extent Etsy) sends a brand new notification email
# every time either side adds to an ongoing conversation, so one real
# back-and-forth about a single item can show up as ten or more separate
# "sent a message about ..." / "Re: ... sent a message about ..." emails,
# all landing in "Messages to Reply To". Left alone, that turns a handful
# of conversations that actually need a reply into a wall of near-duplicate
# rows. This groups messages by conversation thread and keeps only the most
# recent email per thread.

# eBay's per-buyer message links carry a stable "qid" query param that's the
# same across every notification for one conversation thread - the most
# reliable dedup key when we have it.
QID_RE = re.compile(r'[?&]qid=([^&]+)')

def compute_thread_key(platform, subject, link):
    """
    Returns a key that's the same across every notification email for one
    ongoing conversation, so only the latest needs to be kept.
    """
    if link:
        m = QID_RE.search(link)
        if m:
            return (platform, "qid", m.group(1))

    # Fallback when there's no qid to key off of: pull out the other
    # party's name from the subject line itself.
    s = re.sub(r'^(re:\s*)+', '', subject, flags=re.I).strip()
    m = re.match(r'etsy conversation with\s+(.+?)(?:\s+about\b|$)', s, re.I)
    if m:
        return (platform, "buyer", m.group(1).strip().lower())
    m = re.match(r'(.+?)\s+sent a message about\b', s, re.I)
    if m:
        return (platform, "buyer", m.group(1).strip().lower())

    # Last resort: no recognizable pattern - treat the normalized subject
    # itself as the thread key (better to risk an occasional missed
    # duplicate than to accidentally merge two unrelated conversations).
    return (platform, "subject", s.lower())


def dedupe_latest_per_thread(entries):
    """
    entries: list of (thread_key, received_dt, payload) tuples.
    Returns payloads, one per distinct thread_key, keeping whichever entry
    has the latest received_dt, sorted newest-first.
    """
    latest = {}
    for thread_key, received_dt, payload in entries:
        current = latest.get(thread_key)
        if current is None or received_dt > current[0]:
            latest[thread_key] = (received_dt, payload)
    ordered = sorted(latest.values(), key=lambda pair: pair[0], reverse=True)
    return [payload for _received_dt, payload in ordered]


# ---------------------------------------------------------------------------
# Offer expiration parsing
# ---------------------------------------------------------------------------

def parse_received_date(date_str):
    """
    Best-effort parse of Mail's 'date received as string' output.
    Mail.app's AppleScript date-to-string format varies by macOS locale/version,
    so this may need adjusting once we see real output.
    """
    for fmt in ("%A, %B %d, %Y at %I:%M:%S %p", "%B %d, %Y at %I:%M:%S %p"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime.now()


def extract_expiration(source, received_dt):
    text = unescape(re.sub(r"<[^>]+>", " ", source))
    text = re.sub(r"\s+", " ", text)

    # Mercari: "Offer will expire by 08:50PM Jun 20, 2026 CDT"
    m = re.search(r"expire[s]? by\s+(\d{1,2}:\d{2}\s*[AP]M\s+\w+\s+\d{1,2},\s*\d{4})", text, re.I)
    if m:
        try:
            return datetime.strptime(m.group(1), "%I:%M%p %b %d, %Y")
        except ValueError:
            pass

    # eBay counteroffer: "Counter offer expires: Sep-26 20:53"
    m = re.search(r"expires:\s*([A-Za-z]{3}-\d{1,2}\s+\d{1,2}:\d{2})", text)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%b-%d %H:%M")
            return dt.replace(year=received_dt.year)
        except ValueError:
            pass

    # "expires in 18 hours" / "valid for 47 hours"
    m = re.search(r"(?:expires in|valid for)\s+(\d+)\s*hours?", text, re.I)
    if m:
        return received_dt + timedelta(hours=int(m.group(1)))

    # Fallback assumption when nothing explicit is stated
    return received_dt + timedelta(hours=24)


def format_time_left(expires_dt, now=None):
    now = now or datetime.now()
    total_minutes = int((expires_dt - now).total_seconds() // 60)
    if total_minutes <= 0:
        return "Expired"
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) + " left"


# ---------------------------------------------------------------------------
# HTML building
# ---------------------------------------------------------------------------

def esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

def build_row(msg_id, platform, subject, link, extra_html=""):
    href = esc(link) if link else "#"
    return f'''<div class="mkt-row" data-mkt-id="{esc(msg_id)}">
  <label class="mkt-checkbox"><input type="checkbox" class="mkt-dismiss" data-id="{esc(msg_id)}"></label>
  <span class="mkt-platform">{esc(platform)}</span>
  <a class="mkt-subject" href="{href}" target="_blank" rel="noopener">{esc(subject)}</a>
  {extra_html}
</div>'''

def build_section(rows):
    if not rows:
        return '<div class="mkt-empty">Nothing here right now.</div>'
    return "\n".join(rows)

def build_tile(count, label, css_class):
    return (f'<div class="dash-tile {css_class}"><div class="num">{count}</div>'
            f'<div class="label">{esc(label)}</div></div>')


def replace_between(html, start_marker, end_marker, new_inner):
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.S)
    replacement = f"{start_marker}\n{new_inner}\n{end_marker}"
    if not pattern.search(html):
        raise RuntimeError(f"Markers {start_marker} / {end_marker} not found in index.html")
    return pattern.sub(replacement, html)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def git(*args):
    return subprocess.run(["git", "-C", str(REPO_DIR), *args],
                           capture_output=True, text=True)


def push_with_retry(max_attempts=5):
    """
    bambu_refresh.py commits and pushes to this same repo on its own
    15-minute launchd schedule, and this script will eventually run on a
    schedule too - so a push landing at the same moment as the other
    script's push gets rejected ("fetch first") purely from the race, not
    from any real conflict. Since both scripts touch different, non-
    overlapping sections of index.html, pulling with --rebase and trying
    again resolves this automatically almost every time.
    """
    for attempt in range(1, max_attempts + 1):
        push = git("push")
        print(push.stdout, push.stderr)
        if push.returncode == 0:
            return True

        print(f"  push rejected (attempt {attempt}/{max_attempts}) - "
              f"pulling and retrying...", file=sys.stderr)
        pull = git("pull", "--rebase")
        print(pull.stdout, pull.stderr)
        if pull.returncode != 0:
            print("ERROR: git pull --rebase failed - resolve manually "
                  "in the launchpad folder", file=sys.stderr)
            return False

    print(f"ERROR: push still rejected after {max_attempts} attempts - "
          f"resolve manually in the launchpad folder", file=sys.stderr)
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what was found, don't touch index.html or git")
    parser.add_argument("--debug", action="store_true",
                         help="Also dump the first message's raw source per mailbox to "
                              "debug_<mailbox>.txt in the current directory, for inspecting "
                              "why link extraction did or didn't find a URL")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS,
                         help=f"Ignore messages older than this many days (default {MAX_AGE_DAYS})")
    args = parser.parse_args()

    order_rows, offer_rows, message_rows = [], [], []
    message_candidates = []  # (thread_key, received_dt, (id, platform, subject, link))
    offer_candidates = []    # (thread_key, received_dt, (id, platform, subject, link, countdown))

    for kind, mailbox_name in MAILBOXES.items():
        print(f"--- Reading '{mailbox_name}' ---")
        msgs = fetch_mailbox_messages(mailbox_name, max_age_days=args.max_age_days)
        print(f"  {len(msgs)} message(s) found (within last {args.max_age_days} days)")

        if args.debug and msgs:
            decoded_first = decode_email_body(msgs[0]["source"])
            debug_path = Path.cwd() / f"debug_{kind}.txt"
            debug_path.write_text(decoded_first)
            print(f"  (debug) wrote first message's decoded body to {debug_path}")

        for msg in msgs:
            body = decode_email_body(msg["source"])
            platform = detect_platform(msg["sender"], msg["subject"], body)
            link = extract_deep_link(body)
            received_dt = parse_received_date(msg["date_str"])
            print(f"  -> [{platform}] {msg['subject'][:70]!r} link={link}")

            if kind == "orders":
                order_rows.append(build_row(msg["id"], platform, msg["subject"], link))
            elif kind == "messages":
                thread_key = compute_thread_key(platform, msg["subject"], link)
                message_candidates.append(
                    (thread_key, received_dt, (msg["id"], platform, msg["subject"], link))
                )
            elif kind == "offers":
                expires_dt = extract_expiration(body, received_dt)
                countdown = format_time_left(expires_dt)
                print(f"     expires: {expires_dt}  ({countdown})")
                thread_key = compute_thread_key(platform, msg["subject"], link)
                offer_candidates.append(
                    (thread_key, received_dt, (msg["id"], platform, msg["subject"], link, countdown))
                )

    # Multiple notification emails for the same ongoing conversation / offer
    # negotiation collapse down to just the latest one - see
    # dedupe_latest_per_thread() for why.
    deduped_messages = dedupe_latest_per_thread(message_candidates)
    if len(deduped_messages) != len(message_candidates):
        print(f"\n(deduped 'Messages to Reply To': {len(message_candidates)} email(s) "
              f"-> {len(deduped_messages)} distinct conversation(s))")
    for msg_id, platform, subject, link in deduped_messages:
        message_rows.append(build_row(msg_id, platform, subject, link))

    deduped_offers = dedupe_latest_per_thread(offer_candidates)
    if len(deduped_offers) != len(offer_candidates):
        print(f"(deduped 'Offers to Consider': {len(offer_candidates)} email(s) "
              f"-> {len(deduped_offers)} distinct offer(s))")
    for msg_id, platform, subject, link, countdown in deduped_offers:
        extra = f'<span class="mkt-countdown">{esc(countdown)}</span>'
        offer_rows.append(build_row(msg_id, platform, subject, link, extra))

    if args.dry_run:
        print("\n=== Dry run complete - index.html and git were not touched ===")
        return

    if not INDEX_HTML.exists():
        print(f"ERROR: {INDEX_HTML} not found", file=sys.stderr)
        sys.exit(1)

    html = INDEX_HTML.read_text()
    html = replace_between(html, "<!--MKT_ORDERS_CARD_START-->", "<!--MKT_ORDERS_CARD_END-->", build_section(order_rows))
    html = replace_between(html, "<!--MKT_OFFERS_CARD_START-->", "<!--MKT_OFFERS_CARD_END-->", build_section(offer_rows))
    html = replace_between(html, "<!--MKT_MESSAGES_CARD_START-->", "<!--MKT_MESSAGES_CARD_END-->", build_section(message_rows))

    # "Today at a glance" tiles - same counts as the cards above (post-dedup
    # for offers/messages, so the tile number always matches the card).
    html = replace_between(html, "<!--MKT_ORDERS_TILE_START-->", "<!--MKT_ORDERS_TILE_END-->",
                            build_tile(len(order_rows), "Marketplace orders", "ship"))
    html = replace_between(html, "<!--MKT_OFFERS_TILE_START-->", "<!--MKT_OFFERS_TILE_END-->",
                            build_tile(len(deduped_offers), "Marketplace offers", "warn"))
    html = replace_between(html, "<!--MKT_MESSAGES_TILE_START-->", "<!--MKT_MESSAGES_TILE_END-->",
                            build_tile(len(deduped_messages), "Marketplace messages", "info"))

    INDEX_HTML.write_text(html)
    print("index.html updated.")

    git("pull", "--rebase")
    git("add", "index.html")
    commit = git("commit", "-m", "Update marketplace orders/offers/messages")
    print(commit.stdout, commit.stderr)
    if not push_with_retry():
        sys.exit(1)


if __name__ == "__main__":
    main()
