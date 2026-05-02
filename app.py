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

# ESPN uses 2-letter abbreviations for some teams; normalise to 3-letter standard
_ESPN_MAP = {
    "LV":   "LVA",
    "NY":   "NYL",
    "LA":   "LAS",
    "WAS":  "WSH",
    "GS":   "GSV",
    "CONN": "CON",
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

# ── stats.wnba.com player data ─────────────────────────────────────────────────
def _fetch_all_players(season: str = CURRENT_SEASON) -> list[dict]:
    """
    Pulls leaguedashplayerstats from stats.wnba.com.
    Returns [{player_id, name, team, pos}, …].
    Cached for 1 hour.
    """
    cache_key = f"players_{season}"
    if (cached := cache_get(cache_key)):
        return cached

    url    = "https://stats.wnba.com/stats/leaguedashplayerstats"
    params = {
        "Season":         season,
        "SeasonType":     "Regular Season",
        "PerMode":        "PerGame",
        "MeasureType":    "Base",
        "LeagueID":       "10",
        "GameScope":      "",
        "PlayerPosition": "",
        "LastNGames":     0,
        "Month":          0,
        "OpponentTeamID": 0,
        "PaceAdjust":     "N",
        "PlusMinus":      "N",
        "Rank":           "N",
    }
    try:
        r = wnba_stats.get(url, params=params, timeout=25)
        r.raise_for_status()
        data   = r.json()
        rs     = data["resultSets"][0]
        hdrs   = rs["headers"]
        rows   = rs["rowSet"]

        pid_i  = hdrs.index("PLAYER_ID")
        name_i = hdrs.index("PLAYER_NAME")
        team_i = hdrs.index("TEAM_ABBREVIATION")
        pos_i  = hdrs.index("PLAYER_POSITION") if "PLAYER_POSITION" in hdrs else None

        players = []
        for row in rows:
            players.append({
                "player_id": str(row[pid_i]),
                "name":      row[name_i],
                "team":      norm(row[team_i] or ""),
                "pos":       (row[pos_i] if pos_i is not None else "") or "",
            })

        log.info("Loaded %d players from stats.wnba.com (season %s)", len(players), season)
        cache_set(cache_key, players, ttl=3600)
        return players

    except Exception as exc:
        log.warning("player stats fetch failed: %s", exc)
        return []

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
      player_id (required) — stats.wnba.com player ID
      season    (optional) — 4-digit year, default CURRENT_SEASON
      days      (optional) — window of days to return, default 60
    Response: { player_id, season, logs: [{...}] }
    """
    player_id = request.args.get("player_id", "").strip()
    season    = request.args.get("season", CURRENT_SEASON)
    days      = int(request.args.get("days", 60))

    if not player_id:
        return jsonify({"error": "player_id is required"}), 400

    cache_key = f"logs_{player_id}_{season}"
    if (cached := cache_get(cache_key)):
        # Re-apply days filter on cached data (caller may request different window)
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
        cached_filtered = {
            **cached,
            "logs": [l for l in cached["logs"] if l.get("game_date", "") >= cutoff],
        }
        return jsonify(cached_filtered)

    url    = "https://stats.wnba.com/stats/playergamelog"
    params = {
        "PlayerID":   player_id,
        "Season":     str(season),
        "SeasonType": "Regular Season",
        "LeagueID":   "10",
    }
    try:
        r = wnba_stats.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()
        rs   = data["resultSets"][0]
        hdrs = rs["headers"]
        rows = rs["rowSet"]
    except Exception as exc:
        log.warning("player_logs fetch failed for %s: %s", player_id, exc)
        return jsonify({"player_id": player_id, "season": int(season), "logs": []})

    def hi(name):
        try:
            return hdrs.index(name)
        except ValueError:
            return None

    i_date    = hi("GAME_DATE")
    i_matchup = hi("MATCHUP")
    i_min     = hi("MIN")
    i_pts     = hi("PTS")
    i_reb     = hi("REB")
    i_ast     = hi("AST")
    i_fg3m    = hi("FG3M")
    i_ftm     = hi("FTM")
    i_fga     = hi("FGA")
    i_fta     = hi("FTA")
    i_stl     = hi("STL")
    i_blk     = hi("BLK")
    i_tov     = hi("TOV")

    cutoff_str = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    def fval(row, i):
        if i is None:
            return None
        v = row[i]
        return float(v) if v is not None else None

    def parse_date(raw: str) -> str:
        """'MAY 01, 2026' or 'OCT 15, 2025' → '2026-05-01'"""
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return ""

    def parse_minutes(raw) -> float | None:
        """'35:42' or 35.7 → seconds as float."""
        if raw is None:
            return None
        s = str(raw)
        if ":" in s:
            parts = s.split(":")
            try:
                return float(parts[0]) * 60 + float(parts[1])
            except ValueError:
                pass
        try:
            return float(s) * 60
        except (ValueError, TypeError):
            return None

    logs = []
    for row in rows:
        raw_date  = row[i_date] if i_date is not None else ""
        game_date = parse_date(raw_date)
        if not game_date or game_date < cutoff_str:
            continue

        # MATCHUP format: "LVA vs. NYL" (home) or "LVA @ NYL" (away)
        matchup = (row[i_matchup] if i_matchup is not None else "") or ""
        if " vs. " in matchup:
            parts = matchup.split(" vs. ", 1)
            team, opp = norm(parts[0]), norm(parts[1])
        elif " @ " in matchup:
            parts = matchup.split(" @ ", 1)
            team, opp = norm(parts[0]), norm(parts[1])
        else:
            team, opp = "", ""

        logs.append({
            "season":     int(season),
            "player_id":  player_id,
            "game_date":  game_date,
            "team":       team,
            "opponent":   opp,
            "mp_seconds": parse_minutes(row[i_min] if i_min is not None else None),
            "pts":        fval(row, i_pts),
            "reb":        fval(row, i_reb),
            "ast":        fval(row, i_ast),
            "three_p":    fval(row, i_fg3m),
            "ftm":        fval(row, i_ftm),
            "fga":        fval(row, i_fga),
            "fta":        fval(row, i_fta),
            "stl":        fval(row, i_stl),
            "blk":        fval(row, i_blk),
            "tov":        fval(row, i_tov),
        })

    result = {"player_id": player_id, "season": int(season), "logs": logs}
    cache_set(cache_key, result, ttl=600)
    return jsonify(result)

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
