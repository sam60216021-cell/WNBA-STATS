#!/usr/bin/env python3
from __future__ import annotations

"""
WNBA Stats Server — Render deployment
Serves schedule, standings, rosters, player logs, and lineups for the
Shattered Backboard WNBA iOS app.

Data source:
  • StarterLogs.json — generated on the maintainer's Mac (ESPN scraped from a
    residential IP, which is not blocked) and pushed via git. Render deploys
    and serves this bundle directly. No upstream API calls happen at request
    time: ESPN/Akamai blocks datacenter IPs, so live fetching from the server
    was removed entirely.

Endpoints (routes and JSON shapes unchanged from the previous server):
  GET /                        — health check
  GET /wnba/schedule           — games for a date (?date=YYYY-MM-DD)
  GET /wnba/standings          — standings derived from seed results + logs
  GET /wnba/roster             — player list with IDs
  GET /wnba/stats              — same as /roster (alias)
  GET /wnba/lineups            — projected starters for today's games
  GET /wnba/player_logs        — game log for one player (?player_id=X&season=Y&days=N)
  GET /wnba/player_logs_bulk   — game logs for many players (?player_ids=A,B,C)
  GET /wnba/team_advanced      — empty placeholder (was stats.wnba.com live)
  GET /wnba/team_position_splits — defensive splits derived from seed logs
  GET /wnba/box_score          — degraded empty payload (live ESPN summary removed)
  GET /wnba/seed_import        — reload StarterLogs.json into memory (admin)
  GET /wnba/data_status        — diagnostics on the served seed bundle

To refresh data: double-click "WNBA Update.command" (regenerates the bundle
from ESPN via the Mac, commits, pushes; Render auto-deploys).
"""

import os
import json
import hmac
import time
import logging
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

# ESPN-style abbreviations → 3-letter WNBA standard (defensive normalisation;
# the seed bundle already uses standard abbreviations).
_ABBR_MAP = {
    "LV":         "LVA",
    "NY":         "NYL",
    "LA":         "LAS",
    "WAS":        "WSH",
    "GS":         "GSV",
    "CONN":       "CON",
    "CONNECTICU": "CON",
    "DALLAS":     "DAL",
}

def norm(abbr: str) -> str:
    """Normalise any team abbreviation to the 3-letter WNBA standard."""
    a = (abbr or "").upper().strip()
    return _ABBR_MAP.get(a, a)


def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")

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
SYNC_API_KEY = os.environ.get("WNBA_SYNC_KEY", "").strip()

