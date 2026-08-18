#!/usr/bin/env python3
from __future__ import annotations

"""
WNBA Stats Server — Render deployment
Serves schedule, standings, rosters, player logs, and lineups for the
Shattered Backboard WNBA iOS app.

Data sources:
  • ESPN (public JSON APIs) — schedule, standings, rosters
  • stats.wnba.com (public JSON API) — rosters fallback, per-player logs
  • balldontlie WNBA API (free key, set BDL_API_KEY) — daily game-log sync
    into SQLite + schedule/standings fallback when ESPN is filtered

Endpoints:
  GET /                        — health check
  GET /wnba/schedule           — games for a date (ESPN, balldontlie fallback)
  GET /wnba/standings          — conference standings (ESPN, balldontlie fallback)
  GET /wnba/roster             — full player list with IDs (ESPN)
  GET /wnba/stats              — same as /roster (alias)
  GET /wnba/lineups            — projected starters for today's games
  GET /wnba/player_logs        — game log for one player (?player_id=X&season=Y&days=N)
  GET /wnba/sync               — trigger a balldontlie game-log sync (?days=N)
                                 (admin: requires X-API-Key = WNBA_SYNC_KEY)
"""

import os
import json
import re
import hmac
import sqlite3
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")

# ── In-memory response cache ───────────────────────────────────────────────────
_cache: dict = {}

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() < entry["expires"]:
        return entry["data"]
    _cache.pop(key, None)
    return None

def cache_set(key: str, data, ttl: int = 300):
    _cache[key] = {"data": data, "expires": time.time() + ttl}

# ── HTTP sessions ──────────────────────────────────────────────────────────────

