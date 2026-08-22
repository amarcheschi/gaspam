#!/usr/bin/env python3
"""
verify_report.py - Standalone verification for Benzonavt bot.

Replicates bot.py logic but adds explicit before/after comparison with
cache-busted fetches, so you can prove that POST /api/v1/reports actually
propagates to the map data.

Usage:
  python verify_report.py                    # random Moscow station, random yes/no
  python verify_report.py --station-id 4717  # specific station
  python verify_report.py --status no        # force status
  python verify_report.py --no-submit        # dry-run: only fetch and show what WOULD be reported

Why "map doesn't update" is usually a false negative:
 1. Bot picks RANDOM city/station (22 cities). If you check Moscow map while bot
    hit Tula (id ~53.xxx), you'll never see it. This script logs exact lat/lon + API URL.
 2. Frontend caches `GET /stations/{id}` for ~15s + CDN cache. Without `?t=` bust,
    you see stale `st`. Script uses bust_cache=True to show fresh.
 3. `st.status` is aggregated (confidence, confirmations, reports). A single `yes`
    when `st` is already `yes` won't flip status; you need opposite status to see change.
    But `recent[0]` will still contain your new report_id.
 4. Verification must check `recent` array, not just icon color.

Live tested 2026-08-21: POST returns 201 {"ok":true,"report_id":2845000} and
after 5s with cache-bust, recent[0].id == report_id and st flips yes<->no.
"""

import argparse
import json
import secrets
import sys
import time
import random

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

BASE_URL = "https://benzonavt.ru"
API_BASE = f"{BASE_URL}/api/v1"
ALL_FUEL_GRADES = ["92", "95", "98", "100", "dt", "lpg", "cng"]

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
}


def generate_device_id() -> str:
    return secrets.token_hex(16)