def _admin_authorized() -> tuple[bool, int, str]:
    """(authorized, http_status, error_message) for admin endpoints."""
    expected = SYNC_API_KEY or API_KEY
    if not expected:
        return False, 503, ("admin action disabled: set WNBA_SYNC_KEY (or "
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
# ── Seed bundle (the single source of truth) ───────────────────────────────────

class SeedData:
    """In-memory view of StarterLogs.json: players, games, and logs."""

    def __init__(self) -> None:
        self.loaded_at: float = 0.0
        self.source_path: str = ""
        self.players_by_id: dict[str, dict] = {}
        self.games_by_date: dict[str, list[dict]] = {}
        self.logs_by_player: dict[str, list[dict]] = {}
        self.meta: dict = {}

    def load(self) -> bool:
        path = self._resolve_path()
        if not path:
            log.warning("Seed load skipped: StarterLogs.json not found")
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            log.warning("Seed load failed reading %s: %s", path, exc)
            return False

        players = payload.get("players") or []
        games   = payload.get("games") or []
        logs    = payload.get("logs") or []

        players_by_id: dict[str, dict] = {}
        for p in players:
            pid = str(p.get("player_id", ""))
            if pid:
                players_by_id[pid] = p

        games_by_date: dict[str, list[dict]] = {}
        for g in games:
            gdate = g.get("date")
            if gdate:
                games_by_date.setdefault(str(gdate), []).append(g)

        logs_by_player: dict[str, list[dict]] = {}
        for entry in logs:
            pid   = str(entry.get("player_id", ""))
            gdate = str(entry.get("game_date", ""))
            if not pid or not gdate:
                continue
            logs_by_player.setdefault(pid, []).append(entry)
        for rows in logs_by_player.values():
            rows.sort(key=lambda l: l.get("game_date", ""), reverse=True)

        self.players_by_id = players_by_id
        self.games_by_date = games_by_date
        self.logs_by_player = logs_by_player
        self.meta = {k: payload.get(k) for k in
                     ("season", "date", "generated_at", "note", "source_status")}
        self.source_path = path
        self.loaded_at = time.time()
        log.info("Seed loaded from %s — %d players, %d games (%d dates), %d log rows",
                 path, len(players_by_id), len(games), len(games_by_date), len(logs))
        return True

    def _resolve_path(self) -> str | None:
        for path in SEED_LOG_CANDIDATES:
            if path and os.path.exists(path):
                return path
        # Render may deploy only ./server (rootDir=server), so the app-bundle
        # file may not be present on disk. Fall back to GitHub raw download.
        if SEED_LOGS_URL:
            try:
                r = requests.get(SEED_LOGS_URL, timeout=20,
                                 headers={"User-Agent": "wnba-stats-server"})
                r.raise_for_status()
                with open(DOWNLOADED_SEED_PATH, "wb") as f:
                    f.write(r.content)
                if os.path.exists(DOWNLOADED_SEED_PATH):
                    log.info("Seed download: saved %d bytes from %s",
                             len(r.content), SEED_LOGS_URL)
                    return DOWNLOADED_SEED_PATH
            except Exception as exc:
                log.warning("Seed download failed from %s: %s", SEED_LOGS_URL, exc)
        return None

    def date_range(self) -> tuple[str | None, str | None]:
        log_dates = sorted({str(l.get("game_date", ""))
                            for rows in self.logs_by_player.values()
                            for l in rows} - {""})
        game_dates = sorted(self.games_by_date.keys())
        earliest = log_dates[0] if log_dates else (game_dates[0] if game_dates else None)
        candidates = [d for d in (game_dates[-1] if game_dates else None,
                                  log_dates[-1] if log_dates else None) if d]
        latest = max(candidates) if candidates else None
        return earliest, latest

    def total_log_rows(self) -> int:
        return sum(len(rows) for rows in self.logs_by_player.values())

_seed = SeedData()

def _seed_schedule(date_iso: str) -> list[dict]:
    """One day's games from the seed bundle, already in the app's shape."""
    return _seed.games_by_date.get(date_iso, [])


def _all_players(season: str = CURRENT_SEASON) -> list[dict]:
    """Player list from the seed: [{player_id, name, team, pos}, …]."""
    return [
        {
            "player_id": pid,
            "name":      p.get("name", ""),
            "team":      norm(p.get("team", "")),
            "pos":       p.get("pos", ""),
        }
        for pid, p in sorted(_seed.players_by_id.items())
    ]


def _query_seed_logs(player_ids: list[str], cutoff: str) -> dict[str, list[dict]]:
    """{player_id: [log, …]} for the given players with game_date >= cutoff."""
    out: dict[str, list[dict]] = {}
    for pid in player_ids:
        rows = [l for l in _seed.logs_by_player.get(pid, [])
                if l.get("game_date", "") >= cutoff]
        if rows:
            out[pid] = rows
    return out

# ── /wnba/schedule ─────────────────────────────────────────────────────────────
@app.route("/wnba/schedule")
def schedule():
    """
    Games for one date, served straight from the pushed seed bundle.

    The date filter is exact: only games whose seed `date` equals the requested
    date are returned — a stale bundle can never leak another day's slate into
    today's schedule. If the bundle has no games for the requested date the
    response is simply empty (an off day, or a bundle that needs a refresh via
    "WNBA Update.command"), never another date's games.
    """
    date_param  = request.args.get("date", today_et())
    date_nodash = date_param.replace("-", "")               # YYYYMMDD
    if len(date_nodash) == 8 and date_nodash.isdigit():
        date_iso = f"{date_nodash[:4]}-{date_nodash[4:6]}-{date_nodash[6:]}"
    else:
        date_iso = date_param                                # assume already ISO

    cache_key = f"schedule_{date_iso}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    # Exact-date match only — no cross-day bleed, ever.
    games = _seed_schedule(date_iso)

    result = {
        "date":   date_iso,
        "games":  games,
        "source": "seed",
    }
    ttl = 120 if date_iso == today_et() else 3600
    cache_set(cache_key, result, ttl=ttl)
    return jsonify(result)

# ── /wnba/standings ────────────────────────────────────────────────────────────
_CONFERENCES = {
    "NYL": "Eastern", "CON": "Eastern", "WAS": "Eastern", "ATL": "Eastern",
    "CHI": "Eastern", "IND": "Eastern", "TOR": "Eastern",
    "LVA": "Western", "SEA": "Western", "PHX": "Western", "MIN": "Western",
    "DAL": "Western", "GSV": "Western", "POR": "Western", "UTA": "Western",
}

def _derive_standings() -> list[dict]:
    """
    Derive team records from the seed data:
      1. Final-scored games in the bundle's `games` list (status_code == 3).
      2. Remaining matchups inferred from game logs: rows sharing a
         (game_date, team vs opponent) pair form a game; the side whose players
         sum more points wins (player points sum to the team total).
    """
    wins: dict[str, int] = {}
    losses: dict[str, int] = {}

    def _win(abbr: str) -> None:
        abbr = norm(abbr)
        if abbr:
            wins[abbr] = wins.get(abbr, 0) + 1

    def _loss(abbr: str) -> None:
        abbr = norm(abbr)
        if abbr:
            losses[abbr] = losses.get(abbr, 0) + 1

    seen_matchups: set[tuple[str, str, str]] = set()

    # 1. Explicit final-scored games from the bundle
    for gdate, games in _seed.games_by_date.items():
        for g in games:
            if int(g.get("status_code") or 0) != 3:
                continue
            home = norm(g.get("home", ""))
            away = norm(g.get("away", ""))
            if not home or not away:
                continue
            hs, as_ = int(g.get("home_score") or 0), int(g.get("away_score") or 0)
            if hs == as_:
                continue
            seen_matchups.add((gdate, home, away))
            if hs > as_:
                _win(home); _loss(away)
            else:
                _win(away); _loss(home)

    # 2. Infer remaining results from summed player points per matchup
    team_pts: dict[tuple[str, str, str], dict[str, float]] = {}
    for rows in _seed.logs_by_player.values():
        for l in rows:
            team  = norm(l.get("team", ""))
            opp   = norm(l.get("opponent", ""))
            gdate = l.get("game_date", "")
            pts   = l.get("pts")
            if not team or not opp or not gdate or pts is None:
                continue
            bucket = team_pts.setdefault((gdate, team, opp), {})
            try:
                bucket[team] = bucket.get(team, 0.0) + float(pts)
                if opp not in bucket:
                    bucket[opp] = 0.0
            except (TypeError, ValueError):
                continue

    for (gdate, team, opp), pts in team_pts.items():
        if ((gdate, team, opp) in seen_matchups
                or (gdate, opp, team) in seen_matchups):
            continue
        tp, op = pts.get(team, 0.0), pts.get(opp, 0.0)
        if tp <= 0 or op <= 0 or tp == op:
            continue
        if tp > op:
            _win(team); _loss(opp)
        else:
            _win(opp); _loss(team)

    entries = []
    for abbr in sorted(set(wins) | set(losses)):
        w, l = wins.get(abbr, 0), losses.get(abbr, 0)
        total = w + l
        conf = _CONFERENCES.get(abbr, "")
        entries.append({
            "team_abbreviation": abbr,
            "conference":        f"{conf} Conference" if conf else "",
            "wins":              w,
            "losses":            l,
            "pct":               round(w / total, 3) if total else 0.0,
            "home_record":       "",
            "road_record":       "",
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

    entries = _derive_standings()
    result = {"standings": entries, "source": "seed_derived"}
    cache_set("standings", result, ttl=600)
    return jsonify(result)


# ── /wnba/team_advanced ────────────────────────────────────────────────────────
@app.route("/wnba/team_advanced")
def team_advanced():
    """
    Team-level advanced metrics used to come from stats.wnba.com at request
    time; live upstream calls have been removed. Returns the same shape with an
    empty team list — the app treats this as "no advanced data".
    """
    season = request.args.get("season", CURRENT_SEASON)
    return jsonify({
        "season": int(season) if str(season).isdigit() else season,
        "teams": [],
    })

# ── /wnba/team_position_splits ─────────────────────────────────────────────────
@app.route("/wnba/team_position_splits")
def team_position_splits():
    """Defensive allowed-stat splits by offensive position bucket, derived
    entirely from the seed game logs."""
    season = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 120))
    except (ValueError, TypeError):
        days = 120
    days = max(30, min(days, 365))
    try:
        min_sample = max(1, min(int(request.args.get("min_sample", 20)), 500))
    except (ValueError, TypeError):
        min_sample = 20

    cache_key = f"team_pos_splits_{season}_{days}_{min_sample}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    pos_by_pid: dict[str, str] = {
        pid: (p.get("pos", "") or "")
        for pid, p in _seed.players_by_id.items()
    }

    def pos_group(raw_pos: str) -> str:
        s = (raw_pos or "").upper()
        if "G" in s:
            return "GUARD"
        if "C" in s and "F" not in s:
            return "BIG"
        return "WING"

    agg: dict[tuple[str, str], dict] = {}

    for rows in _seed.logs_by_player.values():
        for row in rows:
            if row.get("game_date", "") < cutoff:
                continue
            opp = norm(row.get("opponent") or "")
            pid = str(row.get("player_id") or "")
            if not opp or not pid:
                continue
            group = pos_group(pos_by_pid.get(pid, ""))
            key = (opp, group)
            bucket = agg.setdefault(key, {
                "count": 0,
                "pts": 0.0, "reb": 0.0, "ast": 0.0,
                "three_p": 0.0, "stl": 0.0, "blk": 0.0, "pra": 0.0,
                "n_pts": 0, "n_reb": 0, "n_ast": 0,
                "n_three_p": 0, "n_stl": 0, "n_blk": 0, "n_pra": 0,
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

            add("pts", row.get("pts"))
            add("reb", row.get("reb"))
            add("ast", row.get("ast"))
            add("three_p", row.get("three_p"))
            add("stl", row.get("stl"))
            add("blk", row.get("blk"))
            p_, r_, a_ = row.get("pts"), row.get("reb"), row.get("ast")
            if p_ is not None and r_ is not None and a_ is not None:
                add("pra", float(p_) + float(r_) + float(a_))
            bucket["count"] += 1

    def avg(total_key: str, n_key: str, bucket: dict):
        n = bucket.get(n_key, 0)
        if n <= 0:
            return None
        return round(bucket.get(total_key, 0.0) / n, 2)

    out = []
    for (team_abbr, group), bucket in agg.items():
        if bucket["count"] < min_sample:
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

# ── /wnba/roster and /wnba/stats ───────────────────────────────────────────────
@app.route("/wnba/stats")
def stats():
    season = request.args.get("season", CURRENT_SEASON)
    return jsonify({"players": _all_players(season)})


@app.route("/wnba/roster")
def roster():
    season = request.args.get("season", CURRENT_SEASON)
    return jsonify({"players": _all_players(season)})


# ── /wnba/lineups ──────────────────────────────────────────────────────────────
@app.route("/wnba/lineups")
def lineups():
    """
    Projected starting lineups for a date's games (default: today, ET).
    Starters are each team's top-5 players by season minutes from the seed.
    """
    date_param  = request.args.get("date", today_et())
    date_nodash = date_param.replace("-", "")
    if len(date_nodash) == 8 and date_nodash.isdigit():
        date_iso = f"{date_nodash[:4]}-{date_nodash[4:6]}-{date_nodash[6:]}"
    else:
        date_iso = date_param

    cache_key = f"lineups_{date_iso}"
    if (cached := cache_get(cache_key)):
        return jsonify(cached)

    games = _seed_schedule(date_iso)
    if not games:
        return jsonify({"date": date_iso, "rows": []})

    # Team → players ordered by total minutes this season (descending)
    team_minutes: dict[str, dict[str, float]] = {}
    for pid, rows in _seed.logs_by_player.items():
        for l in rows:
            team = norm(l.get("team", ""))
            if not team:
                continue
            mp = l.get("mp_seconds") or 0.0
            team_minutes.setdefault(team, {})
            team_minutes[team][pid] = team_minutes[team].get(pid, 0.0) + float(mp)

    all_players = _all_players()
    by_id = {p["player_id"]: p for p in all_players}

    def build_lineup(team_abbr: str) -> list:
        mins = team_minutes.get(team_abbr, {})
        top5 = sorted(mins.items(), key=lambda kv: kv[1], reverse=True)[:5]
        lineup = []
        for pid, _ in top5:
            p = by_id.get(pid)
            if not p:
                continue
            lineup.append({
                "player_id": p["player_id"],
                "name":      p["name"],
                "position":  p.get("pos", ""),
                "team":      team_abbr,
                "status":    "Active",
                "source":    "projected",
            })
        return lineup

    rows = []
    for g in games:
        away_abbr = norm(g.get("away", ""))
        home_abbr = norm(g.get("home", ""))
        if not away_abbr or not home_abbr:
            continue
        rows.append({
            "game_id":     str(g.get("game_id") or ""),
            "date":        date_iso,
            "away":        away_abbr,
            "home":        home_abbr,
            "time":        g.get("tip") or "",
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
    days = max(1, min(days, 400))
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

    logs = _query_seed_logs([player_id], cutoff).get(player_id, [])
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
    """
    ids_raw = request.args.get("player_ids", "").strip()
    if not ids_raw:
        return jsonify({"error": "player_ids is required"}), 400

    season = request.args.get("season", CURRENT_SEASON)
    try:
        days = int(request.args.get("days", 60))
    except (ValueError, TypeError):
        days = 60
    days = max(1, min(days, 400))
    start_date = request.args.get("start_date", "").strip()

    # Cap to a reasonable number to prevent abuse
    player_ids = [p.strip() for p in ids_raw.split(",") if p.strip()][:150]

    if start_date:
        try:
            cutoff = datetime.strptime(start_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "start_date must be YYYY-MM-DD"}), 400
    else:
        cutoff = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")

    logs_by_player = _query_seed_logs(player_ids, cutoff)
    return jsonify({
        "season": int(season) if str(season).isdigit() else season,
        "logs_by_player": logs_by_player,
    })


# ── /wnba/box_score ────────────────────────────────────────────────────────────
@app.route("/wnba/box_score")
def box_score():
    """
    Live ESPN box scores were removed from the server (ESPN blocks datacenter
    IPs). The app falls back to its local seed-derived heuristic when the box
    is empty, so we return the same response shape with no data.
    """
    game_id = request.args.get("game_id", "").strip()
    return jsonify({
        "game_id": game_id,
        "boxscore": {},
        "players": [],
        "team_stats": [],
        "source": "unavailable",
    })


# ── /wnba/seed_import ─────────────────────────────────────────────────────────
@app.route("/wnba/seed_import")
def seed_import():
    """
    Manually reload StarterLogs.json into memory.
    Useful right after a deploy if you want to force a fresh read without
    restarting the service. Requires an X-API-Key header matching
    WNBA_SYNC_KEY (or WNBA_API_KEY).
    """
    ok, code, err = _admin_authorized()
    if not ok:
        return jsonify({"status": "error", "error": err}), code

    loaded = _seed.load()
    if not loaded:
        return jsonify({"status": "error", "error": "seed load failed"}), 500
    earliest, latest = _seed.date_range()
    return jsonify({
        "status": "ok",
        "players": len(_seed.players_by_id),
        "games": sum(len(v) for v in _seed.games_by_date.values()),
        "log_rows": _seed.total_log_rows(),
        "earliest_game_date": earliest,
        "latest_game_date": latest,
    })


# ── /wnba/data_status ─────────────────────────────────────────────────────────
@app.route("/wnba/data_status")
def data_status():
    """Diagnostics on the served seed bundle (freshness, coverage)."""
    earliest, latest = _seed.date_range()
    return jsonify({
        "season": int(CURRENT_SEASON) if str(CURRENT_SEASON).isdigit() else CURRENT_SEASON,
        "players": len(_seed.players_by_id),
        "games": sum(len(v) for v in _seed.games_by_date.values()),
        "total_rows": _seed.total_log_rows(),
        "distinct_players": len(_seed.logs_by_player),
        "earliest_game_date": earliest,
        "latest_game_date": latest,
        "bundle_date": _seed.meta.get("date"),
        "generated_at": _seed.meta.get("generated_at"),
        "source_path": _seed.source_path,
        "loaded_at": _seed.loaded_at,
    })


# ── Entry point ────────────────────────────────────────────────────────────────
# Load the seed bundle once at boot; /wnba/seed_import can refresh it later.
if os.environ.get("WNBA_DISABLE_SEED_LOAD", "").lower() not in ("1", "true", "yes"):
    _seed.load()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