# ESPN API — public, no auth required
espn = requests.Session()
espn.headers.update({
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.espn.com/",
    "Origin":          "https://www.espn.com",
    # A self-identifying bot User-Agent gets silently filtered by ESPN from
    # cloud-hosting IP ranges (Render etc.), returning empty results with no
    # error — mimic a real browser instead.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})
# Avoid env/netrc auth resolution overhead and occasional import-lock stalls.
espn.trust_env = False

# ── ESPN circuit breaker ────────────────────────────────────────────────────────
# From datacenter IPs (Render etc.) ESPN/Akamai frequently blocks every request.
# Without protection the log-index builder alone re-fails ~60 requests every
# 30 minutes. After _ESPN_CB_THRESHOLD consecutive *failures* (exceptions/
# timeouts — a 200 with empty data is a success, e.g. no games that day), ESPN
# calls short-circuit to None for _ESPN_CB_COOLDOWN seconds. Any success resets
# the failure counter.
_ESPN_CB_THRESHOLD  = 3      # consecutive failures before opening the breaker
_ESPN_CB_COOLDOWN   = 3600   # seconds the breaker stays open

_espn_cb_lock       = threading.Lock()
_espn_cb_failures   = 0
_espn_cb_open_until = 0.0


def _espn_note_failure() -> None:
    global _espn_cb_failures, _espn_cb_open_until
    with _espn_cb_lock:
        _espn_cb_failures += 1
        if _espn_cb_failures >= _ESPN_CB_THRESHOLD:
            _espn_cb_open_until = time.time() + _ESPN_CB_COOLDOWN
            log.warning("ESPN circuit breaker OPEN for %ds after %d consecutive failures",
                        _ESPN_CB_COOLDOWN, _espn_cb_failures)


def _espn_note_success() -> None:
    global _espn_cb_failures
    with _espn_cb_lock:
        _espn_cb_failures = 0


def _espn_available() -> bool:
    with _espn_cb_lock:
        return time.time() >= _espn_cb_open_until


def _espn_reset_breaker() -> None:
    """Forget failures and close the breaker (tests / manual recovery)."""
    global _espn_cb_failures, _espn_cb_open_until
    with _espn_cb_lock:
        _espn_cb_failures = 0
        _espn_cb_open_until = 0.0


def _espn_get(url: str, params: dict | None = None, timeout: int = 12):
    """ESPN GET with circuit-breaker protection.

    Returns parsed JSON, or None when the request failed or the breaker is
    open. Callers already treat "no data" as "fall through to the next source".
    """
    if not _espn_available():
        log.debug("ESPN circuit breaker open — skipping %s", url)
        return None
    try:
        r = espn.get(url, timeout=timeout, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("ESPN fetch failed %s: %s: %s", url, type(exc).__name__, exc)
        _espn_note_failure()
        return None
    _espn_note_success()
    return data


# stats.wnba.com — requires these headers to avoid 403
wnba_stats = requests.Session()
wnba_stats.headers.update({
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token":  "true",
    "Referer":            "https://www.wnba.com/",
    "Origin":             "https://www.wnba.com",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
})
# Avoid env/netrc auth resolution overhead and occasional import-lock stalls.
wnba_stats.trust_env = False

# balldontlie WNBA API — optional key-based data source ─────────────────────────
# Free tier (https://balldontlie.io): $0/mo, one sport, 5 requests/minute.
# Create a key at https://app.balldontlie.io and set BDL_API_KEY to enable.
# This replaces Basketball-Reference scraping as the source for daily
# game-log updates (schedule/standings also fall back to it when ESPN is
# filtered from datacenter IPs).

BDL_API_KEY     = os.environ.get("BDL_API_KEY", "").strip()
BDL_BASE        = "https://api.balldontlie.io/wnba/v1"
# 5 req/min free-tier limit with a safety margin (seconds between requests).
BDL_MIN_INTERVAL = float(os.environ.get("BDL_MIN_INTERVAL", "12.5") or 12.5)

bdl = requests.Session()
bdl.headers.update({
    "Accept":        "application/json",
    "Authorization": BDL_API_KEY,   # BDL expects the raw key, no "Bearer" prefix
    "User-Agent":    "shattered-backboard-wnba/1.0",
})
bdl.trust_env = False

_bdl_lock        = threading.Lock()
_bdl_last_request = 0.0


def _bdl_throttle(wait: bool) -> bool:
    """Reserve the next rate-limit slot.

    Returns True when a request may proceed. When the budget is spent:
    • wait=True  → sleep until the next slot frees up (background sync).
    • wait=False → return False immediately so web request paths never block.
    """
    global _bdl_last_request
    while True:
        with _bdl_lock:
            now = time.time()
            if now >= _bdl_last_request + BDL_MIN_INTERVAL:
                _bdl_last_request = now
                return True
            remaining = _bdl_last_request + BDL_MIN_INTERVAL - now
        if not wait:
            return False
        time.sleep(min(remaining, 5.0) + 0.05)


def _bdl_get(path: str, params: dict | None = None, timeout: int = 15,
             wait: bool = False) -> dict | None:
    """GET one page from the balldontlie WNBA API. Returns parsed JSON or None."""
    if not BDL_API_KEY:
        return None
    for attempt in (1, 2):
        if not _bdl_throttle(wait=wait and attempt == 1):
            return None   # rate budget spent and caller must not block
        try:
            r = bdl.get(f"{BDL_BASE}{path}", params=params, timeout=timeout)
            if r.status_code == 429 and attempt == 1:
                # Push the next slot out by Retry-After, then retry once.
                try:
                    delay = min(float(r.headers.get("Retry-After", "13") or 13), 65.0)
                except ValueError:
                    delay = 13.0
                with _bdl_lock:
                    _bdl_last_request = max(_bdl_last_request, time.time() + delay)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            # Surface the response body (truncated) — BDL explains *why* it
            # rejected the request there (e.g. 401 tier/trial reasons), which
            # the HTTPError string alone does not include.
            body = ""
            try:
                if r is not None and not r.ok and r.text:
                    body = " — " + r.text[:200]
            except Exception:
                pass
            log.warning("BDL request failed %s %s: %s%s", path, params, exc, body)
            return None
    return None


def _bdl_get_all(path: str, params: dict | None = None, max_pages: int = 25,
                 wait: bool = True) -> list[dict]:
    """Paginate a cursor-based BDL endpoint (per_page=100). Best effort —
    returns whatever pages succeeded, [] when the first page fails."""
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        q = dict(params or {})
        q["per_page"] = 100
        if cursor is not None:
            q["cursor"] = cursor
        data = _bdl_get(path, params=q, wait=wait)
        if not data:
            break
        rows = data.get("data") or []
        out.extend(rows)
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out

# ── WNBA constants ─────────────────────────────────────────────────────────────
CURRENT_SEASON = os.environ.get("WNBA_SEASON", "") or (
    # WNBA seasons run May–October; during the Jan–Apr offseason the
    # "current" season is still the previous calendar year's.
    str(datetime.now(timezone.utc).year if datetime.now(timezone.utc).month >= 5
        else datetime.now(timezone.utc).year - 1)
)

SEED_LOG_CANDIDATES = [
    os.environ.get("WNBA_STARTER_LOGS_PATH", "").strip(),
    os.path.join(os.path.dirname(__file__), "StarterLogs.json"),
    os.path.join(os.path.dirname(__file__), "..", "Shattered Backboard WNBA Stats", "StarterLogs.json"),
]
SEED_LOGS_URL = os.environ.get(
    "WNBA_STARTER_LOGS_URL",
    "https://raw.githubusercontent.com/sam60216021-cell/WNBA-STATS/main/Shattered%20Backboard%20WNBA%20Stats/StarterLogs.json",
).strip()
DOWNLOADED_SEED_PATH = "/tmp/StarterLogs.seed.json"

# ESPN uses 2-letter / truncated abbreviations for some teams; normalise to 3-letter standard
_ESPN_MAP = {
    "LV":         "LVA",
    "NY":         "NYL",
    "LA":         "LAS",
    "WAS":        "WSH",
    "GS":         "GSV",
    "CONN":       "CON",
    "CONNECTICU": "CON",   # ESPN teams-list uses truncated full name
    "DALLAS":     "DAL",   # ESPN teams-list uses full city name
}

def norm(abbr: str) -> str:
    """Normalise any team abbreviation to the 3-letter WNBA standard."""
    a = (abbr or "").upper().strip()
    return _ESPN_MAP.get(a, a)


def _team_id_abbr_map(season: str = CURRENT_SEASON) -> dict[str, str]:
    """Build TEAM_ID -> team abbreviation map from stats.wnba.com."""
    cache_key = f"wnba_team_id_abbr_{season}"
    if (cached := cache_get(cache_key)):
        return cached

    out: dict[str, str] = {}
    data = _wnba_get(
        "https://stats.wnba.com/stats/commonteamyears",
        params={"LeagueID": "10"},
        cache_key="wnba_team_ids",
        ttl=86400,
    )
    if data:
        for row in _wnba_rs(data, "TeamYears"):
            if str(row.get("MAX_YEAR", "")) >= season:
                tid = str(row.get("TEAM_ID", ""))
                abbr = norm(row.get("ABBREVIATION", ""))
                if tid and abbr:
                    out[tid] = abbr
    cache_set(cache_key, out, ttl=86400)
    return out

# ── stats.wnba.com JSON helpers ────────────────────────────────────────────────

def _wnba_rs(data: dict, name: str) -> list[dict]:
    """Parse a named resultSet from stats.wnba.com JSON into a list of dicts."""
    for rs in (data or {}).get("resultSets", []):
        if rs.get("name") == name:
            headers = rs.get("headers", [])
            return [dict(zip(headers, row)) for row in rs.get("rowSet", [])]
    return []


def _wnba_get(url: str, params: dict, cache_key: str = "", ttl: int = 3600, timeout: int = 15):
    """GET a stats.wnba.com endpoint with optional caching. Returns parsed JSON or None."""
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        r = wnba_stats.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("wnba_stats request failed %s %s: %s", url, params, exc)
        return None
    if cache_key:
        cache_set(cache_key, data, ttl=ttl)
    return data

def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")

def _parse_espn_tip(event_date_str: str) -> str:
    """Convert ESPN UTC date string to 'H:MM AM/PM ET'."""
    if not event_date_str:
        return ""
    try:
        dt = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
        return dt.astimezone(ET).strftime("%-I:%M %p ET")
    except Exception:
        return ""

# ── Optional shared-secret auth ────────────────────────────────────────────────
# When the WNBA_API_KEY env var is set, every /wnba/* endpoint requires a
# matching X-API-Key header. Root/health/ping stay open for Render's health
# checks. Leave WNBA_API_KEY unset to run the server unauthenticated.
API_KEY = os.environ.get("WNBA_API_KEY", "").strip()


@app.before_request
def _require_api_key():
    if not API_KEY:
        return None
    if not request.path.startswith("/wnba/"):
        return None
    supplied = request.headers.get("X-API-Key", "").strip()
    if not hmac.compare_digest(supplied, API_KEY):
        return jsonify({"error": "unauthorized"}), 401
    return None


# ── Admin endpoint auth ────────────────────────────────────────────────────────
# /wnba/sync (alias /wnba/scrape) and /wnba/seed_import can burn the
# balldontlie rate budget or rewrite the DB, so they ALWAYS require a shared
# secret — even when read endpoints are left unauthenticated. Set WNBA_SYNC_KEY
# (preferred) or WNBA_API_KEY, and send it as the X-API-Key header. With no key
# configured these endpoints return 503 (the background sync still runs).
SYNC_API_KEY = os.environ.get("WNBA_SYNC_KEY", "").strip()


def _admin_authorized() -> tuple[bool, int, str]:
    """(authorized, http_status, error_message) for admin endpoints."""
    expected = SYNC_API_KEY or API_KEY
    if not expected:
        return False, 503, ("manual sync disabled: set WNBA_SYNC_KEY (or "
                            "WNBA_API_KEY) on the server and send it as X-API-Key")
    supplied = request.headers.get("X-API-Key", "").strip()
    if not hmac.compare_digest(supplied, expected):
        return False, 401, "unauthorized"
    return True, 200, ""



# ── Health check ───────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({"status": "ok", "sport": "wnba", "season": CURRENT_SEASON})

# ── /wnba/schedule ─────────────────────────────────────────────────────────────
def _bdl_schedule(date_iso: str) -> list[dict]:
    """One day's games from balldontlie, in the same shape the app expects.

    Used when ESPN is unavailable/filtered (its responses are frequently
    empty or blocked from datacenter IP ranges). Non-blocking on the BDL
    rate budget — returns [] when the budget is spent this minute.
    """
    data = _bdl_get("/games", params={"dates[]": date_iso}, wait=False)
    games: list[dict] = []
    for g in (data or {}).get("data") or []:
        home = norm((g.get("home_team") or {}).get("abbreviation", ""))
        away = norm((g.get("visitor_team") or {}).get("abbreviation", ""))
        if not home or not away:
            continue
        status    = str(g.get("status") or "")
        status_lc = status.lower()
        if "final" in status_lc or "complete" in status_lc:
            state, status_desc = "post", status.title()
        elif status and status_lc not in ("scheduled", "pre"):
            state, status_desc = "in", status.title()
        else:
            state, status_desc = "pre", "Scheduled"
        status_code = 2 if state == "in" else (3 if state == "post" else 1)
        games.append({
            "game_id":     str(g.get("id", "")),
            "date":        date_iso,
            "away":        away,
            "home":        home,
            "tip":         _parse_espn_tip(str(g.get("date") or "")),
            "status":      status_desc,
            "game_type":   "postseason" if g.get("postseason") else "regular",
            "status_code": status_code,
            "home_score":  int(g.get("home_score") or 0),
            "away_score":  int(g.get("away_score") or 0),
            "period":      int(g.get("period") or 0),
            "missing_away_players": [],
            "missing_home_players": [],
        })
    return games


@app.route("/wnba/schedule")
def schedule():
    date_param  = request.args.get("date", today_et())
    date_nodash = date_param.replace("-", "")               # YYYYMMDD
    date_iso    = f"{date_nodash[:4]}-{date_nodash[4:6]}-{date_nodash[6:]}"

    cache_key = f"schedule_{date_nodash}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
        f"?dates={date_nodash}"
    )
    data = _espn_get(url)   # None when ESPN is blocked / breaker open

    if data is not None and not data.get("events"):
        log.warning("schedule: ESPN returned 200 but 0 events for %s", date_nodash)

    games = []
    for event in (data or {}).get("events", []):
        comp        = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue

        home_abbr = norm(home_c.get("team", {}).get("abbreviation", ""))
        away_abbr = norm(away_c.get("team", {}).get("abbreviation", ""))
        tip       = _parse_espn_tip(event.get("date", ""))

        status_type  = comp.get("status", {}).get("type", {})
        status_desc  = status_type.get("description", "Scheduled")
        state        = status_type.get("state", "pre")
        status_code  = 2 if state == "in" else (3 if state == "post" else 1)

        home_score = int(home_c.get("score") or 0)
        away_score = int(away_c.get("score") or 0)

        # Period / clock for live games
        period = comp.get("status", {}).get("period", 0)

        games.append({
            "game_id":              str(comp.get("id") or event.get("id") or ""),
            "date":                 date_iso,
            "away":                 away_abbr,
            "home":                 home_abbr,
            "tip":                  tip,
            "status":               status_desc,
            "game_type":            "regular",
            "status_code":          status_code,
            "home_score":           home_score,
            "away_score":           away_score,
            "period":               period,
            "missing_away_players": [],
            "missing_home_players": [],
        })

    if not games:
        # ESPN empty/blocked → fall back to balldontlie (no-op without key)
        bdl_games = _bdl_schedule(date_iso)
        if bdl_games:
            log.info("schedule: using %d games from balldontlie for %s", len(bdl_games), date_iso)
            games = bdl_games

    result = {"date": date_iso, "games": games}
    # Cache 2 min for today; longer for past dates
    ttl = 120 if date_iso == today_et() else 3600
    cache_set(cache_key, result, ttl=ttl)
    return jsonify(result)

# ── /wnba/standings ────────────────────────────────────────────────────────────
def _bdl_standings(season: str = CURRENT_SEASON) -> list[dict]:
    """Conference standings from balldontlie in the app's expected shape.

    Used when ESPN is unavailable/filtered. Fields ESPN provides but BDL
    does not (last_10, streak, points for/against) are filled with "" / 0.0.
    Non-blocking on the BDL rate budget.
    """
    rows = _bdl_get_all("/standings", params={"season": season}, max_pages=2, wait=False)
    entries: list[dict] = []
    for row in rows:
        team = row.get("team") or {}
        abbr = norm(team.get("abbreviation", ""))
        if not abbr:
            continue
        conf = str(row.get("conference") or "")
        if conf and "conference" not in conf.lower():
            conf = f"{conf} Conference"
        try:
            wins   = int(row.get("wins") or 0)
            losses = int(row.get("losses") or 0)
        except (TypeError, ValueError):
            wins = losses = 0
        total = wins + losses
        try:
            pct = round(float(row.get("win_percentage") or 0.0), 3) if row.get("win_percentage") \
                else (round(wins / total, 3) if total else 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        entries.append({
            "team_abbreviation": abbr,
            "conference":        conf,
            "wins":              wins,
            "losses":            losses,
            "pct":               pct,
            "home_record":       str(row.get("home_record") or ""),
            "road_record":       str(row.get("away_record") or ""),
            "last_10":           "",
            "streak":            "",
            "points_pg":         0.0,
            "opp_points_pg":     0.0,
        })
    return entries


@app.route("/wnba/standings")
def standings():
    if (cached := cache_get("standings")):
        return jsonify(cached)

    url = "https://site.api.espn.com/apis/v2/sports/basketball/wnba/standings"
    data = _espn_get(url)   # None when ESPN is blocked / breaker open

    entries = []
    for group in (data or {}).get("children", []):
        conf_name = group.get("name", "")
        for entry in group.get("standings", {}).get("entries", []):
            team     = entry.get("team", {})
            abbr     = norm(team.get("abbreviation", ""))
            stat_map = {s["name"]: s for s in entry.get("stats", [])}

            def sv(name, default=0):
                s = stat_map.get(name, {})
                v = s.get("value", default)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return default

            def sd(name, default=""):
                s = stat_map.get(name, {})
                return s.get("displayValue", default)

            wins   = int(sv("wins"))
            losses = int(sv("losses"))
            total  = wins + losses
            pct    = round(wins / total, 3) if total else 0.0

            entries.append({
                "team_abbreviation": abbr,
                "conference":        conf_name,
                "wins":              wins,
                "losses":            losses,
                "pct":               pct,
                "home_record":       sd("homeRecord"),
                "road_record":       sd("awayRecord"),
                "last_10":           sd("lastTenGames") or sd("last10"),
                "streak":            sd("streak"),
                "points_pg":         sv("pointsFor",     0.0),
                "opp_points_pg":     sv("pointsAgainst", 0.0),
            })

    if not entries:
        # ESPN empty/blocked → fall back to balldontlie (no-op without key)
        bdl_entries = _bdl_standings(CURRENT_SEASON)
        if bdl_entries:
            log.info("standings: using %d entries from balldontlie", len(bdl_entries))
            entries = bdl_entries

    result = {"standings": entries}
    cache_set("standings", result, ttl=600)
    return jsonify(result)


# ── /wnba/team_advanced ───────────────────────────────────────────────────────
@app.route("/wnba/team_advanced")
def team_advanced():
    """
    Returns team-level advanced metrics for matchup context.
    Source: stats.wnba.com leaguedashteamstats (MeasureType=Advanced, PerMode=PerGame)
    """
    season = request.args.get("season", CURRENT_SEASON)
    cache_key = f"team_advanced_{season}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    data = _wnba_get(
        "https://stats.wnba.com/stats/leaguedashteamstats",
        params={
            "LeagueID": "10",
            "Season": season,
            "SeasonType": "Regular Season",
            "PerMode": "PerGame",
            "MeasureType": "Advanced",
        },
        cache_key=f"team_advanced_raw_{season}",
        ttl=1800,
    )

    if not data:
        return jsonify({"season": int(season) if str(season).isdigit() else season, "teams": []})

    id_to_abbr = _team_id_abbr_map(season)

    def _n(row: dict, *keys):
        for k in keys:
            if k in row and row.get(k) is not None:
                try:
                    return float(row.get(k))
                except (TypeError, ValueError):
                    continue
        return None

    rows = []
    for row in _wnba_rs(data, "LeagueDashTeamStats"):
        tid = str(row.get("TEAM_ID", ""))
        abbr = norm(row.get("TEAM_ABBREVIATION", "")) or id_to_abbr.get(tid, "")
        if not abbr:
            continue
        rows.append({
            "team_abbreviation": abbr,
            "pace": _n(row, "PACE"),
            "off_rating": _n(row, "OFF_RATING", "OFFRTG"),
            "def_rating": _n(row, "DEF_RATING", "DEFRTG"),
            "net_rating": _n(row, "NET_RATING", "NETRTG"),
            "ts_pct": _n(row, "TS_PCT"),
            "efg_pct": _n(row, "EFG_PCT"),
            "tov_pct": _n(row, "TOV_PCT"),
            "reb_pct": _n(row, "REB_PCT"),
            "ast_ratio": _n(row, "AST_RATIO"),
        })

    result = {
        "season": int(season) if str(season).isdigit() else season,
        "teams": rows,
    }
    cache_set(cache_key, result, ttl=1800)
    return jsonify(result)


# ── /wnba/team_position_splits ───────────────────────────────────────────────
@app.route("/wnba/team_position_splits")
def team_position_splits():
    """
    Returns defensive allowed-stat splits by offensive position bucket.
    Source: local game_logs DB + roster position map.
    """
    season = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 120))
    except (ValueError, TypeError):
        days = 120
    days = max(30, min(days, 365))

    cache_key = f"team_pos_splits_{season}_{days}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    players = _fetch_all_players(season)
    pos_by_pid: dict[str, str] = {
        str(p.get("player_id", "")): (p.get("pos", "") or "")
        for p in players if p.get("player_id")
    }

    def pos_group(raw_pos: str) -> str:
        s = (raw_pos or "").upper()
        if "G" in s:
            return "GUARD"
        if "C" in s and "F" not in s:
            return "BIG"
        if "F" in s:
            return "WING"
        return "WING"

    conn = _db_conn()
    try:
        rows = conn.execute(
            """
            SELECT player_id, opponent, pts, reb, ast, three_p, stl, blk
            FROM game_logs
            WHERE game_date >= ?
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    agg: dict[tuple[str, str], dict] = {}
    for row in rows:
        opp = norm(row["opponent"] or "")
        pid = str(row["player_id"] or "")
        if not opp or not pid:
            continue

        group = pos_group(pos_by_pid.get(pid, ""))
        key = (opp, group)
        bucket = agg.setdefault(key, {
            "count": 0,
            "pts": 0.0,
            "reb": 0.0,
            "ast": 0.0,
            "three_p": 0.0,
            "stl": 0.0,
            "blk": 0.0,
            "pra": 0.0,
            "n_pts": 0,
            "n_reb": 0,
            "n_ast": 0,
            "n_three_p": 0,
            "n_stl": 0,
            "n_blk": 0,
            "n_pra": 0,
        })

        def add(stat_key: str, value):
            if value is None:
                return
            try:
                v = float(value)
            except (TypeError, ValueError):
                return
            bucket[stat_key] += v
            bucket[f"n_{stat_key}"] += 1

        pts = row["pts"]
        reb = row["reb"]
        ast = row["ast"]
        add("pts", pts)
        add("reb", reb)
        add("ast", ast)
        add("three_p", row["three_p"])
        add("stl", row["stl"])
        add("blk", row["blk"])
        if pts is not None and reb is not None and ast is not None:
            add("pra", float(pts) + float(reb) + float(ast))

        bucket["count"] += 1

    def avg(total_key: str, n_key: str, bucket: dict):
        n = bucket.get(n_key, 0)
        if n <= 0:
            return None
        return bucket.get(total_key, 0.0) / n

    out = []
    for (team_abbr, group), bucket in agg.items():
        if bucket["count"] < 20:
            continue
        out.append({
            "team_abbreviation": team_abbr,
            "position_group": group,
            "pts_allowed": avg("pts", "n_pts", bucket),
            "reb_allowed": avg("reb", "n_reb", bucket),
            "ast_allowed": avg("ast", "n_ast", bucket),
            "threepm_allowed": avg("three_p", "n_three_p", bucket),
            "stl_allowed": avg("stl", "n_stl", bucket),
            "blk_allowed": avg("blk", "n_blk", bucket),
            "pra_allowed": avg("pra", "n_pra", bucket),
            "sample_size": int(bucket["count"]),
        })

    result = {
        "season": int(season) if str(season).isdigit() else season,
        "days": days,
        "splits": out,
    }
    cache_set(cache_key, result, ttl=1800)
    return jsonify(result)

# ── Player roster ──────────────────────────────────────────────────────────────

def _fetch_all_players_espn(season: str = CURRENT_SEASON) -> list[dict]:
    """
    ESPN fallback: fetches WNBA player roster from ESPN team roster endpoints.
    Returns [{player_id, name, team, pos}, …]. Cached for 1 hour.
    """
    cache_key = f"espn_players_{season}"
    if (cached := cache_get(cache_key)):
        return cached

    # 1. Get all WNBA team IDs
    teams_data = _espn_get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams",
    )
    if teams_data is None:
        log.warning("ESPN teams fetch failed (blocked or breaker open)")
        return []

    team_list = []
    for sport in teams_data.get("sports", []):
        for league in sport.get("leagues", []):
            for t_entry in league.get("teams", []):
                t    = t_entry.get("team", {})
                tid  = t.get("id")
                abbr = norm(t.get("abbreviation", ""))
                if tid and abbr:
                    team_list.append({"id": tid, "abbr": abbr})

    if not team_list:
        log.warning("No WNBA teams returned from ESPN")
        return []

    # 2. Get roster for each team
    players = []
    for team in team_list:
        roster_data = _espn_get(
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team['id']}/roster",
        )
        if roster_data is None:
            log.warning("ESPN roster fetch failed for team %s (blocked or breaker open)", team["id"])
            continue
        for athlete in roster_data.get("athletes", []):
            pid  = str(athlete.get("id", ""))
            name = athlete.get("displayName", "")
            pos  = athlete.get("position", {}).get("abbreviation", "")
            if pid and name:
                players.append({
                    "player_id": pid,
                    "name":      name,
                    "team":      team["abbr"],
                    "pos":       pos,
                })

    log.info("Loaded %d players from ESPN rosters (fallback)", len(players))
    cache_set(cache_key, players, ttl=3600)
    return players


def _fetch_all_players(season: str = CURRENT_SEASON) -> list[dict]:
    """
    Fetch WNBA roster using ESPN as primary source (stable ESPN athlete IDs).
    Falls back to stats.wnba.com on failure.
    Returns [{player_id, name, team, pos}, …]. Cached for 1 hour.

    ESPN athlete IDs are used as the primary player_id so they remain consistent
    with the bundled StarterLogs.json seed data and the ESPN box-score log index.
    """
    cache_key = f"wnba_players_{season}"
    if (cached := cache_get(cache_key)):
        return cached

    # 1. Try ESPN first (returns stable ESPN athlete IDs)
    players = _fetch_all_players_espn(season)
    if players:
        return players

    # 2. Fall back to stats.wnba.com if ESPN fails
    log.warning("ESPN roster empty — falling back to stats.wnba.com")
    teams_data = _wnba_get(
        "https://stats.wnba.com/stats/commonteamyears",
        params={"LeagueID": "10"},
        cache_key="wnba_team_ids",
        ttl=86400,
    )

    team_ids: list[dict] = []
    if teams_data:
        for row in _wnba_rs(teams_data, "TeamYears"):
            if str(row.get("MAX_YEAR", "")) >= season:
                tid  = str(row.get("TEAM_ID", ""))
                abbr = norm(row.get("ABBREVIATION", ""))
                if tid and abbr:
                    team_ids.append({"id": tid, "abbr": abbr})

    if not team_ids:
        log.warning("stats.wnba.com also returned no team list")
        return []

    for team in team_ids:
        data = _wnba_get(
            "https://stats.wnba.com/stats/commonteamroster",
            params={"TeamID": team["id"], "Season": season, "LeagueID": "10"},
        )
        if not data:
            continue
        for row in _wnba_rs(data, "CommonTeamRoster"):
            pid  = str(row.get("PLAYER_ID", ""))
            name = row.get("PLAYER", "") or row.get("PLAYER_SLUG", "")
            pos  = row.get("POSITION", "")
            if pid and name:
                players.append({
                    "player_id": pid,
                    "name":      name,
                    "team":      team["abbr"],
                    "pos":       pos,
                })

    if players:
        log.info("Loaded %d players from stats.wnba.com rosters (fallback)", len(players))
        cache_set(cache_key, players, ttl=3600)
        return players

    log.warning("Both ESPN and stats.wnba.com returned no players")
    return []


# ── SQLite game-log database ──────────────────────────────────────────────────
# Preferred location is a Render persistent disk (see render.yaml: disk
# `wnba-data` mounted at /var/data) so logs survive deploys/restarts.
# Falls back to /tmp when no disk is mounted (local dev / disk detached) —
# the background balldontlie sync repopulates it automatically on every cold start.

DB_PATH = (
    os.environ.get("WNBA_DB_PATH")
    or ("/var/data/wnba_logs.db" if os.path.isdir("/var/data") else "/tmp/wnba_logs.db")
)

_db_write_lock = threading.Lock()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db() -> None:
    with _db_write_lock, _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_logs (
                log_id      TEXT PRIMARY KEY,
                player_id   TEXT NOT NULL,
                game_date   TEXT NOT NULL,
                season      INTEGER,
                team        TEXT,
                opponent    TEXT,
                mp_seconds  REAL,
                pts         REAL,
                reb         REAL,
                ast         REAL,
                three_p     REAL,
                ftm         REAL,
                fga         REAL,
                fta         REAL,
                stl         REAL,
                blk         REAL,
                tov         REAL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gl_pid_date "
            "ON game_logs(player_id, game_date)"
        )
        conn.commit()
    log.info("DB initialised — %s", DB_PATH)


def _db_upsert_logs(logs: list[dict]) -> int:
    """Insert-or-replace game log dicts. Returns number of rows written."""
    rows = []
    for entry in logs:
        pid   = entry.get("player_id", "")
        gdate = entry.get("game_date", "")
        if not pid or not gdate:
            continue
        rows.append((
            f"{pid}_{gdate}",
            pid, gdate,
            entry.get("season"), entry.get("team"), entry.get("opponent"),
            entry.get("mp_seconds"), entry.get("pts"), entry.get("reb"),
            entry.get("ast"), entry.get("three_p"), entry.get("ftm"),
            entry.get("fga"), entry.get("fta"), entry.get("stl"),
            entry.get("blk"), entry.get("tov"),
        ))
    if not rows:
        return 0
    with _db_write_lock, _db_conn() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO game_logs
               (log_id, player_id, game_date, season, team, opponent,
                mp_seconds, pts, reb, ast, three_p, ftm, fga, fta, stl, blk, tov)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
    return len(rows)


def _db_query_logs(player_ids: list[str], cutoff: str) -> dict[str, list[dict]]:
    """Return {player_id: [log_dict, ...]} for all rows with game_date >= cutoff."""
    if not player_ids:
        return {}
    ph = ",".join("?" * len(player_ids))
    with _db_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM game_logs "
            f"WHERE player_id IN ({ph}) AND game_date >= ? "
            f"ORDER BY game_date DESC",
            player_ids + [cutoff],
        ).fetchall()
    result: dict[str, list] = {}
    for row in rows:
        pid = row["player_id"]
        result.setdefault(pid, []).append({
            "season":     row["season"],
            "player_id":  pid,
            "game_date":  row["game_date"],
            "team":       row["team"],
            "opponent":   row["opponent"],
            "mp_seconds": row["mp_seconds"],
            "pts":        row["pts"],
            "reb":        row["reb"],
            "ast":        row["ast"],
            "three_p":    row["three_p"],
            "ftm":        row["ftm"],
            "fga":        row["fga"],
            "fta":        row["fta"],
            "stl":        row["stl"],
            "blk":        row["blk"],
            "tov":        row["tov"],
        })
    return result


def _db_row_count() -> int:
    with _db_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM game_logs").fetchone()[0]


def _db_status() -> dict:
    with _db_conn() as conn:
        total_rows = conn.execute("SELECT COUNT(*) FROM game_logs").fetchone()[0]
        distinct_players = conn.execute(
            "SELECT COUNT(DISTINCT player_id) FROM game_logs"
        ).fetchone()[0]
        latest_game_date = conn.execute(
            "SELECT MAX(game_date) FROM game_logs"
        ).fetchone()[0]
        earliest_game_date = conn.execute(
            "SELECT MIN(game_date) FROM game_logs"
        ).fetchone()[0]
    return {
        "total_rows": int(total_rows or 0),
        "distinct_players": int(distinct_players or 0),
        "latest_game_date": latest_game_date,
        "earliest_game_date": earliest_game_date,
    }


def _seed_logs_path() -> str | None:
    for path in SEED_LOG_CANDIDATES:
        if path and os.path.exists(path):
            return path
    # Render may deploy only ./server (rootDir=server), so the iOS bundle file
    # may not be present on disk. Fall back to downloading from GitHub raw.
    if SEED_LOGS_URL:
        try:
            r = requests.get(SEED_LOGS_URL, timeout=20)
            r.raise_for_status()
            with open(DOWNLOADED_SEED_PATH, "wb") as f:
                f.write(r.content)
            if os.path.exists(DOWNLOADED_SEED_PATH):
                log.info("Seed download: saved %d bytes from %s", len(r.content), SEED_LOGS_URL)
                return DOWNLOADED_SEED_PATH
        except Exception as exc:
            log.warning("Seed download failed from %s: %s", SEED_LOGS_URL, exc)
    return None


def _seed_db_from_starter_logs() -> int:
    """
    Import bundled StarterLogs.json rows into SQLite for offline/blocked-source fallback.
    Safe to call on every boot; rows are upserted by (player_id, game_date).
    """
    path = _seed_logs_path()
    if not path:
        log.warning("Seed import skipped: StarterLogs.json not found")
        return 0

    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        logs = payload.get("logs", []) if isinstance(payload, dict) else []
    except Exception as exc:
        log.warning("Seed import failed reading %s: %s", path, exc)
        return 0

    if not isinstance(logs, list) or not logs:
        log.warning("Seed import skipped: no logs in %s", path)
        return 0

    n = _db_upsert_logs(logs)
    log.info("Seed import: upserted %d rows from %s", n, path)
    return n


# ── balldontlie game-log sync (replaces Basketball-Reference scraping) ─────────
# Pulls finished-game box scores from the free balldontlie WNBA API into the
# same SQLite game_logs table the app already reads. Rows are keyed on ESPN
# player IDs (what the app and seed data use), resolved by name matching
# against the StarterLogs seed + the live ESPN roster.

_name_to_espn_id: dict[str, str] = {}      # normalized full name → ESPN player_id
_bdl_id_to_espn:  dict[int, str] = {}      # balldontlie player id → ESPN player_id ("" = unmapped)


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/diacritics — used for fuzzy name matching."""
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", name or "")
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())


