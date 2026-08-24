#!/usr/bin/env python3
"""
Runs LOCALLY on your Mac (not in GitHub Actions -- Bambu printers only
answer on your home network, and GitHub's cloud servers can't reach them).

Queries each of your Bambu Lab printers over local LAN/MQTT for its current
print status, then rewrites the "Printing now" tile + card in your local
clone of index.html between the PRINT_TILE_START/END and PRINT_CARD_START/END
HTML comment markers, and pushes the change to GitHub.
"""
import json
import os
import subprocess
import sys
import time
import re
import datetime
from pathlib import Path

REPO_PATH = os.environ.get("LAUNCHPAD_REPO_PATH", str(Path.home() / "launchpad"))
CONFIG_PATH = os.environ.get("LAUNCHPAD_BAMBU_CONFIG", str(Path.home() / ".launchpad-bambu.json"))
INDEX_PATH = os.path.join(REPO_PATH, "index.html")


def load_printers():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def query_printer(cfg):
    import bambulabs_api as bl

    name = cfg["name"]
    print(f"--- Checking {name} ({cfg['ip']}) ---", flush=True)
    try:
        printer = bl.Printer(cfg["ip"], cfg["access_code"], cfg["serial"])
        printer.connect()
        time.sleep(3)

        raw_state = str(printer.get_state() or "").upper()
        percentage = printer.get_percentage()
        minutes_left = printer.get_time()

        printer.disconnect()

        if "RUN" in raw_state or "PRINT" in raw_state:
            status = "printing"
        elif "PAUSE" in raw_state:
            status = "paused"
        elif "FAIL" in raw_state or "ERROR" in raw_state:
            status = "error"
        elif "IDLE" in raw_state or "FINISH" in raw_state or raw_state == "":
            status = "idle"
        else:
            status = "unknown"

        print(f"    -> {name}: status={status} raw_state={raw_state!r} pct={percentage} mins_left={minutes_left}", flush=True)

        return {
            "name": name,
            "status": status,
            "raw_state": raw_state,
            "percentage": percentage,
            "minutes_left": minutes_left,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001
        print(f"    -> {name}: FAILED - {type(e).__name__}: {e}", flush=True)
        return {"name": name, "status": "unreachable", "raw_state": None,
                "percentage": None, "minutes_left": None, "error": str(e)}


def replace_between(html, start_marker, end_marker, new_inner):
    pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    replacement = start_marker + new_inner + end_marker
    new_html, n = pattern.subn(replacement, html, count=1)
    if n != 1:
        raise RuntimeError(f"Marker pair {start_marker}...{end_marker} not found exactly once")
    return new_html


def build_tile(printing_count):
    return (
        f'<div class="dash-tile"><div class="num">{printing_count}</div>'
        '<div class="label">Printing now</div></div>'
    )


def build_row(p):
    if p["status"] == "unreachable":
        return f'    <div class="dash-empty">{p["name"]}: couldn\'t reach it on the network.</div>'
    if p["status"] == "printing":
        pct = f'{p["percentage"]}%' if p["percentage"] is not None else "printing"
        eta = f' &middot; {p["minutes_left"]} min left' if p["minutes_left"] is not None else ""
        return (
            '    <div class="dash-item">\n'
            '      <div class="dot2 ok"></div>\n'
            '      <div class="dash-item-body">\n'
            f'        <p class="dash-item-title">{p["name"]}</p>\n'
            f'        <p class="dash-item-meta">Printing &middot; {pct}{eta}</p>\n'
            "      </div>\n"
            "    </div>"
        )
    if p["status"] == "paused":
        return (
            '    <div class="dash-item">\n'
            '      <div class="dot2 warn"></div>\n'
            '      <div class="dash-item-body">\n'
            f'        <p class="dash-item-title">{p["name"]}</p>\n'
            '        <p class="dash-item-meta">Paused</p>\n'
            "      </div>\n"
            "    </div>"
        )
    if p["status"] == "error":
        return (
            '    <div class="dash-item">\n'
            '      <div class="dot2 warn"></div>\n'
            '      <div class="dash-item-body">\n'
            f'        <p class="dash-item-title">{p["name"]}</p>\n'
            '        <p class="dash-item-meta">Needs attention</p>\n'
            "      </div>\n"
            "    </div>"
        )
    return f'    <div class="dash-empty">{p["name"]}: idle.</div>'


def build_card(printers):
    rows = "\n".join(build_row(p) for p in printers)
    return (
        '\n  <div class="dash-section-label">Print queue &middot; Bambu Lab</div>\n'
        f'  <div class="dash-card">\n{rows}\n  </div>\n  '
    )


def git(*args):
    return subprocess.run(["git", "-C", REPO_PATH, *args], check=True, capture_output=True, text=True)


def main():
    printers = load_printers()
    results = [query_printer(p) for p in printers]

    printing_count = sum(1 for r in results if r["status"] == "printing")

    print("\n=== Summary ===")
    for r in results:
        print(f"{r['name']}: {r['status']} ({r['error'] or r['raw_state']})")
    print(f"Printing now: {printing_count}\n")

    git("pull", "--rebase")

    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    html = replace_between(html, "<!--PRINT_TILE_START-->", "<!--PRINT_TILE_END-->", build_tile(printing_count))
    html = replace_between(html, "<!--PRINT_CARD_START-->", "<!--PRINT_CARD_END-->", build_card(results))

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    git("add", "index.html")
    diff = subprocess.run(["git", "-C", REPO_PATH, "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        print("No change in printer status -- nothing to commit.")
        return

    git("commit", "-m", "Auto-refresh Bambu print queue")
    try:
        git("push")
    except subprocess.CalledProcessError:
        git("pull", "--rebase")
        git("push")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[{datetime.datetime.now()}] bambu_refresh.py failed: {e}", file=sys.stderr)
        sys.exit(1)
