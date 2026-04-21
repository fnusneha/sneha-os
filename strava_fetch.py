"""
Fetch all cycling rides from Strava and cache as JSON.

Usage:
    python strava_fetch.py          # fetch all rides
    python strava_fetch.py --force  # ignore token cache, re-auth
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import polyline as pl
import requests
from dotenv import load_dotenv

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_FILE = SCRIPT_DIR / "rides_cache.json"
TOKEN_CACHE = SCRIPT_DIR / ".strava_token_cache.json"

CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN")

# ── Region bounding boxes (no external API needed) ──────────────────
REGIONS = [
    # Order matters — more specific regions first
    ("Marin",            37.8, 38.1, -122.8, -122.4),
    ("Peninsula",        37.2, 37.6, -122.5, -121.9),
    ("East Bay",         37.6, 38.0, -122.3, -121.7),
    ("Wine Country",     38.0, 38.9, -123.1, -122.2),
    ("Bay Area",         37.2, 38.0, -122.6, -121.7),
    ("Santa Cruz",       36.8, 37.1, -122.2, -121.8),
    ("Monterey",         36.4, 36.8, -122.0, -121.5),
    ("Sierra Foothills", 38.5, 39.2, -121.5, -120.5),
    ("Lake Tahoe",       38.8, 39.4, -120.2, -119.8),
    ("Utah",             36.5, 41.5, -114.5, -109.0),
    # Travel destinations
    ("Hawaii",           19.0, 22.5, -160.0, -154.0),
    ("Santa Barbara",    34.3, 34.8, -120.5, -119.5),
    ("Paso Robles",      35.4, 35.8, -121.0, -120.4),
    ("Sacramento",       38.4, 38.7, -121.9, -121.3),
    ("Palm Springs",     33.5, 34.0, -116.8, -116.3),
    ("Banff",            50.5, 52.0, -116.5, -114.5),
    ("Humboldt",         40.3, 41.5, -124.5, -123.5),
    ("Puerto Vallarta",  20.5, 21.0, -105.5, -105.0),
    ("Florence",         43.5, 44.0, 11.0, 11.5),
    ("Buenos Aires",     -35.0, -34.0, -59.0, -58.0),
    ("Lima",             -12.5, -11.5, -77.5, -76.5),
]


def _classify_region(latlng: list | None) -> str:
    if not latlng or len(latlng) < 2:
        return "Other"
    lat, lng = latlng[0], latlng[1]
    for name, lat_lo, lat_hi, lng_lo, lng_hi in REGIONS:
        if lat_lo <= lat <= lat_hi and lng_lo <= lng <= lng_hi:
            return name
    return "Other"


# ── Smart ride naming ───────────────────────────────────────────────

_GENERIC_NAMES = {"Morning Ride", "Afternoon Ride", "Lunch Ride", "Evening Ride", "Night Ride"}

# Landmark detection from start_latlng (approximate)
_LANDMARKS = [
    (37.434, -122.170, "Stanford Loop"),
    (37.370, -122.080, "Cupertino Hills"),
    (37.558, -122.271, "Bay Trail"),
    (37.757, -122.437, "SF Urban"),
    (37.871, -122.260, "Berkeley Hills"),
    (37.901, -122.518, "Marin Headlands"),
    (37.984, -122.572, "Mt Tam"),
    (38.045, -122.805, "Point Reyes"),
    (38.040, -122.910, "Marshall Wall"),
    (38.330, -122.460, "Napa Valley"),
    (38.440, -122.710, "Healdsburg"),
    (38.510, -122.815, "Dry Creek"),
    (38.290, -122.290, "Solano Hills"),
    (37.330, -121.890, "South Bay"),
    (37.233, -121.640, "Mt Hamilton"),
    (39.100, -120.030, "Tahoe"),
    (36.600, -121.900, "Monterey"),
    (38.900, -121.100, "Auburn"),
    (38.750, -120.900, "Gold Country"),
    (37.490, -122.180, "Palo Alto"),
    (37.540, -122.340, "San Mateo"),
    (37.620, -122.080, "Fremont Hills"),
    (37.830, -122.150, "Moraga"),
    (37.780, -122.380, "Embarcadero"),
    (36.965, -122.027, "Santa Cruz"),
    (21.276, -157.823, "Waikiki"),
    (34.445, -119.736, "Santa Barbara"),
    (34.616, -120.189, "Santa Ynez"),
    (35.616, -120.692, "Paso Robles"),
    (35.586, -120.699, "Paso Robles"),
    (38.548, -121.760, "Sacramento"),
    (33.824, -116.539, "Palm Springs"),
    (33.815, -116.548, "Palm Springs"),
    (51.177, -115.571, "Banff"),
    (40.576, -124.264, "Humboldt Coast"),
    (40.869, -124.081, "Eureka"),
    (41.363, -124.024, "Crescent City"),
    (20.752, -105.328, "Puerto Vallarta"),
    (34.595, -120.145, "Santa Ynez"),
    (43.775, 11.254, "Florence"),
    (-34.603, -58.378, "Buenos Aires"),
    (-12.117, -77.032, "Lima"),
    (-12.116, -77.030, "Lima"),
    (-12.121, -77.041, "Lima"),
]


def _smart_name(original: str, region: str, distance: float, elevation: int,
                start_latlng: list | None, coords: list) -> str:
    """Generate a descriptive name for generic Strava rides."""
    if original not in _GENERIC_NAMES:
        return original

    # Try landmark matching from start point
    landmark = None
    if start_latlng and len(start_latlng) >= 2:
        lat, lng = start_latlng[0], start_latlng[1]
        best_dist = 0.15  # ~10 mile threshold in degrees
        for lm_lat, lm_lng, lm_name in _LANDMARKS:
            d = ((lat - lm_lat) ** 2 + (lng - lm_lng) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                landmark = lm_name

    # Determine ride character
    is_loop = False
    if coords and len(coords) >= 2:
        start = coords[0]
        end = coords[-1]
        d = ((start[0] - end[0]) ** 2 + (start[1] - end[1]) ** 2) ** 0.5
        is_loop = d < 0.01  # ~0.7 miles

    is_hilly = elevation > 2000
    is_epic = distance > 80

    # Build name
    base = landmark or region
    if base == "Other":
        base = "Cycling"

    if is_epic:
        suffix = "Century" if distance >= 95 else "Epic"
    elif is_loop and is_hilly:
        suffix = "Climb"
    elif is_hilly:
        suffix = "Climb"
    elif is_loop:
        suffix = "Loop"
    else:
        suffix = "Ride"

    # Add distance to distinguish same-name rides
    if distance >= 10:
        # Use decimal if integer part would be ambiguous (e.g., 40.5 vs 40.2)
        if distance == int(distance):
            dist_tag = f"{int(distance)}mi"
        else:
            dist_tag = f"{distance:.0f}mi"
    else:
        dist_tag = ""

    # Avoid "Stanford Loop Loop" — skip suffix if base already contains it
    if suffix.lower() in base.lower():
        return f"{base} {dist_tag}".strip() if dist_tag else base

    if dist_tag:
        return f"{base} {dist_tag} {suffix}"
    return f"{base} {suffix}"


# ── Token management ────────────────────────────────────────────────

def _get_access_token(force: bool = False) -> str:
    """Get a valid Strava access token, refreshing if needed."""
    if not force and TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass

    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    TOKEN_CACHE.write_text(json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", REFRESH_TOKEN),
        "expires_at": data["expires_at"],
    }, indent=2))

    return data["access_token"]


# ── Activity fetching ───────────────────────────────────────────────

def _fetch_all_rides(token: str) -> list[dict]:
    """Fetch all Ride activities from Strava, paginating."""
    headers = {"Authorization": f"Bearer {token}"}
    rides = []
    page = 1

    while True:
        resp = requests.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        activities = resp.json()
        if not activities:
            break

        for a in activities:
            if a.get("type") == "Ride" or a.get("sport_type") == "Ride":
                rides.append(_transform(a))

        page += 1

    return rides


def _transform(a: dict) -> dict:
    """Transform a Strava activity into our simplified format."""
    # Distance: meters → miles
    distance_mi = round(a.get("distance", 0) / 1609.344, 1)

    # Elevation: meters → feet
    elevation_ft = round(a.get("total_elevation_gain", 0) * 3.28084)

    # Date: ISO → human readable
    start_dt = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))
    date_human = start_dt.strftime("%b %d, %Y")
    year = start_dt.year

    # Moving time: seconds → "Xh Ym"
    secs = a.get("moving_time", 0)
    hours, mins = divmod(secs // 60, 60)
    time_str = f"{hours}h {mins}m" if hours else f"{mins}m"

    # Speed: m/s → mph
    avg_speed = round(a.get("average_speed", 0) * 2.23694, 1)

    # Polyline
    summary_polyline = (a.get("map") or {}).get("summary_polyline", "")
    coords = []
    bbox = None
    if summary_polyline:
        decoded = pl.decode(summary_polyline)
        coords = [[lat, lng] for lat, lng in decoded]
        if coords:
            lats = [c[0] for c in coords]
            lngs = [c[1] for c in coords]
            bbox = {
                "min_lat": min(lats), "max_lat": max(lats),
                "min_lng": min(lngs), "max_lng": max(lngs),
            }

    start_latlng = a.get("start_latlng")
    region = _classify_region(start_latlng)

    raw_name = a.get("name", "Untitled Ride")
    smart = _smart_name(raw_name, region, distance_mi, elevation_ft, start_latlng, coords)

    return {
        "id": a["id"],
        "name": smart,
        "strava_name": raw_name,
        "distance": distance_mi,
        "elevation": elevation_ft,
        "date": date_human,
        "year": year,
        "moving_time": time_str,
        "moving_time_secs": secs,
        "avg_speed": avg_speed,
        "start_latlng": start_latlng,
        "region": region,
        "polyline": summary_polyline,
        "coords": coords,
        "bbox": bbox,
    }


# ── Main ────────────────────────────────────────────────────────────

def fetch_all_rides(force: bool = False) -> list[dict]:
    """Public entrypoint: return every ride (newest first) after token refresh.

    Used by sync.py (writes to Neon) and the legacy main() (writes to
    rides_cache.json). Keeping the disk cache for back-compat during the
    migration window; once Render is cutover we'll drop the file write.
    """
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        raise RuntimeError(
            "Missing STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, or STRAVA_REFRESH_TOKEN"
        )
    token = _get_access_token(force=force)
    rides = _fetch_all_rides(token)
    rides.sort(key=lambda r: r.get("id", 0), reverse=True)
    return rides


def main():
    force = "--force" in sys.argv
    try:
        rides = fetch_all_rides(force=force)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    CACHE_FILE.write_text(json.dumps(rides, indent=2, ensure_ascii=False))
    print(f"Fetched {len(rides)} rides")


if __name__ == "__main__":
    main()
