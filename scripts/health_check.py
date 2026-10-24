#!/usr/bin/env python3
"""
EPG Health Check
----------------
Downloads the LIVE epg.xml.gz from the same URL TiviMate uses and checks it
is a complete guide with enough channels and enough hours of listings ahead.

Hourly updates normally keep ~22-25h of guide ahead. If that drops below
HEALTHY_HOURS_AHEAD (default 18h, i.e. roughly 5 missed updates), something
is wrong upstream and the health-check workflow tries to heal it.

Writes `healthy=true|false` and a one-line `summary` to $GITHUB_OUTPUT.
Always exits 0 — the workflow decides what to do with the result.
"""

import gzip
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_epg import validate  # noqa: E402

FEED_URL = os.environ["FEED_URL"]
HEALTHY_HOURS_AHEAD = float(os.environ.get("HEALTHY_HOURS_AHEAD", "18"))


def check():
    # Cache-buster so we see what the CDN serves now, not a cached copy
    url = f"{FEED_URL}?check={int(time.time())}"
    print(f"Checking live feed: {FEED_URL}")
    try:
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        raw_xml = gzip.decompress(response.content)
    except Exception as e:
        return [f"could not download/decompress live feed: {e}"]
    return validate(raw_xml, min_hours_ahead=HEALTHY_HOURS_AHEAD)


def main():
    problems = check()
    healthy = not problems
    summary = "healthy" if healthy else "; ".join(problems)
    print(f"Result: {'HEALTHY' if healthy else 'UNHEALTHY'} — {summary}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"healthy={'true' if healthy else 'false'}\n")
            f.write(f"summary={summary}\n")


if __name__ == "__main__":
    main()