def _ensure_name_map() -> None:
    """Build normalized name → ESPN player_id from the StarterLogs seed file
    plus the live ESPN roster (best effort). Lazy, once per process."""
    if _name_to_espn_id:
        return
    # 1. Seed file (bundled or downloaded) — the reliable source on Render
    path = _seed_logs_path()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            for p in payload.get("players", []):
                pid, name = p.get("player_id", ""), p.get("name", "")
                if pid and name:
                    _name_to_espn_id[_normalize_name(name)] = str(pid)
            log.info("BDL name map: %d entries from seed %s", len(_name_to_espn_id), path)
        except Exception as exc:
            log.warning("BDL name map: seed load failed: %s", exc)
    # 2. ESPN roster (best effort — may be filtered from datacenter IPs)
    try:
        for p in _fetch_all_players_espn():
            _name_to_espn_id.setdefault(_normalize_name(p.get("name", "")), str(p.get("player_id", "")))
        log.info("BDL name map: %d entries after ESPN roster merge", len(_name_to_espn_id))
    except Exception as exc:
        log.warning("BDL name map: ESPN roster merge failed: %s", exc)


def _espn_id_for_bdl_player(player: dict) -> str | None:
    """Map a balldontlie player object ({id, first_name, last_name}) to the
    ESPN player_id via name matching. Results cached per BDL ID."""
    bdl_id = player.get("id")
    if bdl_id in _bdl_id_to_espn:
        return _bdl_id_to_espn[bdl_id] or None
    _ensure_name_map()
    name = " ".join(x for x in (player.get("first_name"), player.get("last_name")) if x)
    espn_id = _name_to_espn_id.get(_normalize_name(name))
    if not espn_id:
        # Unique last-name match fallback (handles minor name differences)
        last = _normalize_name(player.get("last_name") or "")
        if last:
            candidates = {v for k, v in _name_to_espn_id.items() if k.endswith(last)}
            if len(candidates) == 1:
                espn_id = next(iter(candidates))
    _bdl_id_to_espn[bdl_id] = espn_id or ""
    return espn_id


