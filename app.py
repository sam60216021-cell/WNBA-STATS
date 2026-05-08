#!/usr/bin/env python3
"""
WNBA Stats Server — Render deployment
Serves schedule, standings, rosters, player logs, and lineups for the
Shattered Backboard WNBA iOS app.

Endpoints:
  GET /                        — health check
  GET /wnba/schedule           — today's games (ESPN)
  GET /wnba/standings          — conference standings (ESPN)
  GET /wnba/roster             — full player list with IDs (stats.wnba.com)
  GET /wnba/stats              — same as /roster (alias)
  GET /wnba/lineups            — projected starters for today's games
  GET /wnba/player_logs        — game log for one player (?player_id=X&season=Y&days=N)
"""

import os
import time
import logging
from datetime import datetime, timedelta
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
    "Accept":     "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; WNBA-Stats-Server/1.0)",
})

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

# ── WNBA constants ─────────────────────────────────────────────────────────────
CURRENT_SEASON = os.environ.get("WNBA_SEASON", "2026")

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

# ── Health check ───────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/health")
@app.route("/ping")
def health():
    return jsonify({"status": "ok", "sport": "wnba", "season": CURRENT_SEASON})

# ── /wnba/schedule ─────────────────────────────────────────────────────────────
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
    try:
        r = espn.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("schedule fetch failed: %s", exc)
        return jsonify({"date": date_iso, "games": []})

    games = []
    for event in data.get("events", []):
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
            "game_id":              comp.get("id", event.get("id", "")),
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

    result = {"date": date_iso, "games": games}
    # Cache 2 min for today; longer for past dates
    ttl = 120 if date_iso == today_et() else 3600
    cache_set(cache_key, result, ttl=ttl)
    return jsonify(result)

# ── /wnba/standings ────────────────────────────────────────────────────────────
@app.route("/wnba/standings")
def standings():
    if (cached := cache_get("standings")):
        return jsonify(cached)

    url = "https://site.api.espn.com/apis/v2/sports/basketball/wnba/standings"
    try:
        r = espn.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("standings fetch failed: %s", exc)
        return jsonify({"standings": []})

    entries = []
    for group in data.get("children", []):
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

    result = {"standings": entries}
    cache_set("standings", result, ttl=600)
    return jsonify(result)

# ── ESPN-based player roster ──────────────────────────────────────────────────

def _fetch_all_players(season: str = CURRENT_SEASON) -> list[dict]:
    """
    Fetches WNBA player roster from ESPN team roster endpoints.
    Returns [{player_id, name, team, pos}, …].
    Season parameter kept for API compatibility; ESPN always returns current roster.
    Cached for 1 hour.
    """
    cache_key = "espn_players"
    if (cached := cache_get(cache_key)):
        return cached

    # 1. Get all WNBA team IDs
    try:
        r = espn.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams",
            timeout=12,
        )
        r.raise_for_status()
        teams_data = r.json()
    except Exception as exc:
        log.warning("ESPN teams fetch failed: %s", exc)
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
        try:
            r = espn.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team['id']}/roster",
                timeout=12,
            )
            r.raise_for_status()
            roster_data = r.json()
        except Exception as exc:
            log.warning("ESPN roster fetch failed for team %s: %s", team["id"], exc)
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

    log.info("Loaded %d players from ESPN rosters", len(players))
    cache_set(cache_key, players, ttl=3600)
    return players


# ── ESPN box-score game-log index ──────────────────────────────────────────────

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

    today_dt = datetime.now(ET)
    event_ids: list[tuple[str, str]] = []   # (event_id, game_date_iso)

    for delta in range(days):
        day = today_dt - timedelta(days=delta)
        ds  = day.strftime("%Y%m%d")
        iso = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        try:
            r = espn.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
                params={"dates": ds},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            continue
        for event in data.get("events", []):
            state = event.get("status", {}).get("type", {}).get("state", "")
            if state in ("in", "post"):
                event_ids.append((event.get("id", ""), iso))

    index: dict[str, list] = {}
    for event_id, game_date in event_ids:
        if not event_id:
            continue
        try:
            r = espn.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
                params={"event": event_id},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.debug("Summary %s failed: %s", event_id, exc)
            continue
        _extract_boxscore(data, game_date, CURRENT_SEASON, index)

    # Sort each player's logs newest-first
    for pid in index:
        index[pid].sort(key=lambda l: l["game_date"], reverse=True)

    log.info("ESPN log index: %d players, %d events scanned", len(index), len(event_ids))
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
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
        f"?dates={date_nodash}"
    )
    try:
        r = espn.get(url, timeout=12)
        r.raise_for_status()
        sched = r.json()
    except Exception as exc:
        log.warning("lineups/schedule fetch failed: %s", exc)
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
        game_id   = comp.get("id", event.get("id", ""))
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
      player_id (required) — ESPN athlete ID (same IDs served by /wnba/roster)
      season    (optional) — 4-digit year, default CURRENT_SEASON (informational label)
      days      (optional) — window of days to scan, default 60
    Response: { player_id, season, logs: [{...}] }
    """
    player_id = request.args.get("player_id", "").strip()
    season    = request.args.get("season", CURRENT_SEASON)
    days      = int(request.args.get("days", 60))

    if not player_id:
        return jsonify({"error": "player_id is required"}), 400

    index  = _build_espn_log_index(days=days)
    cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
    logs   = [l for l in index.get(player_id, []) if l.get("game_date", "") >= cutoff]

    return jsonify({"player_id": player_id, "season": int(season), "logs": logs})


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
