#!/usr/bin/env python3
"""
Tests for the seed-only server (server/app.py).

The server no longer calls ESPN / stats.wnba.com / balldontlie at request
time — everything is served from StarterLogs.json. These tests cover:
  • exact-date schedule filtering (no cross-day bleed from stale bundles)
  • standings derivation from final scores + log-summed points
  • player logs (single + bulk) window filtering
  • lineups built from top-minutes players
  • degraded endpoints keep their response shapes
  • admin auth on /wnba/seed_import
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Isolate from any real StarterLogs.json on disk: the fixture must exist and
# be pointed at via env BEFORE importing app (candidate list frozen at import).
_tmpdir = tempfile.mkdtemp(prefix="wnba_seed_test_")
ET = ZoneInfo("America/New_York")


def _iso(days_from_today: int) -> str:
    return (datetime.now(ET) + timedelta(days=days_from_today)).strftime("%Y-%m-%d")


SEED = {
    "season": 2026,
    "date": _iso(-1),
    "generated_at": "2026-08-21T05:00:00+00:00",
    "note": "test bundle",
    "source_status": {},
    "players": [
        {"player_id": "1", "name": "Guard One",   "team": "NYL", "pos": "G"},
        {"player_id": "2", "name": "Guard Two",   "team": "NYL", "pos": "G"},
        {"player_id": "3", "name": "Center One",  "team": "MIN", "pos": "C"},
        {"player_id": "4", "name": "Forward One", "team": "MIN", "pos": "F"},
        {"player_id": "5", "name": "Wing One",    "team": "SEA", "pos": "F"},
    ],
    "games": [
        # Final game three days ago: NYL beat MIN 80-70
        {"game_id": "g1", "date": _iso(-3), "away": "MIN", "home": "NYL",
         "tip": "7:00 PM ET", "status": "Final", "status_code": 3,
         "home_score": 80, "away_score": 70},
        # Upcoming game tomorrow
        {"game_id": "g2", "date": _iso(1), "away": "SEA", "home": "NYL",
         "tip": "6:00 PM ET", "status": "Scheduled", "status_code": 1,
         "home_score": 0, "away_score": 0},
    ],
    "logs": [
        # NYL vs MIN (3 days ago): NYL players sum 80, MIN players sum 70
        {"season": 2026, "player_id": "1", "game_date": _iso(-3), "team": "NYL",
         "opponent": "MIN", "mp_seconds": 1200, "pts": 30.0, "reb": 3.0,
         "ast": 5.0, "three_p": 2.0, "ftm": 4.0, "fga": 15.0, "fta": 5.0,
         "stl": 1.0, "blk": 0.0, "tov": 2.0},
        {"season": 2026, "player_id": "2", "game_date": _iso(-3), "team": "NYL",
         "opponent": "MIN", "mp_seconds": 1100, "pts": 50.0, "reb": 2.0,
         "ast": 8.0, "three_p": 1.0, "ftm": 3.0, "fga": 20.0, "fta": 4.0,
         "stl": 2.0, "blk": 0.0, "tov": 1.0},
        {"season": 2026, "player_id": "3", "game_date": _iso(-3), "team": "MIN",
         "opponent": "NYL", "mp_seconds": 1300, "pts": 45.0, "reb": 10.0,
         "ast": 2.0, "three_p": 0.0, "ftm": 5.0, "fga": 18.0, "fta": 6.0,
         "stl": 0.0, "blk": 3.0, "tov": 3.0},
        {"season": 2026, "player_id": "4", "game_date": _iso(-3), "team": "MIN",
         "opponent": "NYL", "mp_seconds": 1000, "pts": 25.0, "reb": 6.0,
         "ast": 4.0, "three_p": 2.0, "ftm": 1.0, "fga": 12.0, "fta": 2.0,
         "stl": 1.0, "blk": 1.0, "tov": 2.0},
        # Older SEA game (10 days ago) for minutes accumulation
        {"season": 2026, "player_id": "5", "game_date": _iso(-10), "team": "SEA",
         "opponent": "CON", "mp_seconds": 1400, "pts": 22.0, "reb": 5.0,
         "ast": 3.0, "three_p": 3.0, "ftm": 2.0, "fga": 14.0, "fta": 3.0,
         "stl": 2.0, "blk": 0.0, "tov": 1.0},
    ],
}


# Write fixture and point the server at it BEFORE importing app (the seed
# candidate list is frozen at import time).
_SEED_PATH = os.path.join(_tmpdir, "seed.json")
with open(_SEED_PATH, "w") as _f:
    json.dump(SEED, _f)
os.environ["WNBA_STARTER_LOGS_PATH"] = _SEED_PATH
os.environ["WNBA_STARTER_LOGS_URL"] = ""          # never hit the network
os.environ["WNBA_DISABLE_SEED_LOAD"] = "1"        # we load explicitly below

app_module = importlib.import_module("app")


def setUpModule():
    assert app_module._seed.load()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_exact_date_returns_only_that_dates_games(self):
        r = self.client.get(f"/wnba/schedule?date={_iso(1)}")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["date"], _iso(1))
        self.assertTrue(all(g["date"] == _iso(1) for g in data["games"]))
        self.assertEqual(len(data["games"]), 1)
        self.assertEqual(data["source"], "seed")

    def test_stale_bundle_never_leaks_other_days(self):
        """Requesting a date absent from the bundle returns an empty slate —
        it must NOT return some older date's games."""
        r = self.client.get(f"/wnba/schedule?date={_iso(0)}")   # today, not in bundle
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["games"], [])
        self.assertEqual(data["source"], "seed")

    def test_nodash_date_accepted(self):
        r = self.client.get("/wnba/schedule?date=" + _iso(1).replace("-", ""))
        data = r.get_json()
        self.assertEqual(data["date"], _iso(1))
        self.assertEqual(len(data["games"]), 1)

    def test_default_date_is_today_et(self):
        r = self.client.get("/wnba/schedule")
        self.assertEqual(r.get_json()["date"], _iso(0))


class StandingsTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_records_derived_from_final_scores(self):
        r = self.client.get("/wnba/standings")
        entries = {e["team_abbreviation"]: e for e in r.get_json()["standings"]}
        self.assertEqual(entries["NYL"]["wins"], 1)
        self.assertEqual(entries["NYL"]["losses"], 0)
        self.assertEqual(entries["MIN"]["losses"], 1)

    def test_shape_has_expected_keys(self):
        r = self.client.get("/wnba/standings")
        e = r.get_json()["standings"][0]
        for key in ("team_abbreviation", "conference", "wins", "losses",
                    "pct", "home_record", "road_record", "last_10", "streak"):
            self.assertIn(key, e)


class PlayerLogTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_single_player_logs_window(self):
        r = self.client.get("/wnba/player_logs?player_id=5&days=5")
        self.assertEqual(r.get_json()["logs"], [])   # only log is 10 days old

        r = self.client.get("/wnba/player_logs?player_id=5&days=30")
        data = r.get_json()
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["pts"], 22.0)

    def test_start_date_filter(self):
        r = self.client.get(f"/wnba/player_logs?player_id=5&start_date={_iso(-5)}")
        self.assertEqual(r.get_json()["logs"], [])

    def test_missing_player_id_is_400(self):
        self.assertEqual(
            self.client.get("/wnba/player_logs").status_code, 400)

    def test_bulk_logs(self):
        r = self.client.get("/wnba/player_logs_bulk?player_ids=1,2,5&days=30")
        data = r.get_json()
        self.assertIn("1", data["logs_by_player"])
        self.assertIn("5", data["logs_by_player"])
        self.assertNotIn("3", data["logs_by_player"])

    def test_bulk_requires_ids(self):
        self.assertEqual(
            self.client.get("/wnba/player_logs_bulk").status_code, 400)

    app_module.app.config["TESTING"] = True


class LineupTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_lineups_for_game_day(self):
        r = self.client.get(f"/wnba/lineups?date={_iso(1)}")
        rows = r.get_json()["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["home"], "NYL")
        self.assertEqual(row["away"], "SEA")
        away_names = [p["name"] for p in row["away_lineup"]]
        self.assertIn("Wing One", away_names)   # SEA's only player

    def test_no_games_means_empty_rows(self):
        r = self.client.get(f"/wnba/lineups?date={_iso(0)}")
        self.assertEqual(r.get_json()["rows"], [])


class DegradedEndpointTests(unittest.TestCase):
    """Endpoints that used live upstream APIs keep their shapes but serve
    empty/degraded payloads."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_box_score_empty_payload(self):
        r = self.client.get("/wnba/box_score?game_id=g1")
        data = r.get_json()
        self.assertEqual(data["game_id"], "g1")
        self.assertEqual(data["players"], [])
        self.assertIn("source", data)

    def test_team_advanced_empty(self):
        self.assertEqual(
            self.client.get("/wnba/team_advanced").get_json()["teams"], [])

    def test_roster_and_stats_serve_seed_players(self):
        for route in ("/wnba/roster", "/wnba/stats"):
            players = self.client.get(route).get_json()["players"]
            ids = {p["player_id"] for p in players}
            self.assertIn("1", ids)

    def test_position_splits_derived(self):
        splits = self.client.get("/wnba/team_position_splits?days=60&min_sample=1") \
                            .get_json()["splits"]
        self.assertTrue(any(s["team_abbreviation"] == "NYL" for s in splits))


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_seed_import_disabled_without_key(self):
        old = app_module.SYNC_API_KEY
        app_module.SYNC_API_KEY = ""
        try:
            self.assertEqual(
                self.client.get("/wnba/seed_import").status_code, 503)
        finally:
            app_module.SYNC_API_KEY = old

    def test_wrong_key_is_401_right_key_ok(self):
        old = app_module.SYNC_API_KEY
        app_module.SYNC_API_KEY = "sekrit"
        try:
            r = self.client.get("/wnba/seed_import",
                                headers={"X-API-Key": "wrong"})
            self.assertEqual(r.status_code, 401)
            r = self.client.get("/wnba/seed_import",
                                headers={"X-API-Key": "sekrit"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["status"], "ok")
        finally:
            app_module.SYNC_API_KEY = old

    def test_data_status_reports_bundle(self):
        data = self.client.get("/wnba/data_status").get_json()
        self.assertGreater(data["total_rows"], 0)
        self.assertEqual(data["bundle_date"], SEED["date"])
        self.assertIsNotNone(data["latest_game_date"])


if __name__ == "__main__":
    unittest.main()
