# Ride Atlas — California Cycling Map

A personal California cycling map dashboard. Part of the `SNEHA.OS` dashboard (tab: **Ride Atlas** at `/rides`).

Shows three layers of cycling data on an outline of California:
- 🔴 **Ridden** — rides actually completed (from Strava)
- 🔵 **Booked** — upcoming cycling trips (from Google Sheets → Master Planner tab)
- 🟡 **Wishlist** — California rides I want to do someday (from Google Sheets → Library tab)

---

## Live URL

`https://mbp-fvnwqhyh4j.tail790bc5.ts.net/rides`

- Runs on my MacBook Pro (`claude-mcp-server/server.js`, Express/Node on port `8275`)
- Exposed publicly via **Tailscale Funnel** (not private — anyone with the URL can view)
- No authentication

---

## Data sources

### 🔴 Ridden — Strava API
- Pulled by `strava_fetch.py` → written to `rides_cache.json`
- All activities where `type == "Ride"` are included
- Each ride brings its GPS start point (`start_latlng`), distance, elevation, date, moving time
- Token refresh handled automatically; cached in `.strava_token_cache.json`

### 🔵 Booked — Google Sheets `Master Planner` tab
Filter criteria (all must be true):
| Column | Requirement |
|---|---|
| `Year` | Current year (e.g., 2026) |
| `Status` | `Booked` or `Completed` |
| Icon (derived from name) | 🚴 (name contains "bike", "gran fondo", "century", "fondo", "cycling", "otter") |
| `End Date` | In the future (past trips don't show as blue) |

### 🟡 Wishlist — Google Sheets `Library` tab
Filter criteria (all must be true):
| Column | Requirement |
|---|---|
| `State` | Contains `California` or `CA` (or "california" in name) |
| `Tags` | Contains `Biking` or `Cycling` (case-insensitive) |
| `Status` | Anything except `Completed` |

---

## How pins get their location

- **Ridden:** GPS `start_latlng` comes from Strava directly — no lookup needed
- **Booked & Wishlist:** name-keyword lookup in a hardcoded Python dict `DESTINATION_COORDS` inside `rides_report.py` (e.g., `"nevada city" → (39.26, -121.02)`)
- **Fallback:** `_auto_geocode()` hits OpenStreetMap Nominatim, but has SSL issues in the venv so it's unreliable. The manual dict is the source of truth. **When a new city appears in a sheet, add its keyword → (lat, lng) entry in `rides_report.py`.**

---

## Priority / dedup rules

```
Ridden (red)         → always show
Booked (blue)        → always show
Wishlist Event       → always show (bypasses dedup)
Wishlist Generic     → hide if within ~15 mi of a Ridden OR Booked pin
```

- **Event-tagged** = any Library row whose `Tags` contain `Event` (e.g., `Biking, Event`). Specific dated races — always show.
- **Generic** = Library rows without `Event` in tags. Hide when superseded by something more concrete.

**Principle:** specific dated items (bookings, events, actual rides) are distinct and always render. Generic "someday in this area" entries hide when you've already ridden or booked there.

---

## Anti-clutter visual techniques

1. **Pre-clustering (ridden):** Rides with the same rounded lat/lng (~1 km) merge into a single dot labeled "Region · N rides". Clicking shows the longest ride's details.
2. **Screen-space super-clustering:** Same-color dots within ~9 SVG pixels merge into one bigger dot with a count badge inside (e.g., red dot with "4"). Keeps dense areas from becoming a solid blob.
3. **City labels as map backdrop:** 26 fixed California cities render in soft gray behind the pins — only if a pin exists within ~30 px, so empty regions don't get cluttered.
4. **Name dedup:** Same city name never prints twice on the map.
5. **Pin-label proximity rule:** If two labels would be within 10 px vertically and 24 px horizontally, only the first one renders.

---

## Click interactions

- **Tap a dot** → popup below the map shows type, name, date/best months, and description. Matching chip in the chip list below highlights.
- **Tap a chip** → popup shows, matching pin glows (drop-shadow).
- **"X" in popup** → closes it.

### Chip list
Below the map, three horizontally-scrollable rows (Ridden / Booked / Wishlist) act as a scannable index. Each chip maps to a pin.

### Popup content by type
- **Ridden cluster:** "Region · N rides · X mi total" + longest-ride details
- **Booked:** "Booked · Upcoming" + start/end dates + "N day trip"
- **Wishlist:** "Wishlist" + best months + description from Library `Notes` column

---

## Other Ride Atlas sections

Above the California map:

- **Monthly Pulse card** — mirrors the Quest Hub's Weekly Pulse widget. Big number for miles this month + 3 stat slots (rides, best, avg) + progress bar toward a 100 mi monthly goal + week-by-week breakdown.
- **Year at a Glance card** — big number for miles this year + bronze/silver/gold milestone medals on a progress bar (250 mi / 500 mi / 1000 mi) + 12-month grid showing miles per month.
- **All-Time stats pills** — total miles / total elevation / total ride count.
- **Crowning Achievement card** — your single longest ride, featured with its own large card.

Below the California map:

- **Upcoming Rides card** — list of booked cycling trips (pulled from Master Planner, 🚴 icon, future dates), formatted as "Apr 17-18, 2026" style ranges.
- **Route cards by region** — up to 6 rides per region group, sorted by distance.
- **Yearly Breakdown table** — year / miles / ride count / best-ride name.
- **Insight block** — auto-generated summary sentence.

---

## Architecture

```
strava_fetch.py          → Strava API                        → rides_cache.json
travel_source.py         → Google Sheets (Master Planner)    → cache/travel_pins.json
travel_source.py         → Google Sheets (Library)           → cache/library_cycling.json
rides_report.py          → reads caches + template           → ~/rides_report.html

claude-mcp-server/server.js
  ├── GET  /rides               → serves ~/rides_report.html
  ├── GET  /api/rides           → returns rides_cache.json as JSON
  └── POST /api/rides/refresh   → runs strava_fetch.py + rides_report.py

Tailscale Funnel → localhost:8275
```

### Key files

- `strava_fetch.py` — fetches Strava, smart-names generic rides ("Morning Ride" → "Marin Headlands 34mi Loop"), classifies regions by bounding box
- `travel_source.py` — Google Sheets fetchers for Master Planner + Library, with 6h disk caching
- `rides_report.py` — renders `templates/rides.html` with all the data, SVG map, clustering logic, and popup JSON
- `templates/rides.html` — the HTML template with CSS + client-side JS for click handling

### Cache TTLs

| Cache | TTL |
|---|---|
| Strava access token | Auto-refresh |
| Strava rides data | Manual (Sync button or `python strava_fetch.py`) |
| Travel pins (Master Planner) | 6 hours |
| Library cycling | 6 hours |
| Geocode (Nominatim fallback) | Persistent JSON |

---

## How to add things

### New wishlist destination
1. Add a row to the `Library` tab with:
   - `Name` = destination
   - `State` = `California`
   - `Tags` = `Biking` (add `Event` for specific dated rides)
   - `Notes` = description
2. If the city isn't in `DESTINATION_COORDS`, add an entry in `rides_report.py`.
3. Wait up to 6 hours OR force-refresh (delete `cache/library_cycling.json`).

### New booked trip
1. Add a row to the `Master Planner` tab:
   - `Year` = current year
   - `Start Date` / `End Date` (supports `2026-04-17` or `April 17, 2026`)
   - `Name` with a cycling keyword so it gets the 🚴 icon
   - `Status` = `Booked`
2. If the city isn't in `DESTINATION_COORDS`, add it.

### Completed a trip
Red dots come **only from Strava**. Recording the ride on Garmin/Strava auto-populates on the next Strava sync. There's no automatic sync from Master Planner "Status = Completed".

---

## Visual design

- Navy background `#0d1b2e` with soft radial gradient top
- Sky-blue hollow California state outline
- 28 px grid background lines
- Glassmorphism cards (frosted white rgba, backdrop blur)
- Fonts: **Fraunces** (serif) for numbers/headings, **DM Mono** for UI text
- Dot colors: `#e05050` (ridden) · `#7dd3fc` (booked sky) · `#f5c842` (wishlist gold)
- All pins are simple solid circles (`r=3.5`) — no rings, no outlines, no complex shapes
- Floating legend overlay in the top-right of the map with live counts

---

## Current pin counts (as of latest build)

- **126 ridden** (from Strava, clustered to ~45 visible dots)
- **4 booked** (Sea Otter Classic, Levi's GranFondo, Nevada City trip, Marin Century)
- **27 wishlist** (after generic-dedup against ridden + booked)

---

## Sneha's cycling profile (context, doesn't affect logic)

- Road cycling only — no gravel, no MTB
- Typical ride: 20–50 miles
- Max ~2,000 ft elevation gain preferred
- Rides for scenery, quiet roads, experience
- Not a suffer-fest climber

---

## Debugging: pin not showing?

Run through this checklist:

1. ✅ Is the row in the correct sheet tab? (Library for wishlist, Master Planner for booked)
2. ✅ Does `Tags` contain `Biking` or `Cycling`?
3. ✅ Is `State` = `California`?
4. ✅ Is `Status` ≠ `Completed`?
5. ✅ Is the name keyword in `DESTINATION_COORDS`? If not, add it to `rides_report.py`.
6. ✅ Is it a generic wishlist entry within 15 mi of a ridden cluster or booked pin? It's being deduped. Add `Event` to its tags to bypass.
7. ✅ Force-refresh caches:
   ```bash
   rm cache/travel_pins.json cache/library_cycling.json
   python rides_report.py
   launchctl kickstart -k gui/$(id -u)/com.sneha.mcp-server
   ```

---

## Manual commands

```bash
# Fetch fresh Strava data
python strava_fetch.py

# Force Strava re-auth (ignore cached token)
python strava_fetch.py --force

# Regenerate the Ride Atlas HTML
python rides_report.py

# Restart the server
launchctl kickstart -k gui/$(id -u)/com.sneha.mcp-server

# Check Tailscale funnel is exposing the server
tailscale serve status
```