def _sync_bdl_logs(start_date: str, end_date: str) -> dict:
    """Pull box scores from balldontlie for a date window and upsert them into
    the game_logs DB (keyed on ESPN player IDs).

    Regular-season only (parity with the old BR source). Blocks on the BDL
    rate budget as needed — intended for the background/manual sync paths.
    Returns {games, rows, unmapped}.
    """
    if not BDL_API_KEY:
        log.warning("BDL sync skipped: BDL_API_KEY not configured")
        return {"games": 0, "rows": 0, "unmapped": 0}

    season = int(CURRENT_SEASON) if str(CURRENT_SEASON).isdigit() else None
    games_params: dict = {"start_date": start_date, "end_date": end_date}
    if season:
        games_params["seasons[]"] = season

    games = _bdl_get_all("/games", params=games_params)
    games_by_id: dict = {}
    for g in games:
        games_by_id[g.get("id")] = {
            "home":       norm((g.get("home_team") or {}).get("abbreviation", "")),
            "away":       norm((g.get("visitor_team") or {}).get("abbreviation", "")),
            "date":       str(g.get("date") or "")[:10],
            "postseason": bool(g.get("postseason")),
        }

    stat_rows = _bdl_get_all("/player_stats", params={
        "start_date": start_date, "end_date": end_date,
    })

    logs: list[dict] = []
    unmapped = 0
    for row in stat_rows:
        game     = row.get("game") or {}
        game_id  = game.get("id")
        meta     = games_by_id.get(game_id)
        if meta and meta["postseason"]:
            continue
        if not row.get("min"):          # DNP / inactive — nothing to store
            continue
        espn_id = _espn_id_for_bdl_player(row.get("player") or {})
        if not espn_id:
            unmapped += 1
            continue

        team_abbr = norm((row.get("team") or {}).get("abbreviation", ""))
        opponent  = ""
        if meta:
            opponent = meta["away"] if team_abbr == meta["home"] else meta["home"]

        def _n(key: str):
            try:
                return float(row[key])
            except (KeyError, TypeError, ValueError):
                return None

        try:
            season_val = int(game.get("season") or season or 0)
        except (TypeError, ValueError):
            season_val = 0

        game_date = str(game.get("date") or (meta or {}).get("date") or "")[:10]
        if not game_date:
            continue

        logs.append({
            "season":     season_val,
            "player_id":  espn_id,
            "game_date":  game_date,
            "team":       team_abbr,
            "opponent":   opponent,
            "mp_seconds": _parse_wnba_min(row.get("min")),
            "pts":        _n("pts"),
            "reb":        _n("reb"),
            "ast":        _n("ast"),
            "three_p":    _n("fg3m"),
            "ftm":        _n("ftm"),
            "fga":        _n("fga"),
            "fta":        _n("fta"),
            "stl":        _n("stl"),
            "blk":        _n("blk"),
            "tov":        _n("turnover"),
        })

    written = _db_upsert_logs(logs)
    if unmapped:
        log.warning("BDL sync: %d stat rows skipped (no ESPN-ID name match)", unmapped)
    log.info("BDL sync %s→%s: %d games, %d stat rows, %d written, %d unmapped",
             start_date, end_date, len(games_by_id), len(stat_rows), written, unmapped)
    return {"games": len(games_by_id), "rows": written, "unmapped": unmapped}


