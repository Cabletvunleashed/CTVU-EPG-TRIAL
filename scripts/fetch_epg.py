#!/usr/bin/env python3
"""
EPG Auto-Updater for IPTV Smarters Pro
---------------------------------------
Fetches your myepg.top EPG, compresses it to epg.xml.gz,
and saves it so GitHub Actions can commit it to your repo.

Your Smarters URL after setup:
https://raw.githubusercontent.com/YOUR_USERNAME/epg-auto/main/epg.xml.gz
"""

import os
import sys
import gzip
import requests


# ── CONFIG ────────────────────────────────────────────────────────────────────
# URL is injected securely from GitHub Secrets — never hardcoded here
MYEPG_URL = os.environ.get("MYEPG_URL")

# Output filename — must match what update-epg.yml commits
OUTPUT_FILE = "epg.xml.gz"
# ──────────────────────────────────────────────────────────────────────────────


def fetch_epg(url):
    print("Fetching EPG from myepg.top...")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; EPG-Fetcher/1.0)"}

    response = requests.get(url, headers=headers, timeout=120, stream=True)
    response.raise_for_status()

    content = response.content
    print(f"Received {len(content) / 1024 / 1024:.1f} MB from server")

    # If source is already gzipped, decompress first so we re-compress cleanly
    if content[:2] == b'\x1f\x8b':
        print("Source is gzipped — decompressing to raw XML first...")
        content = gzip.decompress(content)
        print(f"Decompressed size: {len(content) / 1024 / 1024:.1f} MB")

    return content


def save_compressed(raw_bytes, path):
    print(f"Compressing and saving to {path}...")
    with gzip.open(path, "wb") as f:
        f.write(raw_bytes)

    final_size = os.path.getsize(path) / 1024 / 1024
    print(f"Done! Final file size: {final_size:.1f} MB")


def main():
    if not MYEPG_URL:
        print("ERROR: MYEPG_URL secret is not set.")
        print("Go to your GitHub repo > Settings > Secrets > Actions > New secret")
        print("Name: MYEPG_URL  |  Value: your full myepg.top download URL")
        sys.exit(1)

    try:
        raw_xml = fetch_epg(MYEPG_URL)
        save_compressed(raw_xml, OUTPUT_FILE)
        print("EPG update complete!")
    except requests.exceptions.Timeout:
        print("ERROR: Request timed out. myepg.top may be slow — will retry tomorrow.")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"ERROR: HTTP error from myepg.top: {e}")
        print("Check your MYEPG_URL secret is still valid.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Unexpected failure: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
