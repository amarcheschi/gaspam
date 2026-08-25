#!/usr/bin/env python3
"""
Benzonavt Bot - crowdsourced gas-station fuel-availability reporter.

Selects a random station, then reports fuel status with 65/35 probability
(yes/no), rotating proxies after each station and enforcing a 5-minute cooldown.
"""

import argparse
import json
import os
import random
import secrets
import sys
import threading
import time
from datetime import datetime, timezone

import requests

# Ensure UTF-8 output for Cyrillic station names on Windows
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- Configuration -----------------------------------------------------------

BASE_URL = "https://benzonavt.ru"
API_BASE = f"{BASE_URL}/api/v1"
COOLDOWN_SECONDS = 300          # 5 minutes between station reports
FUEL_PROBABILITY = 0.65         # 65% chance of "has fuel"
MAX_STATIONS_FETCH = 300        # max stations to fetch per request (API limit)
NEAREST_RADIUS_KM = 100         # radius for nearest-stations lookup (API limit)
REQUEST_TIMEOUT = 30            # HTTP timeout in seconds

# Major Russian cities - (lat, lon, label)
RUSSIAN_CITIES = [
    (55.7558, 37.6176, "Moscow"),
    (59.9343, 30.3351, "St. Petersburg"),
    (56.0105, 90.5002, "Yekaterinburg"),
    (55.7878, 49.1221, "Kazan"),
    (54.1929, 34.3934, "Tver"),
    (55.3523, 40.3933, "Vladimir"),
    (53.2968, 34.2068, "Bryansk"),
    (54.1058, 39.4756, "Voronezh"),
    (55.0435, 82.9249, "Omsk"),
    (55.5333, 47.2167, "Kaluga"),
    (46.8512, 51.8944, "Astrakhan"),
    (45.7372, 48.0307, "Volgograd"),
    (47.5118, 42.9858, "Rostov"),
    (55.1067, 38.8356, "Smolensk"),
    (47.2294, 39.7122, "Rostov-on-Don"),
    (53.6027, 40.1848, "Tula"),
    (48.7302, 44.5218, "Moscow Region South"),
    (43.9984, 52.3686, "Kaliningrad"),
    (44.8815, 38.0214, "Novorossiysk"),
    (46.5674, 48.0984, "Anapa"),
    (51.5075, 37.6176, "Moscow Region"),
    (54.5187, 38.1565, "Kursk"),
    (53.7118, 39.5847, "Lipetsk"),
    (52.6581, 39.5415, "Kursk Region"),
]

# Fuel grades available in Russia
ALL_FUEL_GRADES = ["92", "95", "98", "100", "dt", "lpg", "cng"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 "
    "Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0",
]


# --- Logging ----------------------------------------------------------------

