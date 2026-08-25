# Benzonavt Bot — Crowdsourced Gas Station Reporter

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Benzonavt API](https://img.shields.io/badge/API-benzonavt.ru-orange)](https://benzonavt.ru)
[![Proxy Ready](https://img.shields.io/badge/proxy-rotating-lightgrey)](#proxy-management)

Randomly selects a gas station near a major Russian city and reports fuel availability (`yes`/`no`) to [benzonavt.ru](https://benzonavt.ru) — a crowdsourced map of petrol availability. Built for research / load-testing the map's aggregation logic. Verifies that reports actually propagate to `st`/`recent` despite heavy CDN caching.

> **Live-tested 2026-08-21:** `POST /api/v1/reports → 201 {"ok":true,"report_id":2847xxx}` and after 5 s cache-busted `GET /stations/{id}?t=...` shows `recent[0].id == report_id` and `st` flips `yes ↔ no`.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
  - [CLI Flags](#cli-flags)
  - [Stealth vs Verify](#stealth-vs-verify)
  - [Examples](#examples)
- [Proxy Management](#proxy-management)
- [Verification](#verification)
- [API Details](#api-details)
- [Troubleshooting](#troubleshooting)
- [Ethics & Disclaimer](#ethics--disclaimer)

---

## How It Works

```
1. Pick random city from RUSSIAN_CITIES (22 entries, bot.py:38)
2. GET /api/v1/stations/nearest?lat=&lon=&radius_km=100&limit=300
3. Random station from returned items
4. Decide status: 65% yes (with random fuels 92/95/98/100/dt/lpg/cng), 35% no
5. POST /api/v1/reports {station_id, status, fuel_grades, device_id}
6. (optional) Verify via POST /stations/{id}/live + GET ?t= cache-bust
7. Sleep COOLDOWN_SECONDS (300 s default), rotate proxy
   (+ with --threads N: steps 1-7 run independently in N threads, each with
      own ProxyPool/device_id, staggered start, [Bot-X] log prefix, bot.py:595)
```

* `device_id` is a fresh 32-char hex per cycle (`secrets.token_hex(16)`, `bot.py:91`), matching frontend `getDeviceId()` (`localStorage zaprav:did`).
* `User-Agent` rotated from 6 real browser strings (`bot.py:67`).
* `Accept: application/json` required — without it `benzonavt.ru` returns `403` via nginx.

---

## Features

- **Randomized reporting** — 65/35 `yes`/`no` split, random subset of station's `fuels`
- **Proxy rotation** — loads `proxies.txt`, tests via `GET /api/v1/cities`, fallback to direct
- **Retry logic** — on `SOCKS 10054 / ProxyError / ConnectTimeout` retries 3× with next proxy instead of failing cycle (`bot.py:349`)
- **Stealth / Verify toggle** — `--no-verify` (default, 2 requests) vs `--verify` (5 requests, polls map)
- **Multi-threaded** — `--threads N` spawns N independent bot loops (own `device_id`/`ProxyPool`, `threading.Thread`, `bot.py:559`) with `Ctrl+C` handling and `[Bot-X]` log prefix (`bot.py:86`)
- **Cache-aware verification** — busts CDN with `?t=timestamp` and checks `recent` array, not just icon color
- **Exact map links** — logs `https://benzonavt.ru/?lat=&lon=&zoom=14` for the *same* station hit, not a random one
- **City coverage** — Moscow, St. Petersburg, Kazan, etc. (some coordinates approximate - see troubleshooting)

---

## Project Structure

```
GAS/
├── bot.py              # Main loop, BenzoClient, ProxyPool, verify logic
├── proxies.txt         # 20 verified working proxies (2026-08-21, see header)
├── proxies.bak         # Backup of previous list
├── check_proxies.py    # Tester for proxies.txt (lives in bot folder)
├── verify_report.py    # Standalone before/after verifier for one station
├── proxy_test.json     # Latencies of last verified proxies
├── requirements.txt    # requests>=2.28.0 (+ pysocks for socks5://)
└── README.md           # This doc
```

---

## Installation

```bash
# 1. Clone / copy folder
cd GAS

# 2. Python 3.8+
python --version  # 3.13.1 tested

# 3. Install deps
pip install -r requirements.txt
# For socks5:// proxies:
pip install pysocks  # already 1.7.1 if you used check_proxies.py

# 4. (Optional) Fill proxies - already filled with 20 verified 2026-08-21
# To refresh:
python check_proxies.py --fetch
```

---

## Configuration

Edit constants at top of `bot.py`:

| Constant | Default | Description |
|---|---|---|
| `BASE_URL` | `https://benzonavt.ru` | API host |
| `COOLDOWN_SECONDS` | `300` | 5 min between reports |
| `FUEL_PROBABILITY` | `0.65` | `yes` chance |
| `MAX_STATIONS_FETCH` | `300` | API limit |
| `NEAREST_RADIUS_KM` | `100` | Search radius |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout |
| `RUSSIAN_CITIES` | 22 tuples | `(lat, lon, label)` — random pick per cycle |
| `ALL_FUEL_GRADES` | `92,95,98,100,dt,lpg,cng` | Valid fuels |

Fuel picker `random_fuel_grades()` (`bot.py:100`) samples from station's `fuels` or random if empty.

---

## Usage

### CLI Flags

```
usage: bot.py [-h] [--proxies PROXIES] [--iterations ITERATIONS]
              [--cooldown COOLDOWN] [--verify | --no-verify]
              [--fuel-prob FUEL_PROB] [--no-prob] [--threads THREADS]

options:
  --proxies PROXIES     Path to proxy list (default: proxies.txt)
  --iterations N        Max reports per thread (default: infinite)
  --cooldown SECONDS    Override 300 s (e.g. 10 for testing)
  --verify / --no-verify
                        Enable post-submit polling (extra GETs, detectable).
                        Default --no-verify (stealth: only POST /reports).
  --fuel-prob, --yes-prob, --fuel-probability FUEL_PROB
                        Probability of "yes" (has fuel) as 0.0-1.0 or 0-100.
                        Default 0.65 (65% yes). Use --no-prob for always "no".
  --threads, -t, --workers, -w THREADS
                        Number of independent bot threads (default: 1).
                        Each thread runs its own loop with own device_id/
                        ProxyPool and staggered start (0.2-0.8s + idx*0.15s).
                        Example: --threads 5 → 5 concurrent bots;
                        --threads 3 --iterations 10 → 30 total (10/thread).
                        Logs prefixed with [Bot-X] (bot.py:86).
```

*Implemented `bot.py:516` with `argparse` + `threading` (Python 3.9+ for `BooleanOptionalAction`). Logs mode at startup. `--threads` validated `>=1` (warns `>100`).*

### Stealth vs Verify

| Mode | Requests per cycle | Detectability | Use when |
|---|---|---|---|
| `--no-verify` (default) | 2: `GET nearest` + `POST reports` | **Low** - mimics single user tap | Production / stealth |
| `--verify` | 5: + `POST /live` + 2× `GET ?t=` polls | **Higher** - 3 extra cache-busted fetches | Debugging / proving `recent` updated |

Verification checks `recent[0].id == report_id` and logs `st.status/fuels_now/reports` delta. Without it you see stale CDN cache and think bot failed (see [Verification](#verification)).

### Examples

```bash
# Single stealth report, 1s cooldown, show map link
python bot.py --iterations 1 --cooldown 1 --no-verify

# Verify mode - see polling
python bot.py --iterations 1 --cooldown 1 --verify

# Infinite loop with private proxies
python bot.py --proxies my_private.txt --cooldown 300

# No proxies (direct IP, most reliable)
python bot.py --proxies "" --iterations 5 --no-verify

# Multi-threaded: 5 concurrent bots, each does 20 reports
python bot.py --threads 5 --iterations 20 --cooldown 60

# Short alias, 3 workers, stealth, 1s cooldown (load test)
python bot.py -t 3 --iterations 10 --cooldown 1 --no-verify
# Equivalent long form
python bot.py --workers 3 --iterations 10 --cooldown 1 --no-verify

# Threads + verify (more detectable, shows [Bot-X] prefix per thread)
python bot.py --threads 2 --iterations 1 --verify --cooldown 1

# Dry-run via verifier (no POST)
python verify_report.py --station-id 4717 --no-submit
python verify_report.py --station-id 4717 --status no   # force flip
python verify_report.py --lat 55.7558 --lon 37.6176      # random Moscow

# Test proxies in place (same folder)
python check_proxies.py                 # test current 20
python check_proxies.py --fetch --limit 50  # fetch fresh RU + test
```

---

## Proxy Management

`proxies.txt` format (`bot.py:136` `_format()`):

```
# Comments start with #
127.0.0.1:8080
http://1.2.3.4:3128
socks5://5.6.7.8:1080
http://user:pass@9.10.11.12:8080
socks5://user:pass@13.14.15.16:1080
```

* **Rotation:** `ProxyPool.next()` round-robin, `test()` via `GET /api/v1/cities` (8 s timeout). Failed proxies skipped, fallback to direct after 5 attempts (`bot.py:332`).
* **Current file:** 20 verified 2026-08-21 16:15 UTC — tested against both `/cities` and `/stations/nearest` (only 8% of free proxies pass). Fastest `199.7.149.90:3128` (1.24 s), slowest `103.172.23.210:1080` (15.7 s). Full latencies in `proxy_test.json`.
* **Free proxies die fast:** re-run `check_proxies.py --fetch` when you see `SOCKSHTTPSConnectionPool 10054` / `ProxyError`. Free list sources: `proxifly`, `hproxy-com`, `databay-labs`, `proxycompass` RU.
* **Paid RU proxies recommended** for stable stealth - Yandex.Cloud / Selectel RU IPs have lowest latency to `benzonavt.ru`.
* **Deps:** `socks5://` requires `pysocks` (`pip install pysocks`).

---

## Verification

**Why "map doesn't update" is usually false:**

1. **Wrong station checked** - Bot hits random city (e.g. `Tula 53.60,40.18` → station `5966` at `56.28,90.22`). If you watch Moscow, you'll never see it. Bot now logs `Map check: https://benzonavt.ru/?lat=&lon=` (`bot.py:376`).
2. **CDN cache** - Frontend `staleTime:15000` + CDN. `GET /stations/{id}` without `?t=` is stale. Verifier uses `bust_cache=True` (`bot.py:195`).
3. **Aggregation** - `st.status` is `confidence/reports/confirmations` aggregated. Submitting `yes` when already `yes` won't flip icon, but `recent` will contain `report_id`. Check `recent`, not color.

**How to prove it works:**

```bash
# Bot stealth run - note id from log
python bot.py --iterations 1 --no-verify
# [INFO] Selected station: Техас (id=5966) ...
# [INFO] Report submitted: {"report_id":2847593}

# Manual cache-busted check
curl "https://benzonavt.ru/api/v1/stations/5966?t=$(date +%s)000" | jq .recent[0]
# {"id":2847593,"status":"no",...} -> verified

# Or automated
python verify_report.py --station-id 5966 --status yes
# ... VERIFIED: report 2847593 appears in recent - bot WORKS
# Delta: no -> yes | [] -> ['95'] | reports 1 -> 2
```

Live helper `verify_report.py` does before/after `?t=` fetches and explains deltas. `check_proxies.py` lives in working folder for local proxy health.

---

## API Details

Relevant endpoints (reverse-engineered from `benzonavt.ru/_next/static/chunks/*.js`):

* `GET /api/v1/cities` — proxy health check
* `GET /api/v1/stations/nearest?lat=&lon=&radius_km=&limit=` — returns `{items:[{id, name, lat, lon, fuels, st:{status, fuels_now, updated_at, reports, confirmations}}]}`
* `GET /api/v1/stations/{id}` — detail with `recent:[{id, status, fuel_grades, created_at}]` — **cached, use `?t=`**
* `POST /api/v1/stations/{id}/live` — frontend pings before refetch (used in verify mode)
* `POST /api/v1/reports` — `{station_id, status: "yes"|"no", fuel_grades: [], device_id: hex32}` → `201 {"ok":true,"report_id":...}`

Example station `4717 РН-Москва`:

```json
{
  "id": 4717, "lat": 55.7463995, "lon": 37.640869,
  "st": {"status":"yes","fuels_now":["92","95"],"reports":6,"confirmations":3,"updated_at":"2026-08-21T14:55:40+00:00"},
  "recent": [{"id":2843268,"status":"yes","fuel_grades":["92","95"]}]
}
```

Submit payload (`bot.py:207`):

```json
{"station_id":4717,"status":"yes","fuel_grades":["92","95"],"device_id":"5503de98c33609d3a14eae661a2237aa"}
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `403 Forbidden` | Missing `User-Agent` / `Accept` | Bot sets both via `BenzoClient` (`bot.py:178`). Direct `requests` without headers fails. |
| `SOCKS 10054 / ConnectTimeout` | Dead free proxy | `bot.py:349` retries 3×; run `python check_proxies.py --fetch` or use ` --proxies ""` direct. |
| `No stations found` | Wrong coords (e.g. `Yekaterinburg 56.01,90.50` is Krasnoyarsk) | Check `RUSSIAN_CITIES` — some labels approximate. Use valid lat/lon or increase `radius_km`. |
| `Report 201 but st unchanged` | Stale cache / same status | Use `--verify` or `verify_report.py` with `?t=`; try opposite status to see flip. |
| `422 radius_km=200` | API limit exceeded | Keep `radius_km <=100`, `limit <=300`. |
| Unicode `charmap` error | Windows console | Bot does `sys.stdout.reconfigure(encoding="utf-8")` (`bot.py:20`). Run in UTF-8 terminal. |

**Proxy tester overwrote file:** `check_proxies.py --limit 5` overwrites `proxies.txt` with only 2 working - restore from `proxies.bak` or omit `--limit`.

---

## Ethics & Disclaimer

* This bot is for **research/testing** the aggregation logic of a crowdsourced crisis map. Mass false reports degrade trust for drivers searching for fuel.
* Use `--cooldown 300` (default) and **do not** flood. Respect `benzonavt.ru` ToS. Consider contributing accurate reports instead.
* Proxies are public free lists - no guarantee of privacy. Prefer direct or paid residential proxies for sensitive use.
* Authors not responsible for misuse or API bans.

---

## License

see `LICENSE` on the same repo

## Changelog

* **2026-08-25** - Added `--threads/-t/--workers/-w` multi-threaded mode (`bot.py:559`): N independent `threading.Thread` workers, own `ProxyPool`/`device_id`, staggered start, `[Bot-X]` log prefix, `Ctrl+C` graceful join.
* **2026-08-21** - Added `--verify/--no-verify` (stealth default), retry for `10054`, `GET ?t=` verification, `check_proxies.py` in working folder, 20 verified RU proxies, map links.
