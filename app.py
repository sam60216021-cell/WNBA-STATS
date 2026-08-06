#!/usr/bin/env python3
from __future__ import annotations

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
import json
import sqlite3
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Avoid env/netrc auth resolution overhead and occasional import-lock stalls.
espn.trust_env = False

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

# ── WNBA constants ─────────────────────────────────────────────────────────────
CURRENT_SEASON = os.environ.get("WNBA_SEASON", "2026")

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
# Stored in /tmp — always available on Render, even the free tier.
# The filesystem is ephemeral (wiped on every deploy/restart), so the
# background scraper repopulates it automatically on every cold start.

DB_PATH = "/tmp/wnba_logs.db"
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


# ── Parallel ESPN scraper ─────────────────────────────────────────────────────

def _scrape_date_range(days: int = 90) -> int:
    """
    Fetch ESPN box scores for every completed/live WNBA game in the last
    `days` days and upsert them into the SQLite DB.

    Uses two parallel pools:
      - 10 workers to fetch scoreboard pages (one per calendar day)
      - 8 workers to fetch individual game box scores

    Returns the total number of log rows upserted.
    """
    today_dt = datetime.now(ET)

    # ── 1. Collect event IDs (parallel) ──────────────────────────────────────
    def _fetch_day_events(delta: int) -> list[tuple[str, str]]:
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
            return []
        return [
            (e.get("id", ""), iso)
            for e in data.get("events", [])
            if e.get("status", {}).get("type", {}).get("state", "") in ("in", "post")
            and e.get("id")
        ]

    event_ids: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for result in as_completed(
            [pool.submit(_fetch_day_events, d) for d in range(days)]
        ):
            event_ids.extend(result.result())

    log.info("Scraper: %d completed/live events over last %d days", len(event_ids), days)

    # ── 2. Fetch box scores + extract player logs (parallel) ─────────────────
    def _fetch_box(event_id: str, game_date: str) -> list[dict]:
        try:
            r = espn.get(
                "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
                params={"event": event_id},
                timeout=12,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.debug("Box score %s failed: %s", event_id, exc)
            return []
        idx: dict[str, list] = {}
        _extract_boxscore(data, game_date, CURRENT_SEASON, idx)
        return [row for rows in idx.values() for row in rows]

    all_logs: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_box, eid, gdate): (eid, gdate)
            for eid, gdate in event_ids
        }
        for future in as_completed(futures):
            all_logs.extend(future.result())

    n = _db_upsert_logs(all_logs)
    log.info("Scraper: upserted %d rows from %d events", n, len(event_ids))
    return n


_scrape_lock  = threading.Lock()
_scrape_ready = threading.Event()   # set after the initial 90-day scrape


def _background_scraper() -> None:
    """
    Background daemon thread.
    • On cold start: scrapes the last 90 days (covers the full current season).
    • Every 20 minutes after that: re-scrapes the last 3 days to pick up
      any games that finished or went live since the last run.
    """
    if _scrape_lock.acquire(blocking=False):
        try:
            log.info("Background scraper: initial 90-day scrape starting…")
            _scrape_date_range(days=90)
            log.info(
                "Background scraper: initial scrape done — %d rows in DB",
                _db_row_count(),
            )
        except Exception as exc:
            log.warning("Background scraper: initial scrape failed: %s", exc)
        finally:
            _scrape_ready.set()
            _scrape_lock.release()

    while True:
        time.sleep(1200)  # 20-minute refresh interval
        if _scrape_lock.acquire(blocking=False):
            try:
                _scrape_date_range(days=3)
            except Exception as exc:
                log.warning("Background scraper: incremental scrape failed: %s", exc)
            finally:
                _scrape_lock.release()


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
) -> list[dict]:
    """
    Fetch a single player's game log from stats.wnba.com/stats/playergamelog.
    Falls back to the ESPN box-score index on failure.
    Results filtered to the last `days` days.
    """
    cache_key = f"wnba_log_{player_id}_{season}"
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
      player_id (required) — WNBA/ESPN athlete ID (same IDs served by /wnba/roster)
      season    (optional) — 4-digit year, default CURRENT_SEASON
      days      (optional) — window of days to include, default 60
    Response: { player_id, season, logs: [{...}] }
    """
    player_id = request.args.get("player_id", "").strip()
    season    = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 60))
    except (ValueError, TypeError):
        days = 60

    if not player_id:
        return jsonify({"error": "player_id is required"}), 400

    cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    # 1. Query the local DB (instant — populated by background scraper)
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
        logs = _fetch_player_logs_wnba(player_id, season, days)
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

    Response: { season, logs_by_player: { player_id: [{...}], ... } }

    Strategy:
      1. Query the SQLite DB (built and refreshed by the background scraper).
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

    # Cap to a reasonable number to prevent abuse
    player_ids = [p.strip() for p in ids_raw.split(",") if p.strip()][:150]
    cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        # 1. SQLite DB query (instant)
        logs_by_player = _db_query_logs(player_ids, cutoff)
        missing_ids    = [pid for pid in player_ids if pid not in logs_by_player]

        # 2. ESPN log index for players missing from the DB.
        if missing_ids:
            db_is_cold = (
                len(missing_ids) == len(player_ids) and not _scrape_ready.is_set()
            )
            espn_days = 20 if db_is_cold else min(days, 90)
            index     = _build_espn_log_index(days=espn_days)
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

    try:
        r = espn.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
            params={"event": game_id},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("play_by_play fetch failed for %s: %s", game_id, exc)
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

    try:
        r = espn.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
            params={"event": game_id},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("box_score fetch failed for %s: %s", game_id, exc)
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



# ── /wnba/scrape ───────────────────────────────────────────────────────────────
@app.route("/wnba/scrape")
def scrape():
    """
    Manually trigger a scrape of recent WNBA game logs.
    Query params:
      days (optional) — how many days to scrape, default 7, max 90
    Response: { status, rows_upserted, total_rows }
    """
    try:
        days = min(int(request.args.get("days", 7)), 90)
    except (ValueError, TypeError):
        days = 7

    if not _scrape_lock.acquire(blocking=False):
        return jsonify({"status": "busy", "message": "Scrape already in progress"}), 202

    try:
        n = _scrape_date_range(days=days)
        return jsonify({"status": "ok", "rows_upserted": n, "total_rows": _db_row_count()})
    except Exception as exc:
        log.exception("Manual scrape failed: %s", exc)
        return jsonify({"status": "error", "error": str(exc)}), 500
    finally:
        _scrape_lock.release()


# ── /wnba/seed_import ─────────────────────────────────────────────────────────
@app.route("/wnba/seed_import")
def seed_import():
    """
    Manually re-import StarterLogs seed rows into SQLite.
    Useful when upstream APIs are degraded and DB needs a quick baseline reload.
    """
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
        "scrape_ready": _scrape_ready.is_set(),
        "db_path": DB_PATH,
    })
    return jsonify(status)


# ── Entry point ────────────────────────────────────────────────────────────────
# Initialise the SQLite DB and launch the background scraper before serving
# any requests.  Both are safe to call from the module-level on Render.
_init_db()
if _db_row_count() == 0:
    _seed_db_from_starter_logs()
threading.Thread(target=_background_scraper, daemon=True, name="bg-scraper").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