_sync_lock  = threading.Lock()
_sync_ready = threading.Event()


def _db_is_fresh() -> bool:
    """True when the DB already holds game logs from the last ~24 h.

    Fresh DBs (persistent disk, or an incremental sync within the last day)
    can skip the cold-start backfill; the 6-hour incremental loop picks up
    newly completed games instead.
    """
    try:
        latest = _db_status().get("latest_game_date") or ""
        if not latest:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        return latest >= cutoff
    except Exception:
        return False


def _backfill_days() -> int:
    try:
        return max(1, min(int(os.environ.get("WNBA_SYNC_BACKFILL_DAYS", "75")), 365))
    except (TypeError, ValueError):
        return 75


def _background_sync() -> None:
    """
    Background daemon thread (replaces the old Basketball-Reference scraper).
    • Cold start with a stale/empty DB: backfill the last
      WNBA_SYNC_BACKFILL_DAYS (default 75) days from balldontlie — a handful
      of paginated calls, rate-limited to the free tier's 5 req/min.
    • Every 6 hours: incremental sync of the last 3 days (cheap, idempotent
      upserts pick up newly completed games and stat corrections).
    """
    if _sync_lock.acquire(blocking=False):
        try:
            if _db_is_fresh():
                log.info("Background sync: DB is fresh — skipping initial backfill")
            else:
                log.info("Background sync: initial backfill starting…")
                start = (datetime.now(ET) - timedelta(days=_backfill_days())).strftime("%Y-%m-%d")
                _sync_bdl_logs(start, today_et())
                log.info("Background sync: initial backfill done — %d rows in DB",
                         _db_row_count())
        except Exception as exc:
            log.warning("Background sync: initial backfill failed: %s", exc)
        finally:
            _sync_ready.set()
            _sync_lock.release()

    while True:
        time.sleep(6 * 3600)  # incremental sync every 6 hours
        if _sync_lock.acquire(blocking=False):
            try:
                recent = (datetime.now(ET) - timedelta(days=3)).strftime("%Y-%m-%d")
                _sync_bdl_logs(recent, today_et())
            except Exception as exc:
                log.warning("Background sync: incremental sync failed: %s", exc)
            finally:
                _sync_lock.release()

