"""Slow load tests (tools/loadtest.py against a real server process). Skipped unless
SURV_LOAD_TESTS=1, so the default `python -m unittest discover -s tests` stays quick:

    set SURV_LOAD_TESTS=1                        (PowerShell: $env:SURV_LOAD_TESTS=1)
    python -m unittest tests.test_load -v        (from poc-server/, ~4 minutes)

SURV_LOAD_PLAYERS overrides the player count of the big run (default 100).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))

from test_e2e import SSL_DIR  # noqa: E402

TICK_BUDGET_MS = 33.0


@unittest.skipUnless(os.environ.get("SURV_LOAD_TESTS"), "slow: set SURV_LOAD_TESTS=1")
@unittest.skipUnless((SSL_DIR / "survarium_login_server.key").is_file(), "game ssl dir missing")
class LoadTest(unittest.TestCase):
    def run_load(self, players: int, **kw) -> dict:
        import loadtest
        report = loadtest.run_loadtest(loadtest.single_run_args(players, quiet=True, **kw))
        loadtest.print_report(report)
        return report

    def check(self, r: dict, players: int) -> None:
        s = r["server_side"]
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["faults"], [])
        self.assertEqual(r["players_finished"], players)
        self.assertEqual(r["server_tracebacks"], 0)
        self.assertLess(s["tick_p99_worst_match_ms"], TICK_BUDGET_MS)
        self.assertLess(s["cycle_ms"]["p99"], TICK_BUDGET_MS)
        self.assertLess(abs(s["tick_interval_ms"]["p50"] - TICK_BUDGET_MS), 3.0)
        self.assertLess(s["loop_lag_ms"]["p99"], 100.0)
        self.assertLess(r["client"]["ping_ms"]["p99"], 250.0)

    def test_many_players_ten_per_match(self):
        n = int(os.environ.get("SURV_LOAD_PLAYERS", "100"))
        self.check(self.run_load(n, match_size=10, rounds=2, play_seconds=20), n)

    def test_five_matches_of_twenty(self):
        self.check(self.run_load(100, match_size=20, rounds=1, play_seconds=25), 100)


if __name__ == "__main__":
    unittest.main()
