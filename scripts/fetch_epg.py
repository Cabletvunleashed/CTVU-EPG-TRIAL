#!/usr/bin/env python3
"""
EPG Auto-Updater for TiviMate
-----------------------------
Fetches the source EPG, checks it looks healthy, compresses it to
epg.xml.gz, and saves it so GitHub Actions can publish it to the repo.

The source is a rolling window (~24h ahead), so this runs hourly
(triggered by cron-job.org) to keep the guide topped up.

If the download fails or the guide looks broken, it retries 4 more times
(waiting 1, 2, 5, then 10 minutes);
if every attempt fails it exits with an error WITHOUT touching epg.xml.gz,
so the last good guide stays live.
"""

import os
import re
import sys
import time
import gzip
import datetime
import requests


# ── CONFIG ────────────────────────────────────────────────────────────────────
# URL is injected securely from GitHub Secrets — never hardcoded here
MYEPG_URL = os.environ.get("MYEPG_URL")

# Output filename — must match what update-epg.yml commits
OUTPUT_FILE = "epg.xml.gz"

# Sanity checks — a guide that fails these is not published
MIN_CHANNELS = int(os.environ.get("MIN_CHANNELS", "10000"))
MIN_HOURS_AHEAD = float(os.environ.get("MIN_HOURS_AHEAD", "12"))

# Retries with growing waits (1, 2, 5, 10 min) — rides out source outages
# of up to ~18 minutes instead of losing the whole hour
RETRY_WAITS = [int(x) for x in os.environ.get("RETRY_WAITS", "60,120,300,600").split(",")]
ATTEMPTS = len(RETRY_WAITS) + 1
# ──────────────────────────────────────────────────────────────────────────────

STOP_RE = re.compile(rb'stop="(\d{14})\s*([+-]\d{4})?"')


def fetch_epg(url):
    print("Fetching EPG from source...")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; EPG-Fetcher/1.0)"}

    response = requests.get(url, headers=headers, timeout=120)
    response.raise_for_status()

    content = response.content
    print(f"Received {len(content) / 1024 / 1024:.1f} MB from server")

    # If source is already gzipped, decompress first so we re-compress cleanly
    if content[:2] == b'\x1f\x8b':
        print("Source is gzipped — decompressing to raw XML first...")
        content = gzip.decompress(content)
        print(f"Decompressed size: {len(content) / 1024 / 1024:.1f} MB")

    return content


def latest_stop_utc(raw_xml):
    latest = None
    for stamp, tz in STOP_RE.findall(raw_xml):
        t = datetime.datetime.strptime(stamp.decode(), "%Y%m%d%H%M%S")
        if tz:
            sign = -1 if tz[:1] == b"-" else 1
            t -= sign * datetime.timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5]))
        if latest is None or t > latest:
            latest = t
    return latest


def validate(raw_xml, min_hours_ahead=MIN_HOURS_AHEAD):
    """Return a list of problems; empty list means the guide is OK to publish."""
    problems = []

    if b"<tv" not in raw_xml[:2000] or not raw_xml.rstrip().endswith(b"</tv>"):
        problems.append("file is not a complete XMLTV guide (missing <tv> or </tv>)")

    channels = raw_xml.count(b"<channel ")
    programmes = raw_xml.count(b"<programme ")
    print(f"Channels: {channels}  Programmes: {programmes}")
    if channels < MIN_CHANNELS:
        problems.append(f"only {channels} channels (minimum {MIN_CHANNELS})")

    latest = latest_stop_utc(raw_xml)
    if latest is None:
        problems.append("no programme times found")
    else:
        now = datetime.datetime.utcnow()
        hours_ahead = (latest - now).total_seconds() / 3600
        print(f"Guide runs until {latest:%Y-%m-%d %H:%M} UTC ({hours_ahead:.1f}h ahead)")
        if hours_ahead < min_hours_ahead:
            problems.append(f"guide only {hours_ahead:.1f}h ahead (minimum {min_hours_ahead}h)")

    return problems


def save_compressed(raw_bytes, path):
    print(f"Compressing and saving to {path}...")
    # mtime=0 keeps the output identical when the guide hasn't changed
    with open(path, "wb") as out, gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as f:
        f.write(raw_bytes)

    final_size = os.path.getsize(path) / 1024 / 1024
    print(f"Done! Final file size: {final_size:.1f} MB")


def main():
    if not MYEPG_URL:
        print("ERROR: MYEPG_URL secret is not set.")
        print("Go to your GitHub repo > Settings > Secrets > Actions > New secret")
        print("Name: MYEPG_URL  |  Value: your full EPG download URL")
        sys.exit(1)

    for attempt in range(1, ATTEMPTS + 1):
        print(f"── Attempt {attempt} of {ATTEMPTS} ──")
        try:
            raw_xml = fetch_epg(MYEPG_URL)
            problems = validate(raw_xml)
        except requests.exceptions.Timeout:
            problems = ["request timed out"]
        except requests.exceptions.HTTPError as e:
            problems = [f"HTTP error from source: {e} (check the MYEPG_URL secret is still valid)"]
        except Exception as e:
            problems = [f"unexpected failure: {e}"]

        if not problems:
            break

        print("Attempt failed:")
        for p in problems:
            print(f"  - {p}")
        if attempt < ATTEMPTS:
            wait = RETRY_WAITS[attempt - 1]
            print(f"Retrying in {wait}s...")
            time.sleep(wait)
    else:
        print(f"ERROR: All {ATTEMPTS} attempts failed — NOT publishing.")
        print("The previous epg.xml.gz stays live.")
        # Hand the last error to the workflow so the phone alert can show it
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            # Strip the secret source link (errors can echo all or part of it) and keep to one line
            last = "; ".join(problems)
            last = re.sub(r"https?://\S+", "<url>", last)
            last = re.sub(r"(url: )\S+", r"\1<url>", last)
            last = re.sub(r"(host=')[^']*", r"\1<host>", last)
            last = last.replace("\n", " ")[:300]
            with open(out, "a") as f:
                f.write(f"error={last}\n")
        sys.exit(1)

    save_compressed(raw_xml, OUTPUT_FILE)
    print("EPG update complete!")


if __name__ == "__main__":
    main()
