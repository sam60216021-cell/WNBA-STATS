#!/usr/bin/env python3
"""Unit tests for the balldontlie WNBA integration in server/app.py.

Run:  python3 -m unittest test_app -v   (from the server/ directory)

No network access and no API key required — all BDL responses are mocked.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

# Isolate the test DB (and ensure no real BDL key leaks in) BEFORE importing app.
os.environ["WNBA_DB_PATH"] = tempfile.mktemp(prefix="wnba_test_", suffix=".db")
os.environ.pop("BDL_API_KEY", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as srv  # noqa: E402


def _game(bdl_id, date, home, away, postseason=False, status="Final"):
    return {
        "id": bdl_id, "date": date, "season": 2026, "status": status,
        "postseason": postseason,
        "home_team": {"abbreviation": home, "id": 1},
        "visitor_team": {"abbreviation": away, "id": 2},
        "home_score": 85, "away_score": 80,
    }


def _stat(bdl_player_id, first, last, team_abbr, game, **over):
    row = {
        "player": {"id": bdl_player_id, "first_name": first, "last_name": last},
        "team": {"abbreviation": team_abbr},
        "game": game,
        "min": "31:42", "fgm": 8, "fga": 14, "fg3m": 3, "fg3a": 7,
        "ftm": 4, "fta": 4, "reb": 6, "ast": 5, "stl": 2, "blk": 1,
        "turnover": 3, "pts": 23,
    }
    row.update(over)
    return row


class MinParsingTests(unittest.TestCase):
    def test_plain_minutes(self):
        self.assertEqual(srv._parse_wnba_min("25"), 25 * 60)

    def test_mm_ss(self):
        self.assertEqual(srv._parse_wnba_min("25:42"), 25 * 60 + 42)

    def test_dnp_and_none(self):
        self.assertIsNone(srv._parse_wnba_min("DNP"))
        self.assertIsNone(srv._parse_wnba_min(None))
        self.assertIsNone(srv._parse_wnba_min(""))


class ThrottleTests(unittest.TestCase):
    def test_non_waiting_call_rejects_when_budget_spent(self):
        srv._bdl_last_request = time.time()
        self.assertFalse(srv._bdl_throttle(wait=False))

    def test_slot_free_after_interval(self):
        srv._bdl_last_request = time.time() - srv.BDL_MIN_INTERVAL - 0.01
        self.assertTrue(srv._bdl_throttle(wait=False))


class NameMapTests(unittest.TestCase):
    def setUp(self):
        srv._name_to_espn_id.clear()
        srv._bdl_id_to_espn.clear()

    def test_exact_full_name(self):
        srv._name_to_espn_id["tiffanyhayes"] = "1054"
        got = srv._espn_id_for_bdl_player({"id": 501, "first_name": "Tiffany", "last_name": "Hayes"})
        self.assertEqual(got, "1054")

    def test_unique_last_name_fallback(self):
        srv._name_to_espn_id["nnekaogwumike"] = "1068"
        got = srv._espn_id_for_bdl_player({"id": 502, "first_name": "N.", "last_name": "Ogwumike"})
        self.assertEqual(got, "1068")

    def test_unmapped_returns_none_and_is_cached(self):
        srv._name_to_espn_id["someoneelse"] = "1"
        self.assertIsNone(srv._espn_id_for_bdl_player({"id": 999, "first_name": "Rook", "last_name": "Player"}))
        self.assertIsNone(srv._espn_id_for_bdl_player({"id": 999, "first_name": "Rook", "last_name": "Player"}))


class BdlSyncTests(unittest.TestCase):
    """_sync_bdl_logs: page fetch → ESPN-ID mapping → SQLite upsert."""

    def setUp(self):
        srv._name_to_espn_id.clear()
        srv._bdl_id_to_espn.clear()
        # Synthetic ESPN IDs (9xxx) that cannot collide with real seed players
        srv._name_to_espn_id["tiffanyhayes"] = "9054"
        srv._name_to_espn_id["nnekaogwumike"] = "9068"
        srv._name_to_espn_id["emmecannon"] = "9331"

    def test_sync_maps_rows_opponents_and_skips(self):
        reg_game  = {"id": 901, "date": "2026-08-14T23:00:00Z", "season": 2026}
        post_game = {"id": 902, "date": "2026-09-20T23:00:00Z", "season": 2026}

        games = [
            _game(901, "2026-08-14T23:00:00Z", "LVA", "CON"),
            _game(902, "2026-09-20T23:00:00Z", "NYL", "CHI", postseason=True),
        ]
        stats = [
            _stat(11, "Tiffany", "Hayes", "LVA", reg_game),           # home → opp CON
            _stat(12, "Nneka", "Ogwumike", "CON", reg_game),          # away → opp LVA
            _stat(13, "Emme", "Cannon", "NYL", post_game),            # postseason → skip
            _stat(14, "Rook", "McNewbie", "CHI", reg_game, min=""),   # DNP → skip
            _stat(15, "Unknown", "Player", "CON", reg_game),          # unmapped → skip
        ]

        def fake_get_all(path, params=None, max_pages=25, wait=True):
            self.assertIn("start_date", params)
            self.assertIn("end_date", params)
            return games if path == "/games" else stats

        with mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "_bdl_get_all", side_effect=fake_get_all):
            res = srv._sync_bdl_logs("2026-08-01", "2026-08-15")

        self.assertEqual(res["games"], 2)
        self.assertEqual(res["rows"], 2)
        self.assertEqual(res["unmapped"], 1)   # Unknown Player

        got = srv._db_query_logs(["9054", "9068"], "2026-08-01")
        self.assertEqual(len(got.get("9054", [])), 1)
        self.assertEqual(len(got.get("9068", [])), 1)

        hayes = got["9054"][0]
        self.assertEqual(hayes["opponent"], "CON")
        self.assertEqual(hayes["team"], "LVA")
        self.assertEqual(hayes["pts"], 23.0)
        self.assertEqual(hayes["mp_seconds"], 31 * 60 + 42)
        self.assertEqual(hayes["three_p"], 3.0)
        self.assertEqual(hayes["tov"], 3.0)
        self.assertEqual(hayes["game_date"], "2026-08-14")

        ogwumike = got["9068"][0]
        self.assertEqual(ogwumike["opponent"], "LVA")

    def test_sync_without_key_is_a_noop(self):
        with mock.patch.object(srv, "BDL_API_KEY", ""):
            res = srv._sync_bdl_logs("2026-08-01", "2026-08-15")
        self.assertEqual(res, {"games": 0, "rows": 0, "unmapped": 0})


class BdlScheduleTests(unittest.TestCase):
    def test_shape_matches_espn_contract(self):
        payload = {"data": [
            _game(7001, "2026-08-15T23:00:00Z", "GSV", "LAS", status="Final"),
            _game(7002, "2026-08-15T21:00:00Z", "SEA", "MIN", status="Scheduled"),
        ]}
        with mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "_bdl_get", return_value=payload):
            games = srv._bdl_schedule("2026-08-15")

        self.assertEqual(len(games), 2)
        final, sched = games[0], games[1]
        self.assertEqual(final["home"], "GSV")
        self.assertEqual(final["away"], "LAS")
        self.assertEqual(final["status_code"], 3)      # finished
        self.assertEqual(final["game_type"], "regular")
        self.assertEqual(final["game_id"], "7001")
        self.assertEqual(sched["status_code"], 1)      # not started
        self.assertIn("ET", final["tip"])
        for key in ("game_id", "date", "away", "home", "tip", "status", "game_type",
                    "status_code", "home_score", "away_score", "period",
                    "missing_away_players", "missing_home_players"):
            self.assertIn(key, final)

    def test_no_key_returns_empty(self):
        with mock.patch.object(srv, "BDL_API_KEY", ""):
            self.assertEqual(srv._bdl_schedule("2026-08-15"), [])


class BdlStandingsTests(unittest.TestCase):
    def test_shape(self):
        rows = [{
            "team": {"abbreviation": "LVA"},
            "conference": "Western", "wins": 12, "losses": 4,
            "win_percentage": 0.75, "home_record": "7-1", "away_record": "5-3",
        }]
        with mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "_bdl_get_all", return_value=rows):
            entries = srv._bdl_standings("2026")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["team_abbreviation"], "LVA")
        self.assertEqual(e["conference"], "Western Conference")
        self.assertEqual(e["wins"], 12)
        self.assertEqual(e["home_record"], "7-1")
        self.assertEqual(e["last_10"], "")


class RouteTests(unittest.TestCase):
    def setUp(self):
        srv._cache.clear()
        srv._espn_reset_breaker()
        self.client = srv.app.test_client()

    def test_sync_route_requires_key(self):
        with mock.patch.object(srv, "BDL_API_KEY", ""), \
             mock.patch.object(srv, "SYNC_API_KEY", ""), \
             mock.patch.object(srv, "API_KEY", ""):
            resp = self.client.get("/wnba/sync")
        self.assertEqual(resp.status_code, 503)

    def test_sync_route_runs_sync(self):
        fake = {"games": 3, "rows": 40, "unmapped": 0}
        expected_start = (
            srv.datetime.now(srv.ET) - srv.timedelta(days=5)
        ).strftime("%Y-%m-%d")
        with mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "SYNC_API_KEY", "secret"), \
             mock.patch.object(srv, "API_KEY", ""), \
             mock.patch.object(srv, "_sync_bdl_logs", return_value=fake) as m:
            resp = self.client.get("/wnba/sync?days=5",
                                   headers={"X-API-Key": "secret"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["rows"], 40)
        self.assertEqual(m.call_args[0][0], expected_start)

    def test_scrape_alias_still_works(self):
        with mock.patch.object(srv, "BDL_API_KEY", ""):
            resp = self.client.get("/wnba/scrape")
        self.assertEqual(resp.status_code, 503)

    def test_schedule_falls_back_to_bdl_when_espn_blocked(self):
        bdl_payload = {"data": [_game(7001, "2026-05-17T23:00:00Z", "SEA", "MIN")]}
        with mock.patch.object(srv.espn, "get", side_effect=Exception("blocked")), \
             mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "_bdl_get", return_value=bdl_payload):
            resp = self.client.get("/wnba/schedule?date=2026-05-17")
        self.assertEqual(resp.status_code, 200)
        games = resp.get_json()["games"]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["home"], "SEA")
        self.assertEqual(games[0]["away"], "MIN")

    def test_data_status_reports_bdl_flag(self):
        with mock.patch.object(srv, "BDL_API_KEY", ""):
            resp = self.client.get("/wnba/data_status")
        body = resp.get_json()
        self.assertIn("bdl_configured", body)
        self.assertIn("sync_ready", body)
        self.assertFalse(body["bdl_configured"])


class AdminAuthTests(unittest.TestCase):
    """Admin endpoints require a shared secret even when read endpoints are open."""

    def setUp(self):
        srv._cache.clear()
        self.client = srv.app.test_client()

    def test_no_key_configured_disables_manual_sync(self):
        with mock.patch.object(srv, "SYNC_API_KEY", ""), \
             mock.patch.object(srv, "API_KEY", ""), \
             mock.patch.object(srv, "BDL_API_KEY", "test-key"):
            resp = self.client.get("/wnba/sync")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("WNBA_SYNC_KEY", resp.get_json()["error"])

    def test_wrong_key_is_401(self):
        with mock.patch.object(srv, "SYNC_API_KEY", "secret"), \
             mock.patch.object(srv, "API_KEY", ""), \
             mock.patch.object(srv, "BDL_API_KEY", "test-key"):
            resp = self.client.get("/wnba/sync", headers={"X-API-Key": "nope"})
        self.assertEqual(resp.status_code, 401)

    def test_falls_back_to_wnba_api_key(self):
        fake = {"games": 1, "rows": 2, "unmapped": 0}
        with mock.patch.object(srv, "SYNC_API_KEY", ""), \
             mock.patch.object(srv, "API_KEY", "shared"), \
             mock.patch.object(srv, "BDL_API_KEY", "test-key"), \
             mock.patch.object(srv, "_sync_bdl_logs", return_value=fake):
            resp = self.client.get("/wnba/sync", headers={"X-API-Key": "shared"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["rows"], 2)

    def test_seed_import_requires_key(self):
        with mock.patch.object(srv, "SYNC_API_KEY", "secret"), \
             mock.patch.object(srv, "API_KEY", ""):
            resp = self.client.get("/wnba/seed_import")
        self.assertEqual(resp.status_code, 401)


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        srv._espn_reset_breaker()

    def tearDown(self):
        srv._espn_reset_breaker()

    def test_opens_after_consecutive_failures(self):
        for _ in range(srv._ESPN_CB_THRESHOLD):
            srv._espn_note_failure()
        self.assertFalse(srv._espn_available())

    def test_success_resets_counter(self):
        srv._espn_note_failure()
        srv._espn_note_failure()
        srv._espn_note_success()
        srv._espn_note_failure()
        self.assertTrue(srv._espn_available())   # only 1 consecutive failure

    def test_open_breaker_skips_http(self):
        for _ in range(srv._ESPN_CB_THRESHOLD):
            srv._espn_note_failure()
        with mock.patch.object(srv.espn, "get") as fake:
            self.assertIsNone(srv._espn_get("https://example.invalid/x"))
            fake.assert_not_called()

    def test_espn_get_returns_json_and_resets(self):
        for _ in range(srv._ESPN_CB_THRESHOLD - 1):
            srv._espn_note_failure()
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"events": []}
        with mock.patch.object(srv.espn, "get", return_value=resp):
            self.assertEqual(srv._espn_get("https://example.invalid/x"), {"events": []})
        self.assertTrue(srv._espn_available())
        self.assertEqual(srv._espn_cb_failures, 0)


class DbFreshnessTests(unittest.TestCase):
    def test_empty_db_is_not_fresh(self):
        with mock.patch.object(srv, "_db_status", return_value={"latest_game_date": None}):
            self.assertFalse(srv._db_is_fresh())

    def test_recent_date_is_fresh(self):
        recent = srv.datetime.now(srv.timezone.utc).strftime("%Y-%m-%d")
        with mock.patch.object(srv, "_db_status", return_value={"latest_game_date": recent}):
            self.assertTrue(srv._db_is_fresh())


if __name__ == "__main__":
    unittest.main()