# ── stats.wnba.com player game logs ───────────────────────────────────────────

def _parse_wnba_game_date(date_str: str) -> str:
    """Normalise a stats.wnba.com game date to YYYY-MM-DD.
    Handles: 'MAY 15, 2026', '2026-05-15', '05/15/2026'."""
    if not date_str:
        return ""
    s = date_str.strip()
    if len(s) == 10 and s[4] == "-":        # already ISO
        return s
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _parse_wnba_min(val) -> float | None:
    """Convert 'MM:SS' or plain-minute value to total seconds."""
    if val in (None, "", "DNP"):
        return None
    s = str(val).strip()
    if ":" in s:
        try:
            parts = s.split(":")
            return float(parts[0]) * 60 + float(parts[1])
        except ValueError:
            return None
    try:
        return float(s) * 60
    except (ValueError, TypeError):
        return None


def _opp_from_matchup(matchup: str, team_abbr: str) -> str:
    """Extract opponent abbreviation from a MATCHUP string like 'SEA vs. NYL' or 'SEA @ NYL'."""
    if not matchup:
        return ""
    parts = matchup.replace(" @ ", " vs. ").split(" vs. ")
    for part in parts:
        a = norm(part.strip())
        if a.upper() != team_abbr.upper():
            return a
    return ""


def _fetch_player_logs_wnba(
    player_id: str,
    season: str,
    days: int,
    use_espn_fallback: bool = True,
    source_timeout: int = 5,
    start_date: str | None = None,
) -> list[dict]:
    """
    Fetch a single player's game log from stats.wnba.com/stats/playergamelog.
    Falls back to the ESPN box-score index on failure.
    Results filtered to the last `days` days.
    """
    cache_key = f"wnba_log_{player_id}_{season}_{start_date or 'recent'}"
    cached = cache_get(cache_key)
    if cached is not None:
        logs = cached
    else:
        data = _wnba_get(
            "https://stats.wnba.com/stats/playergamelog",
            params={
                "PlayerID":   player_id,
                "Season":     season,
                "SeasonType": "Regular Season",
                "LeagueID":   "10",
            },
            timeout=source_timeout,
        )
        logs: list[dict] = []
        if data:
            for row in _wnba_rs(data, "PlayerGameLog"):
                game_date = _parse_wnba_game_date(str(row.get("GAME_DATE", "")))
                matchup   = row.get("MATCHUP", "")
                # First token before "vs." / "@" is the player's team
                team_abbr = norm(
                    matchup.replace(" @ ", " vs. ").split(" vs. ")[0].strip()
                )
                opp_abbr = _opp_from_matchup(matchup, team_abbr)

                def _n(key):
                    v = row.get(key)
                    try:
                        return float(v) if v is not None else None
                    except (ValueError, TypeError):
                        return None

                logs.append({
                    "season":     int(season) if str(season).isdigit() else 0,
                    "player_id":  player_id,
                    "game_date":  game_date,
                    "team":       team_abbr,
                    "opponent":   opp_abbr,
                    "mp_seconds": _parse_wnba_min(row.get("MIN")),
                    "pts":        _n("PTS"),
                    "reb":        _n("REB"),
                    "ast":        _n("AST"),
                    "three_p":    _n("FG3M"),
                    "ftm":        _n("FTM"),
                    "fga":        _n("FGA"),
                    "fta":        _n("FTA"),
                    "stl":        _n("STL"),
                    "blk":        _n("BLK"),
                    "tov":        _n("TOV"),
                })
            logs.sort(key=lambda l: l.get("game_date", ""), reverse=True)

        if not logs and use_espn_fallback:
            log.warning("stats.wnba.com returned no logs for player %s — falling back to ESPN index", player_id)
            # Cap ESPN index scan to 90 days — scanning 365 days makes 365+ HTTP
            # calls and times out on Render. stats.wnba.com is the right source
            # for deep history; ESPN is only useful for very recent games.
            espn_days = min(days, 20)
            idx  = _build_espn_log_index(days=espn_days)
            logs = idx.get(player_id, [])

        cache_set(cache_key, logs, ttl=1800)

    if start_date:
        try:
            cutoff = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
    else:
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [l for l in logs if l.get("game_date", "") >= cutoff]


# ── ESPN box-score game-log index (fallback) ───────────────────────────────────

def _sv(stats: list, i, made_only: bool = True):
    """Extract a numeric value from an ESPN stats list. Handles 'made-att' format."""
    if i is None or i >= len(stats):
        return None
    v = stats[i]
    if v in (None, "", "--", "DNP"):
        return None
    s = str(v)
    if "-" in s:
        parts = s.split("-", 1)
        try:
            return float(parts[0] if made_only else parts[1])
        except ValueError:
            return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_min(stats: list, i) -> float | None:
    """Convert ESPN minutes value ('25' or '25:42') to seconds as float."""
    if i is None or i >= len(stats):
        return None
    v = stats[i]
    if v in (None, "", "--", "DNP"):
        return None
    s = str(v)
    if ":" in s:
        try:
            p = s.split(":")
            return float(p[0]) * 60 + float(p[1])
        except ValueError:
            return None
    try:
        return float(s) * 60   # plain-minute integer from ESPN
    except (ValueError, TypeError):
        return None


def _extract_boxscore(data: dict, game_date: str, season: str, index: dict) -> None:
    """Parse one ESPN game summary and append per-player logs into index."""
    boxscore = data.get("boxscore", {})
    if not boxscore:
        return

    # Map ESPN team id → abbreviation from the competition header
    team_abbrs: dict[str, str] = {}
    for comp in (data.get("header", {}).get("competitions", [{}])[:1]):
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            team_abbrs[t.get("id", "")] = norm(t.get("abbreviation", ""))
    all_abbrs = list(team_abbrs.values())

    for team_section in boxscore.get("players", []):
        t_info    = team_section.get("team", {})
        team_id   = t_info.get("id", "")
        team_abbr = norm(t_info.get("abbreviation", "")) or team_abbrs.get(team_id, "")
        opp_abbr  = next((a for a in all_abbrs if a != team_abbr), "") if len(all_abbrs) == 2 else ""

        for stat_section in team_section.get("statistics", []):
            names = [n.upper() for n in stat_section.get("names", [])]

            def fi(name: str):
                return names.index(name) if name in names else None

            i_min = fi("MIN"); i_pts = fi("PTS"); i_fg = fi("FG")
            i_3pt = fi("3PT"); i_ft  = fi("FT");  i_reb = fi("REB")
            i_ast = fi("AST"); i_to  = fi("TO");   i_stl = fi("STL")
            i_blk = fi("BLK")

            for ae in stat_section.get("athletes", []):
                athlete = ae.get("athlete", {})
                pid     = str(athlete.get("id", ""))
                stats   = ae.get("stats", [])
                if not pid or not stats:
                    continue
                # Skip DNP entries
                if _parse_min(stats, i_min) is None and _sv(stats, i_pts) is None:
                    continue

                index.setdefault(pid, []).append({
                    "season":     int(season),
                    "player_id":  pid,
                    "game_date":  game_date,
                    "team":       team_abbr,
                    "opponent":   opp_abbr,
                    "mp_seconds": _parse_min(stats, i_min),
                    "pts":        _sv(stats, i_pts),
                    "reb":        _sv(stats, i_reb),
                    "ast":        _sv(stats, i_ast),
                    "three_p":    _sv(stats, i_3pt),
                    "ftm":        _sv(stats, i_ft,  made_only=True),
                    "fga":        _sv(stats, i_fg,  made_only=False),
                    "fta":        _sv(stats, i_ft,  made_only=False),
                    "stl":        _sv(stats, i_stl),
                    "blk":        _sv(stats, i_blk),
                    "tov":        _sv(stats, i_to),
                })