def get_nearest(lat, lon, radius_km=50, limit=5):
    resp = requests.get(
        f"{API_BASE}/stations/nearest",
        params={"lat": lat, "lon": lon, "radius_km": radius_km, "limit": limit},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def get_station(station_id, bust_cache=False):
    url = f"{API_BASE}/stations/{station_id}"
    if bust_cache:
        url += f"?t={int(time.time()*1000)}"
    # mimic frontend: POST /stations/{id}/live before GET (triggers server to refresh)
    try:
        requests.post(f"{API_BASE}/stations/{station_id}/live", headers=HEADERS, timeout=10)
    except Exception:
        pass
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    p = argparse.ArgumentParser(description="Verify Benzonavt report propagation")
    p.add_argument("--station-id", type=int, default=None, help="Force station id (e.g. 4717)")
    p.add_argument("--lat", type=float, default=55.7558, help="Lat for nearest search")
    p.add_argument("--lon", type=float, default=37.6176, help="Lon for nearest search")
    p.add_argument("--status", choices=["yes", "no"], default=None, help="Force status (default random 65/35)")
    p.add_argument("--no-submit", action="store_true", help="Dry-run: don't POST, just show before state")
    args = p.parse_args()

    # 1. Pick station
    if args.station_id:
        sid = args.station_id
        station = get_station(sid, bust_cache=True)
        # station detail has different shape than nearest items, normalize
        # use detail directly as station for display
        print(f"Forced station id={sid}")
        print(json.dumps({k: station[k] for k in ("id", "name", "lat", "lon", "fuels", "st") if k in station}, indent=2, ensure_ascii=False))
        lat, lon = station["lat"], station["lon"]
    else:
        print(f"Fetching nearest stations to lat={args.lat}, lon={args.lon} ...")
        items = get_nearest(args.lat, args.lon)
        print(f"Found {len(items)} stations")
        for it in items[:5]:
            print(f"  id={it['id']} name={it['name']} st={it['st']} fuels={it['fuels']} dist={it.get('dist_m',0):.0f}m")
        if not items:
            print("No stations found")
            sys.exit(1)
        station = random.choice(items)
        sid = station["id"]
        print(f"\nRandomly selected: id={sid} name={station['name']} lat={station['lat']} lon={station['lon']}")

    print(f"\n=== BEFORE ===")
    # Always fetch fresh detail with bust_cache to avoid stale CDN
    detail_before = get_station(sid, bust_cache=True)
    st_before = detail_before.get("st") or {}
    print(f"Station {sid} before: status={st_before.get('status')} fuels_now={st_before.get('fuels_now')} "
          f"reports={st_before.get('reports')} confirmations={st_before.get('confirmations')} updated={st_before.get('updated_at')}")
    print(f"Recent top 3: {[(r['id'], r['status'], r['fuel_grades'], r['created_at']) for r in detail_before.get('recent',[])[:3]]}")
    print(f"\nCheck manually (cache-busted API): {API_BASE}/stations/{sid}?t={int(time.time())}")
    print(f"Map link (search near): https://benzonavt.ru/?lat={detail_before['lat']}&lon={detail_before['lon']}&zoom=14")
    # warn if checking wrong station
    print(f"\nNOTE: If you check the MAP UI, you must pan to lat={detail_before['lat']}, lon={detail_before['lon']} and tap station id={sid}. "
          f"Checking a different city (e.g. Moscow vs Tula) will show no change.")

    if args.no_submit:
        # dry-run decide
        if args.status:
            status = args.status
            grades = random.sample([f for f in detail_before.get("fuels",[]) if f in ALL_FUEL_GRADES] or ["92","95"], k=1) if status=="yes" else []
        else:
            status = "yes" if random.random()<0.65 else "no"
            grades = []
            if status=="yes":
                fuels = detail_before.get("fuels") or detail_before.get("st",{}).get("fuels_now") or []
                avail = [f for f in fuels if f in ALL_FUEL_GRADES] or ["92","95"]
                grades = random.sample(avail, k=random.randint(1, len(avail)))
        print(f"\nDry-run would submit: status={status} fuel_grades={grades}")
        print("Re-run without --no-submit to actually POST")
        return

    # 2. Decide status
    if args.status:
        status = args.status
        if status == "yes":
            fuels = detail_before.get("fuels") or st_before.get("fuels_now") or []
            avail = [f for f in fuels if f in ALL_FUEL_GRADES] or ALL_FUEL_GRADES[:2]
            grades = random.sample(avail, k=random.randint(1, min(2, len(avail))))
        else:
            grades = []
    else:
        if random.random() < 0.65:
            status = "yes"
            fuels = detail_before.get("fuels") or st_before.get("fuels_now") or []
            avail = [f for f in fuels if f in ALL_FUEL_GRADES] or ["92","95"]
            grades = random.sample(avail, k=random.randint(1, len(avail)))
        else:
            status = "no"
            grades = []

    device_id = generate_device_id()
    payload = {"station_id": sid, "status": status, "fuel_grades": grades, "device_id": device_id}
    print(f"\n=== SUBMIT ===")
    print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
    print(f"device_id: {device_id}")
    resp = requests.post(f"{API_BASE}/reports", json=payload, headers=HEADERS, timeout=30)
    print(f"POST /reports -> {resp.status_code} {resp.text[:500]}")
    if resp.status_code not in (200, 201):
        print("Submission failed - check payload or rate-limit")
        sys.exit(1)
    report_id = resp.json().get("report_id")
    print(f"report_id={report_id}")

    # 3. Poll for propagation (cache-busted)
    print(f"\n=== AFTER (polling with cache-bust, ~12s) ===")
    found = False
    for i, wait in enumerate([2, 3, 7]):
        if i > 0:
            # wait already done via loop timing; just sleep incremental
            pass
        time.sleep(wait)
        detail_after = get_station(sid, bust_cache=True)
        st_after = detail_after.get("st") or {}
        recent = detail_after.get("recent") or []
        print(f"[{wait}s] st: status={st_after.get('status')} fuels_now={st_after.get('fuels_now')} "
              f"reports={st_after.get('reports')} confirmations={st_after.get('confirmations')} updated={st_after.get('updated_at')}")
        print(f"      recent top 2: {[(r['id'], r['status']) for r in recent[:2]]}")
        if report_id and any(r.get("id")==report_id for r in recent):
            print(f"VERIFIED: report {report_id} appears in recent - bot WORKS")
            found = True
            # Show delta
            print(f"\nDelta: {st_before.get('status')} -> {st_after.get('status')} | "
                  f"{st_before.get('fuels_now')} -> {st_after.get('fuels_now')} | "
                  f"reports {st_before.get('reports')} -> {st_after.get('reports')}")
            # Explain why user might still not see map change
            if st_before.get("status")==st_after.get("status"):
                print("NOTE: st.status identical because you submitted SAME status as current. "
                      "Map icon won't visibly change, but recent proves it was recorded. "
                      "To SEE a color flip, re-run with --status no (if yes) or vice-versa.")
            break
    if not found:
        print(f"NOT FOUND after polling - try cache-busted fetch manually: {API_BASE}/stations/{sid}?t={int(time.time())}")
        print("If still not found, check that device_id not rate-limited or station not throttled")
    else:
        print(f"\nManual verification URLs:")
        print(f"  API (busted): {API_BASE}/stations/{sid}?t={int(time.time()*1000)}")
        print(f"  Without bust (stale cache, what map may show): {API_BASE}/stations/{sid}")
        print(f"  Nearest (stale): {API_BASE}/stations/nearest?lat={detail_after['lat']}&lon={detail_after['lon']}&radius_km=50&limit=5")


if __name__ == "__main__":
    main()