def log(msg: str, level: str = "INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    thread = threading.current_thread().name
    # Prefix with thread name when running multi-threaded (not MainThread)
    prefix = f"[{thread}] " if thread != "MainThread" else ""
    print(f"[{ts}] [{level}] {prefix}{msg}", flush=True)


# --- Helpers ----------------------------------------------------------------

def generate_device_id() -> str:
    """Generate a 32-character lowercase hex device_id (128-bit)."""
    return secrets.token_hex(16)


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def random_fuel_grades(station_fuels: list) -> list:
    """Pick a random subset of fuel grades the station offers, at least one."""
    if not station_fuels:
        return random.sample(ALL_FUEL_GRADES, k=random.randint(1, 3))
    available = [f for f in station_fuels if f in ALL_FUEL_GRADES]
    if not available:
        return random.sample(ALL_FUEL_GRADES, k=random.randint(1, 3))
    count = random.randint(1, len(available))
    return random.sample(available, k=count)


# --- Proxy management --------------------------------------------------------

class ProxyPool:
    """Manages a rotating list of HTTP proxies."""

    def __init__(self, proxies: list = None):
        self._proxies = list(proxies) if proxies else []
        self._index = 0
        self._working = []

    @classmethod
    def from_file(cls, filepath: str) -> "ProxyPool":
        proxies = []
        if os.path.isfile(filepath):
            with open(filepath, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        proxies.append(line)
        log(f"Loaded {len(proxies)} proxies from {filepath}")
        return cls(proxies)

    def __len__(self):
        return len(self._proxies)

    def _format(self, raw: str) -> dict:
        """Convert raw proxy string to requests-ready dict."""
        raw = raw.strip()
        if "://" in raw:
            scheme, rest = raw.split("://", 1)
        else:
            scheme, rest = "http", raw
        if scheme in ("http", "https"):
            url = f"{scheme}://{rest}"
        elif scheme in ("socks4", "socks5"):
            url = f"{scheme}://{rest}"
        else:
            url = f"http://{rest}"
        return {"http": url, "https": url}

    def next(self) -> dict:
        """Return the next proxy dict, rotating. Returns None if no proxies."""
        if not self._proxies:
            return None
        proxy = self._proxies[self._index % len(self._proxies)]
        self._index += 1
        return self._format(proxy)

    def test(self, proxy: dict, timeout: float = 10) -> bool:
        """Test whether a proxy is reachable."""
        try:
            resp = requests.get(
                f"{BASE_URL}/api/v1/cities",
                proxies=proxy,
                timeout=timeout,
                headers={"User-Agent": random_user_agent()},
            )
            return resp.status_code == 200
        except Exception:
            return False


# --- API client --------------------------------------------------------------

class BenzoClient:
    """Client for the benzonavt.ru API."""

    def __init__(self, proxy: dict = None, device_id: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": random_user_agent(),
        })
        self.proxies = proxy
        self.device_id = device_id or generate_device_id()

    def _request(self, method: str, path: str, **kwargs):
        url = f"{API_BASE}{path}"
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        if self.proxies:
            kwargs["proxies"] = self.proxies
        resp = self.session.request(method, url, **kwargs)
        return resp

    def get_nearest_stations(self, lat: float, lon: float,
                              radius_km: float = NEAREST_RADIUS_KM,
                              limit: int = MAX_STATIONS_FETCH) -> list:
        """Fetch nearby gas stations via /api/v1/stations/nearest."""
        qs = f"lat={lat}&lon={lon}&radius_km={radius_km}&limit={limit}"
        resp = self._request("GET", f"/stations/nearest?{qs}")
        if resp.status_code != 200:
            log(f"Station fetch returned {resp.status_code} - {resp.text[:300]}", "WARN")
            return []
        data = resp.json()
        return data.get("items", [])

    def get_station(self, station_id: int, bust_cache: bool = False) -> dict:
        """Fetch single station detail via /api/v1/stations/{id}.

        Use bust_cache=True to bypass CDN/server cache (adds ?t= timestamp).
        Frontend does POST /stations/{id}/live before GET; we mimic cache-bust.
        """
        path = f"/stations/{station_id}"
        if bust_cache:
            import time as _time
            path += f"?t={int(_time.time()*1000)}"
        resp = self._request("GET", path)
        if resp.status_code != 200:
            log(f"Station {station_id} fetch returned {resp.status_code} - {resp.text[:200]}", "WARN")
            return None
        return resp.json()

    def submit_report(self, station_id: int, status: str,
                      fuel_grades: list) -> dict:
        """Submit a fuel-availability report to /api/v1/reports.

        Args:
            station_id: The numeric station ID.
            status: "yes" (has fuel) or "no" (no fuel).
            fuel_grades: List of fuel grade strings (e.g. ["92","95"]).
                        Empty list for "no" status.

        Returns:
            The JSON response dict, or None on failure.
        """
        payload = {
            "station_id": station_id,
            "status": status,
            "fuel_grades": fuel_grades,
            "device_id": self.device_id,
        }
        resp = self._request(
            "POST", "/reports",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        # API returns 201 on creation; accept 200 as well for forward-compat
        if resp.status_code in (200, 201):
            try:
                return resp.json()
            except Exception:
                return {"ok": True, "raw": resp.text}
        log(f"Report submission failed: {resp.status_code} - {resp.text[:300]}", "WARN")
        return None


# --- Bot logic ---------------------------------------------------------------

def select_random_station(stations: list) -> dict:
    """Pick a random station from the list."""
    if not stations:
        return None
    return random.choice(stations)


def decide_fuel_status(station: dict, fuel_probability: float = None) -> tuple:
    """With fuel_probability report 'yes' (has fuel), else 'no' (no fuel)."""
    prob = fuel_probability if fuel_probability is not None else FUEL_PROBABILITY
    if random.random() < prob:
        st = station.get("st") or {}
        grades = random_fuel_grades(
            station.get("fuels", st.get("fuels_now", []))
        )
        return "yes", grades
    return "no", []


def verify_submission(client: BenzoClient, station_id: int, report_id: int, timeout_sec: int = 12) -> bool:
    """Verify that a submitted report actually appears in station data.

    The map UI caches aggressively (staleTime 15s + CDN). Without cache-bust,
    GET /stations/{id} returns stale `st`. We fetch with ?t= timestamp and
    check `recent` array for our report_id, and log `st` delta.

    Returns True if report found in recent after polling.
    """
    import time as _time
    for wait in (2, 5, timeout_sec):
        _time.sleep(wait if wait <= 3 else wait - 5 if wait == 5 else 2)
        # mimic frontend: POST /stations/{id}/live before invalidation
        try:
            client._request("POST", f"/stations/{station_id}/live")
        except Exception:
            pass
        detail = client.get_station(station_id, bust_cache=True)
        if not detail:
            continue
        st = detail.get("st") or {}
        recent = detail.get("recent") or []
        ids = [r.get("id") for r in recent[:5]]
        log(f"Verify poll: st.status={st.get('status')} fuels_now={st.get('fuels_now')} "
            f"reports={st.get('reports')} confirmations={st.get('confirmations')} "
            f"updated_at={st.get('updated_at')} | recent top ids={ids}")
        if report_id in ids:
            log(f"VERIFIED: report {report_id} found in recent (visible on map after cache expiry)", "INFO")
            return True
        # also consider recent may contain new id larger than report_id due to race
        if recent and recent[0].get("id") and recent[0].get("id") >= report_id:
            # if top id >= ours, ours may have been pushed down but still present
            if any(r.get("id") == report_id for r in recent):
                log(f"VERIFIED: report {report_id} found deeper in recent list")
                return True
    log(f"Verification: report {report_id} NOT found in recent after {timeout_sec}s - "
        f"check https://benzonavt.ru/api/v1/stations/{station_id}?t=<ts> manually "
        f"or use verify_report.py", "WARN")
    return False


def one_cycle(pool: ProxyPool, verify: bool = False, fuel_probability: float = None) -> bool:
    """Execute one full bot cycle: fetch station, submit report.

    Args:
        pool: ProxyPool instance or None
        verify: if True, poll verification (stealth=False)
        fuel_probability: override for FUEL_PROBABILITY (0.0-1.0), None uses default

    Returns True on success, False on failure.
    """
    # Pick a random city
    city = random.choice(RUSSIAN_CITIES)
    lat, lon, label = city
    log(f"Selected city: {label} (lat={lat}, lon={lon})")

    # Get a working proxy (handle pool=None or empty)
    proxy = None
    if pool is None or len(pool) == 0:
        log("No proxies configured, using direct connection.")
    else:
        attempts = 0
        while attempts < 5:
            proxy = pool.next()
            if proxy is None:
                log("No proxies configured, using direct connection.")
                break
            if pool.test(proxy):
                log("Proxy verified as working.")
                break
            attempts += 1
            log(f"Proxy failed, trying next (attempt {attempts}).", "WARN")
            if attempts >= 5:
                log("All proxy attempts failed, falling back to direct.", "WARN")
                proxy = None
                break

    # Create client with proxy (retry with next proxy on network error)
    # Free proxies are volatile: WinError 10054 = remote forcibly closed, SOCKS timeout = dead proxy
    # If the chosen proxy dies on the actual API call (not just test), retry with next one instead of failing cycle
    max_fetch_retries = 3 if pool and len(pool) > 0 else 1
    stations = None
    last_err = None
    for fetch_attempt in range(max_fetch_retries):
        client = BenzoClient(proxy=proxy, device_id=generate_device_id())
        if fetch_attempt == 0:
            log(f"Using device_id: {client.device_id}")
        else:
            log(f"Retry {fetch_attempt}: new device_id {client.device_id} with next proxy", "WARN")
        try:
            stations = client.get_nearest_stations(lat, lon)
            last_err = None
            break  # success (even if empty list, don't retry - city may genuinely have no stations)
        except Exception as e:
            last_err = e
            # Classify as proxy error (SOCKS, timeout, 10054) vs real bug
            err_str = str(e)
            is_proxy_err = any(k in err_str for k in ("SOCKS", "ProxyError", "ConnectTimeout", "NewConnectionError", "10054", "10060", "timed out"))
            log(f"Fetch failed via proxy {proxy}: {e} (proxy_err={is_proxy_err})", "WARN")
            if is_proxy_err and pool and len(pool) > 0 and fetch_attempt + 1 < max_fetch_retries:
                # try next proxy (re-test loop)
                next_proxy = None
                for _ in range(5):
                    cand = pool.next()
                    if cand is None:
                        next_proxy = None
                        break
                    if pool.test(cand):
                        next_proxy = cand
                        log("Retrying with next working proxy.")
                        break
                    log("Next proxy also failed test, trying another.", "WARN")
                proxy = next_proxy
                if proxy is None:
                    log("No more working proxies, falling back to direct.", "WARN")
                continue
            else:
                # non-proxy error or no proxies left, propagate
                break

    if last_err is not None:
        # re-raise to be caught by run_bot's outer handler if all retries exhausted
        raise last_err

    if not stations:
        log("No stations found for this location.", "WARN")
        return None

    log(f"Found {len(stations)} stations nearby.")

    # Select a random station
    station = select_random_station(stations)
    if station is None:
        log("Failed to select a station.", "ERROR")
        return None

    sid = station.get("id")
    sname = station.get("name", "?")
    slat = station.get("lat")
    slon = station.get("lon")
    # Log station with direct map and API links for manual verification
    # User asked: "i do not see status being updated - am I checking wrong station on map?"
    # Provide exact URLs to check the SAME station the bot hit, not a random one on map.
    log(f"Selected station: {sname} (id={sid}) at lat={slat}, lon={slon}")
    if slat and slon:
        log(f"  Map check: https://benzonavt.ru/?lat={slat}&lon={slon}&zoom=14  (station id {sid} nearby)")
    log(f"  API check (cache-busted): {API_BASE}/stations/{sid}?t=<timestamp>  (use verify_report.py)")
    # Log before-state for diff
    before_st = station.get("st") or {}
    log(f"  Before st: status={before_st.get('status')} fuels_now={before_st.get('fuels_now')} "
        f"updated_at={before_st.get('updated_at')} reports={before_st.get('reports')}")

    # Decide fuel status (configurable via --fuel-prob)
    prob = fuel_probability if fuel_probability is not None else FUEL_PROBABILITY
    status, grades = decide_fuel_status(station, fuel_probability=prob)
    log(f"Reporting status: {status} | fuel_grades: {grades} (p_yes={prob:.2f})")

    # Submit report
    result = client.submit_report(sid, status, grades)
    if result:
        log(f"Report submitted successfully: {json.dumps(result, ensure_ascii=False)}")
        report_id = result.get("report_id")
        if verify and report_id:
            # Verify that report actually propagated to station's recent list
            # Without this, user sees stale cache and thinks bot is broken (aggregation delay ~5-30s)
            verify_submission(client, sid, report_id)
        elif verify:
            # No report_id but ok=true - still try to fetch fresh detail to show new st
            detail = client.get_station(sid, bust_cache=True)
            if detail:
                log(f"Post-submit fresh st: {detail.get('st')}")
        return True
    else:
        log("Report submission failed.", "ERROR")
        return False


def run_bot(proxies_file: str = None, max_iterations: int = None,
            cooldown_override: float = None, verify: bool = False,
            fuel_probability: float = None):
    """Run the bot loop.

    Args:
        proxies_file: Path to file with proxy list (one per line).
        max_iterations: If set, stop after this many station reports.
        cooldown_override: Override the cooldown in seconds.
        verify: If True, poll station detail with cache-bust to confirm propagation
                (extra POST /live + GETs - more detectable). Default False for stealth.
        fuel_probability: Override FUEL_PROBABILITY (0.0-1.0). None uses default 0.65.
    """
    pool = None
    if proxies_file:
        pool = ProxyPool.from_file(proxies_file)

    cooldown = cooldown_override if cooldown_override is not None else COOLDOWN_SECONDS
    fuel_prob = fuel_probability if fuel_probability is not None else FUEL_PROBABILITY
    # normalize if user passed 0-100 instead of 0-1
    if fuel_prob > 1.0 and fuel_prob <= 100:
        fuel_prob = fuel_prob / 100.0
        log(f"Fuel prob {fuel_probability} interpreted as {fuel_prob:.2f} (0-100 -> 0-1)", "WARN")
    if not (0.0 <= fuel_prob <= 1.0):
        log(f"Invalid fuel probability {fuel_prob} - must be 0.0-1.0, clamping.", "WARN")
        fuel_prob = max(0.0, min(1.0, fuel_prob))

    if not pool or len(pool) == 0:
        log("No proxies configured - running without proxy rotation.")
    else:
        log(f"Proxy rotation enabled with {len(pool)} proxies.")

    log(f"Cooldown set to {cooldown} seconds ({cooldown / 60:.1f} minutes).")
    log(f"Fuel probability: {fuel_prob*100:.0f}% (has fuel) / "
        f"{(1-fuel_prob)*100:.0f}% (no fuel)  (use --fuel-prob to alter)")
    log("Starting bot loop...")

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        log(f"--- Iteration {iteration} ---")

        try:
            result = one_cycle(pool, verify=verify, fuel_probability=fuel_prob)
            log(f"Cycle result: {'SUCCESS' if result else 'FAILED - ' + str(result)}")
        except Exception as e:
            log(f"Cycle failed with error: {e}", "ERROR")

        # Cooldown
        if max_iterations is None or iteration < max_iterations:
            log(f"Waiting {cooldown} seconds (cooldown)...")
            time.sleep(cooldown)


# --- CLI ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Benzonavt fuel-availability report bot."
    )
    parser.add_argument(
        "--proxies", default="proxies.txt",
        help="Path to proxy list file (one proxy per line).",
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Max number of station reports to submit (default: infinite).",
    )
    parser.add_argument(
        "--cooldown", type=float, default=None,
        help="Cooldown in seconds between reports (default: 300).",
    )
    # Boolean flag --verify / --no-verify (default: --no-verify for stealth)
    # When disabled (default), only POST /reports is sent. When enabled, does
    # extra POST /stations/{id}/live + GET ?t= cache-busted polls to confirm map update.
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable post-submit verification polling (extra GETs, more suspicious). "
             "Default is --no-verify (stealth: only POST /reports). Use --verify to confirm propagation.",
    )
    parser.add_argument(
        "--fuel-prob",
        "--yes-prob",
        "--fuel-probability",
        dest="fuel_prob",
        type=float,
        default=None,
        help="Probability of reporting 'yes' (has fuel) as 0.0-1.0 or 0-100. "
             "Default 0.65 (65%% yes / 35%% no). Examples: --fuel-prob 0.8 (80%% yes), --fuel-prob 30 (30%% yes).",
    )
    parser.add_argument(
        "--no-prob",
        dest="fuel_prob",
        action="store_const",
        const=0.0,
        help="Shortcut for --fuel-prob 0 (always report 'no' / no fuel).",
    )
    parser.add_argument(
        "--threads", "-t",
        "--workers", "-w",
        dest="threads",
        type=int,
        default=1,
        help="Number of independent bot threads to spawn (default: 1). "
             "Each thread runs its own loop with own device_id/proxy rotation. "
             "Example: --threads 5 spawns 5 concurrent bots; --threads 3 --iterations 10 = 30 total reports (10 per thread).",
    )
    args = parser.parse_args()

    if args.threads < 1:
        parser.error("--threads must be >= 1")
    if args.threads > 100:
        log(f"Large thread count {args.threads} - ensure system/proxy pool can handle it.", "WARN")

    if args.verify:
        log("Verification ENABLED: will poll station detail after each report (more requests, detectable).")
    else:
        log("Verification DISABLED (stealth): only POST /reports will be sent (less suspicious). Use --verify to enable checks.")

    # Single-thread: preserve original behaviour (main thread runs the loop)
    if args.threads == 1:
        run_bot(
            proxies_file=args.proxies,
            max_iterations=args.iterations,
            cooldown_override=args.cooldown,
            verify=args.verify,
            fuel_probability=args.fuel_prob,
        )
        return

    # Multi-thread: spawn N independent workers
    log(f"Spawning {args.threads} independent bot threads...")

    def _worker(idx: int):
        # Each thread gets its own run_bot invocation (own ProxyPool, device_id, etc.)
        # Small stagger to avoid thundering herd on first request
        if idx > 0:
            time.sleep(random.uniform(0.2, 0.8) + idx * 0.15)
        run_bot(
            proxies_file=args.proxies,
            max_iterations=args.iterations,
            cooldown_override=args.cooldown,
            verify=args.verify,
            fuel_probability=args.fuel_prob,
        )

    threads: list[threading.Thread] = []
    for i in range(args.threads):
        t = threading.Thread(target=_worker, args=(i,), name=f"Bot-{i+1}", daemon=False)
        t.start()
        threads.append(t)
        log(f"Thread Bot-{i+1} started (tid={t.ident})")

    # Wait for all threads; handle Ctrl+C gracefully
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log("Interrupted - waiting for threads to exit...", "WARN")
        # Threads are non-daemon so they will finish current iteration; user can Ctrl+C again to force
        for t in threads:
            t.join(timeout=1)
    log("All threads finished.")


if __name__ == "__main__":
    main()
