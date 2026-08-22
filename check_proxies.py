#!/usr/bin/env python3
"""
check_proxies.py - Test proxies for benzonavt.ru in bot working folder.

- Tests current proxies.txt (same folder as bot.py)
- Optionally fetches fresh RU candidates from free lists and tests them
- Writes only verified working proxies back to proxies.txt (or --output)

Usage:
  python check_proxies.py                 # test current proxies.txt
  python check_proxies.py --fetch         # also fetch ~200 fresh RU candidates and test
  python check_proxies.py --output working.txt --fetch
  python check_proxies.py --limit 100     # test only first 100
"""

import concurrent.futures
import argparse
import json
import os
import re
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
DEFAULT_PROXY_FILE = BASE_DIR / "proxies.txt"
TEST_URL_CITIES = "https://benzonavt.ru/api/v1/cities"
TEST_URL_NEAREST = "https://benzonavt.ru/api/v1/stations/nearest?lat=55.7558&lon=37.6176&radius_km=50&limit=5"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_proxies_file(path: Path):
    proxies = []
    if not path.is_file():
        return proxies
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxies.append(line)
    return proxies


def fetch_fresh_candidates():
    candidates = set()
    # RU fresh sources
    urls = [
        "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list/by-country/ru/http.txt",
        "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list/by-country/ru/socks5.txt",
        "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/RU/data.txt",
        "https://raw.githubusercontent.com/hproxy-com/free-proxy-list/main/by-country/RU.txt",
        "https://cdn.jsdelivr.net/gh/databay-labs/free-proxy-list/http.txt",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            if r.ok:
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "://" in line:
                        line = line.split("://", 1)[1]
                    candidates.add(line)
                print(f"[fetch] {url} -> total {len(candidates)}")
        except Exception as e:
            print(f"[fetch fail] {url}: {e}")

    # proxycompass RU table
    try:
        r = requests.get("https://proxycompass.com/free-proxies/europe/russia/", headers=HEADERS, timeout=15)
        ips = re.findall(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s*</td>\s*<td[^>]*>\s*(\d{2,5})", r.text)
        print(f"[fetch] proxycompass RU ips {len(ips)}")
        for ip, port in ips:
            candidates.add(f"{ip}:{port}")
    except Exception as e:
        print(f"[fetch fail] proxycompass: {e}")

    return list(candidates)


def test_proxy(proxy_str: str, timeout: int = 8):
    raw = proxy_str.strip()
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    else:
        scheme, rest = "http", raw
    url = f"{scheme}://{rest}"
    proxy = {"http": url, "https": url}
    try:
        start = time.time()
        r = requests.get(TEST_URL_CITIES, proxies=proxy, headers=HEADERS, timeout=timeout)
        elapsed = time.time() - start
        if r.status_code != 200:
            return (proxy_str, False, elapsed, f"cities {r.status_code}")
        # also verify nearest (more strict - ensures benzonavt API truly works)
        r2 = requests.get(TEST_URL_NEAREST, proxies=proxy, headers=HEADERS, timeout=timeout)
        elapsed2 = time.time() - start
        if r2.status_code == 200 and r2.json().get("items"):
            return (proxy_str, True, elapsed2, "both ok")
        elif r2.status_code == 200:
            return (proxy_str, True, elapsed2, "cities ok")
        else:
            return (proxy_str, False, elapsed2, f"nearest {r2.status_code}")
    except Exception as e:
        return (proxy_str, False, None, str(e)[:120])


def main():
    ap = argparse.ArgumentParser(description="Test benzonavt proxies in working folder")
    ap.add_argument("--input", default=str(DEFAULT_PROXY_FILE), help="Input proxies file (default: proxies.txt in bot folder)")
    ap.add_argument("--output", default=None, help="Output file for working proxies (default: overwrite input)")
    ap.add_argument("--fetch", action="store_true", help="Also fetch fresh RU candidates from free lists")
    ap.add_argument("--limit", type=int, default=None, help="Test only first N proxies")
    ap.add_argument("--timeout", type=int, default=8, help="Per-proxy timeout seconds")
    ap.add_argument("--workers", type=int, default=25, help="Concurrent workers")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output) if args.output else inp

    candidates = load_proxies_file(inp)
    print(f"Loaded {len(candidates)} proxies from {inp}")

    if args.fetch:
        fresh = fetch_fresh_candidates()
        # merge, dedupe keeping original order first
        seen = set(candidates)
        for p in fresh:
            if p not in seen:
                candidates.append(p)
                seen.add(p)
        print(f"After fetch total {len(candidates)}")

    if args.limit:
        candidates = candidates[: args.limit]
        print(f"Limited to {len(candidates)}")

    # expand 1080 -> also try socks5 variant
    expanded = []
    for p in candidates:
        expanded.append(p)
        if (":1080" in p or ":1082" in p or ":9050" in p) and not p.startswith("socks"):
            expanded.append(f"socks5://{p}")
    print(f"Testing {len(expanded)} proxies (with socks5 expansion) with {args.workers} workers, timeout={args.timeout}s ...")

    working = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(test_proxy, p, args.timeout): p for p in expanded}
        for fut in concurrent.futures.as_completed(futs):
            proxy_str, ok, elapsed, detail = fut.result()
            mark = "OK" if ok else "FAIL"
            el = f"{elapsed:.2f}s" if elapsed else "-"
            print(f"{mark:4} {proxy_str:28} {el:6} {detail}")
            if ok:
                working.append((proxy_str, elapsed))

    working_sorted = sorted(working, key=lambda x: x[1])
    print(f"\nWorking: {len(working_sorted)}/{len(expanded)}")
    for p, el in working_sorted:
        print(f"  {p}  ({el:.2f}s)")

    # backup original
    if out == inp and inp.is_file():
        bak = inp.with_suffix(".bak")
        try:
            bak.write_text(inp.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Backup saved to {bak}")
        except Exception:
            pass

    # write output
    with open(out, "w", encoding="utf-8") as f:
        f.write("# Verified working proxies for benzonavt.ru\n")
        f.write(f"# Tested {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} against {TEST_URL_CITIES} + nearest\n")
        f.write(f"# {len(working_sorted)}/{len(expanded)} passed (free proxies die fast - re-run with --fetch)\n")
        f.write("# Format: IP:PORT or scheme://IP:PORT\n#\n")
        for p, _ in working_sorted:
            f.write(p + "\n")

    print(f"Wrote {len(working_sorted)} working proxies to {out}")

    # also save json with timings next to bot
    json_path = BASE_DIR / "proxy_test.json"
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump([[p, el] for p, el in working_sorted], jf, indent=2)
    print(f"Timings saved to {json_path}")


if __name__ == "__main__":
    main()