def _build_espn_log_index(days: int = 60) -> dict[str, list]:
    """
    Scans the past `days` days of WNBA scoreboard events, fetches ESPN box scores
    for completed/in-progress games, and returns a player_id → [log] map.
    Cached for 30 minutes so live games refresh frequently.
    """
    cache_key = f"espn_log_idx_{days}"
    if (cached := cache_get(cache_key)):
        return cached

    if not _espn_available():
        # ESPN is filtered from this IP — skip the ~60-request scan entirely.
        log.debug("ESPN log index skipped — circuit breaker open")
        return {}

    today_dt = datetime.now(ET)
    event_ids: list[tuple[str, str]] = []   # (event_id, game_date_iso)

    def _fetch_day(delta: int) -> list[tuple[str, str]]:
        day = today_dt - timedelta(days=delta)
        ds  = day.strftime("%Y%m%d")
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        data = _espn_get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
            params={"dates": ds},
            timeout=10,
        )
        if data is None:
            return []
        return [
            (e.get("id", ""), iso)
            for e in data.get("events", [])
            if e.get("status", {}).get("type", {}).get("state", "") in ("in", "post")
            and e.get("id")
        ]

    day_pool = ThreadPoolExecutor(max_workers=10)
    try:
        day_futures = [day_pool.submit(_fetch_day, d) for d in range(days)]
        try:
            for future in as_completed(day_futures, timeout=60):
                event_ids.extend(future.result())
        except TimeoutError:
            log.warning("ESPN log index: day scan timed out after 60s — using partial results")
    finally:
        day_pool.shutdown(wait=False, cancel_futures=True)

    def _fetch_summary(event_id: str, game_date: str) -> tuple[str, dict | None]:
        data = _espn_get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
            params={"event": event_id},
            timeout=10,
        )
        return game_date, data

    index: dict[str, list] = {}
    summary_pool = ThreadPoolExecutor(max_workers=8)
    try:
        summary_futures = [
            summary_pool.submit(_fetch_summary, eid, gdate)
            for eid, gdate in event_ids if eid
        ]
        try:
            for future in as_completed(summary_futures, timeout=90):
                game_date, data = future.result()
                if data:
                    _extract_boxscore(data, game_date, CURRENT_SEASON, index)
        except TimeoutError:
            log.warning("ESPN log index: box-score fetch timed out after 90s — using partial results")
    finally:
        summary_pool.shutdown(wait=False, cancel_futures=True)

    # Sort each player's logs newest-first
    for pid in index:
        index[pid].sort(key=lambda l: l["game_date"], reverse=True)

    log.info("ESPN log index: %d players, %d events scanned", len(index), len(event_ids))
    # Only cache non-empty results — an empty index usually means a transient
    # upstream failure, and caching it would starve the fallback path for 30 min.
    if index:
        cache_set(cache_key, index, ttl=1800)
    return index

@app.route("/wnba/stats")
def stats():
    season  = request.args.get("season", CURRENT_SEASON)
    players = _fetch_all_players(season)
    return jsonify({"players": players})

@app.route("/wnba/roster")
def roster():
    season  = request.args.get("season", CURRENT_SEASON)
    players = _fetch_all_players(season)
    return jsonify({"players": players})

# ── /wnba/lineups ──────────────────────────────────────────────────────────────
@app.route("/wnba/lineups")
def lineups():
    """
    Returns projected starting lineups for today's games.
    Uses each team's top-5 players by minutes (season-to-date) as starters.
    """
    date_iso  = today_et()
    cache_key = f"lineups_{date_iso}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    # Fetch today's schedule
    date_nodash = date_iso.replace("-", "")
    sched = _espn_get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
        params={"dates": date_nodash},
    )
    if sched is None:
        log.warning("lineups/schedule fetch failed (blocked or breaker open)")
        return jsonify({"date": date_iso, "rows": []})

    # Build team → ordered player list (top minutes = first)
    all_players = _fetch_all_players()
    team_map: dict[str, list] = {}
    for p in all_players:
        team_map.setdefault(p["team"], []).append(p)

    def build_lineup(team_abbr: str) -> list:
        players = team_map.get(team_abbr, [])[:5]
        return [
            {
                "player_id": p["player_id"],
                "name":      p["name"],
                "position":  p.get("pos", ""),
                "team":      team_abbr,
                "status":    "Active",
                "source":    "projected",
            }
            for p in players
        ]

    rows = []
    for event in sched.get("events", []):
        comp        = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_c or not away_c:
            continue

        home_abbr = norm(home_c.get("team", {}).get("abbreviation", ""))
        away_abbr = norm(away_c.get("team", {}).get("abbreviation", ""))
        game_id   = str(comp.get("id") or event.get("id") or "")
        tip       = _parse_espn_tip(event.get("date", ""))

        rows.append({
            "game_id":     game_id,
            "date":        date_iso,
            "away":        away_abbr,
            "home":        home_abbr,
            "time":        tip,
            "away_lineup": build_lineup(away_abbr),
            "home_lineup": build_lineup(home_abbr),
        })

    result = {"date": date_iso, "rows": rows}
    cache_set(cache_key, result, ttl=300)
    return jsonify(result)

# ── /wnba/player_logs ─────────────────────────────────────────────────────────
@app.route("/wnba/player_logs")
def player_logs():
    """
    Query params:
      player_id (required) — WNBA/ESPN athlete ID (same IDs served by /wnba/roster)
      season    (optional) — 4-digit year, default CURRENT_SEASON
      days      (optional) — window of days to include, default 60
      start_date(optional) — ISO date to start the window from (e.g. 2026-08-03)
    Response: { player_id, season, logs: [{...}] }
    """
    player_id = request.args.get("player_id", "").strip()
    season    = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 60))
    except (ValueError, TypeError):
        days = 60
    start_date = request.args.get("start_date", "").strip()

    if not player_id:
        return jsonify({"error": "player_id is required"}), 400

    if start_date:
        try:
            cutoff = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400
    else:
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    # 1. Query the local DB (instant — populated by the background balldontlie sync)
    db_result = _db_query_logs([player_id], cutoff)
    if player_id in db_result:
        logs = db_result[player_id]
        return jsonify({
            "player_id": player_id,
            "season":    int(season) if str(season).isdigit() else season,
            "logs":      logs,
        })

    # 2. DB empty (e.g. server just restarted) — fall back to live fetch
    try:
        logs = _fetch_player_logs_wnba(player_id, season, days, start_date=start_date)
    except Exception as exc:
        log.exception("player_logs fallback failed for %s: %s", player_id, exc)
        return jsonify({"player_id": player_id, "season": season, "logs": []}), 200

    if logs:
        _db_upsert_logs(logs)   # warm the DB for next time

    return jsonify({
        "player_id": player_id,
        "season":    int(season) if str(season).isdigit() else season,
        "logs":      logs,
    })


# ── /wnba/player_logs_bulk ─────────────────────────────────────────────────────
@app.route("/wnba/player_logs_bulk")
def player_logs_bulk():
    """
    Fetches game logs for multiple players in a single request.

    Query params:
      player_ids (required) — comma-separated list of player IDs
      season     (optional) — 4-digit year, default CURRENT_SEASON
      days       (optional) — window of days to include, default 60
      start_date (optional) — ISO date to start the window from (e.g. 2026-08-03)

    Response: { season, logs_by_player: { player_id: [{...}], ... } }

    Strategy:
      1. Query the SQLite DB (built and refreshed by the background balldontlie sync).
         This is instant and covers all players in all recent games.
      2. For any player not found in the DB (DB still warming up after cold start),
         try the ESPN log index (capped at 90 days to prevent Render timeout).
      3. Last resort: stats.wnba.com per-player fallback.
    """
    ids_raw = request.args.get("player_ids", "").strip()
    if not ids_raw:
        return jsonify({"error": "player_ids is required"}), 400

    season = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 60))
    except (ValueError, TypeError):
        days = 60
    start_date = request.args.get("start_date", "").strip()

    # Cap to a reasonable number to prevent abuse
    player_ids = [p.strip() for p in ids_raw.split(",") if p.strip()][:150]
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            cutoff = start_dt.strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400
    else:
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        # 1. SQLite DB query (instant)
        logs_by_player = _db_query_logs(player_ids, cutoff)
        missing_ids    = [pid for pid in player_ids if pid not in logs_by_player]

        # 2. ESPN log index for players missing from the DB.
        if missing_ids:
            db_is_cold = (
                len(missing_ids) == len(player_ids) and not _sync_ready.is_set()
            )
            # Building the ESPN index can be expensive (one scoreboard call per day).
            # When only a small subset of players is missing, skip that scan and go
            # straight to bounded per-player fallback to avoid worker timeout spikes.
            miss_ratio = len(missing_ids) / max(1, len(player_ids))
            use_espn_index = db_is_cold or miss_ratio >= 0.30

            if use_espn_index:
                espn_days = 20 if db_is_cold else min(days, 20)
                index = _build_espn_log_index(days=espn_days)
            else:
                index = {}

            still_missing: list[str] = []
            for pid in missing_ids:
                entries = [l for l in index.get(pid, []) if l.get("game_date", "") >= cutoff]
                if entries:
                    logs_by_player[pid] = entries
                    _db_upsert_logs(entries)   # cache for next time
                else:
                    still_missing.append(pid)

            # 3. stats.wnba.com per-player fallback (last resort)
            # During cold-start/high-latency windows this can exceed Gunicorn timeout.
            # Keep this bounded so API stays responsive and returns partial results.
            if still_missing:
                if db_is_cold:
                    for pid in still_missing:
                        logs_by_player[pid] = []
                    log.warning(
                        "player_logs_bulk: DB cold + %d players still missing after ESPN index; "
                        "skipping deep fallback to avoid request timeout",
                        len(still_missing),
                    )
                else:
                    # Default to 0 so bulk endpoints stay fast and never block
                    # on slow upstream per-player fallbacks unless explicitly enabled.
                    max_fallback = int(os.environ.get("WNBA_BULK_FALLBACK_MAX", "0"))
                    limited_ids = still_missing[:max_fallback]
                    skipped_ids = still_missing[max_fallback:]

                    for pid in skipped_ids:
                        logs_by_player[pid] = []

                    if limited_ids:
                        with ThreadPoolExecutor(max_workers=min(4, len(limited_ids))) as ex:
                            futures = {
                                ex.submit(
                                    _fetch_player_logs_wnba,
                                    pid,
                                    season,
                                    min(days, 45),
                                    False,
                                    3,
                                    start_date,
                                ): pid
                                for pid in limited_ids
                            }
                            for fut in as_completed(futures):
                                pid = futures[fut]
                                try:
                                    fb_logs = fut.result()
                                except Exception as exc:
                                    log.warning("bulk fallback failed for %s: %s", pid, exc)
                                    fb_logs = []
                                logs_by_player[pid] = fb_logs
                                if fb_logs:
                                    _db_upsert_logs(fb_logs)
    except Exception as exc:
        log.exception("player_logs_bulk unexpected failure: %s", exc)
        # Never fail the whole bulk request; return best-effort data.
        logs_by_player = locals().get("logs_by_player", {}) or {}
        missing_ids = [pid for pid in player_ids if pid not in logs_by_player]
        for pid in missing_ids:
            logs_by_player[pid] = []

    log.info(
        "player_logs_bulk: %d requested, %d from DB, %d needed fallback",
        len(player_ids), len(player_ids) - len(missing_ids), len(missing_ids),
    )
    return jsonify({
        "season":         int(season) if str(season).isdigit() else season,
        "logs_by_player": logs_by_player,
    })


# ── /wnba/play_by_play ───────────────────────────────────────────────────────
@app.route("/wnba/play_by_play")
def play_by_play():
    """
    Query params:
      game_id (required) — ESPN event ID
    Response:
      {
        game_id,
        play_by_play: [...],
        status,
        source
      }
    """
    game_id = request.args.get("game_id", "").strip()
    if not game_id:
        return jsonify({"error": "game_id is required"}), 400

    cache_key = f"pbp_{game_id}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    data = _espn_get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
        params={"event": game_id},
    )
    if data is None:
        log.warning("play_by_play fetch failed for %s (blocked or breaker open)", game_id)
        return jsonify({
            "game_id": game_id,
            "play_by_play": [],
            "status": "unavailable",
            "source": "espn_summary",
        }), 200

    result = {
        "game_id": game_id,
        "play_by_play": data.get("drives") or data.get("plays") or [],
        "status": data.get("header", {}).get("competitions", [{}])[0].get("status", {}),
        "source": "espn_summary",
    }
    cache_set(cache_key, result, ttl=60)
    return jsonify(result)


# ── /wnba/box_score ──────────────────────────────────────────────────────────
@app.route("/wnba/box_score")
def box_score():
    """
    Query params:
      game_id (required) — ESPN event ID
    Response:
      {
        game_id,
        boxscore,
        players,
        team_stats,
        source
      }
    """
    game_id = request.args.get("game_id", "").strip()
    if not game_id:
        return jsonify({"error": "game_id is required"}), 400

    cache_key = f"box_{game_id}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    data = _espn_get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
        params={"event": game_id},
    )
    if data is None:
        log.warning("box_score fetch failed for %s (blocked or breaker open)", game_id)
        return jsonify({
            "game_id": game_id,
            "boxscore": {},
            "players": [],
            "team_stats": [],
            "source": "espn_summary",
        }), 200

    box = data.get("boxscore", {})
    result = {
        "game_id": game_id,
        "boxscore": box,
        "players": box.get("players", []),
        "team_stats": box.get("teams", []),
        "source": "espn_summary",
    }
    cache_set(cache_key, result, ttl=60)
    return jsonify(result)



# ── /wnba/sync (alias /wnba/scrape) ────────────────────────────────────────────
@app.route("/wnba/sync")
@app.route("/wnba/scrape")
def sync():
    """
    Manually trigger a balldontlie game-log sync.
    Query params: days (optional, default 3, max 120) — how far back to sync.
    Response: { status, rows, games, unmapped, total_rows }

    Requires an X-API-Key header matching WNBA_SYNC_KEY (or WNBA_API_KEY).
    """
    ok, code, err = _admin_authorized()
    if not ok:
        return jsonify({"status": "error", "error": err}), code
    if not BDL_API_KEY:
        return jsonify({"status": "error", "error": "BDL_API_KEY not configured"}), 503
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Sync already in progress"}), 202

    try:
        try:
            days = max(1, min(int(request.args.get("days", 3)), 120))
        except (TypeError, ValueError):
            days = 3
        start = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
        res = _sync_bdl_logs(start, today_et())
        return jsonify({**res, "status": "ok", "total_rows": _db_row_count()})
    except Exception as exc:
        log.exception("Manual BDL sync failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        _sync_lock.release()


# ── /wnba/seed_import ─────────────────────────────────────────────────────────
@app.route("/wnba/seed_import")
def seed_import():
    """
    Manually re-import StarterLogs seed rows into SQLite.
    Useful when upstream APIs are degraded and DB needs a quick baseline reload.

    Requires an X-API-Key header matching WNBA_SYNC_KEY (or WNBA_API_KEY).
    """
    ok, code, err = _admin_authorized()
    if not ok:
        return jsonify({"status": "error", "error": err}), code
    n = _seed_db_from_starter_logs()
    return jsonify({
        "status": "ok",
        "rows_upserted": n,
        "total_rows": _db_row_count(),
    })


# ── /wnba/data_status ─────────────────────────────────────────────────────────
@app.route("/wnba/data_status")
def data_status():
    """
    Returns quick diagnostics for local data availability/freshness.
    Useful for confirming seed import + scrape/backfill behavior.
    """
    status = _db_status()
    status.update({
        "season": int(CURRENT_SEASON) if str(CURRENT_SEASON).isdigit() else CURRENT_SEASON,
        "sync_ready": _sync_ready.is_set(),
        "bdl_configured": bool(BDL_API_KEY),
        "db_path": DB_PATH,
    })
    return jsonify(status)


# ── Entry point ────────────────────────────────────────────────────────────────
# Initialise the SQLite DB and launch the background balldontlie sync before serving
# any requests.  Both are safe to call from the module-level on Render.
# Set WNBA_DISABLE_BG_SYNC=1 to skip the thread (tests / debugging).
_init_db()
if _db_row_count() == 0:
    _seed_db_from_starter_logs()
if os.environ.get("WNBA_DISABLE_BG_SYNC", "").lower() in ("1", "true", "yes"):
    log.info("Background sync disabled via WNBA_DISABLE_BG_SYNC")
else:
    threading.Thread(target=_background_sync, daemon=True, name="bg-sync").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
