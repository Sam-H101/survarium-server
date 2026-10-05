"""python -m match [--match-host 0.0.0.0] [--match-port 25103] [--tickets PATH] [-v]"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .game_data import DEFAULT_TICKETS_PATH
from .match_state import MatchConfig
from .server import DEFAULT_MATCH_PORT, start_match_server


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m match", description=__doc__)
    ap.add_argument("--match-host", default="0.0.0.0")
    ap.add_argument("--match-port", type=int, default=DEFAULT_MATCH_PORT)
    ap.add_argument("--tickets", type=Path, default=DEFAULT_TICKETS_PATH,
                    help="lobby ticket file (default state/match_tickets.json)")
    ap.add_argument("--reject-unknown", action="store_true",
                    help="reject session ids without a lobby ticket (default: default player)")
    ap.add_argument("--no-corrections", action="store_true", help="never send 0x82")
    ap.add_argument("--respawn-time", type=int, default=10)
    ap.add_argument("--match-time", type=int, default=600)
    ap.add_argument("--victory-items", type=int, default=3)
    ap.add_argument("--join-timeout", type=float, default=60.0)
    ap.add_argument("--friendly-fire", action="store_true")
    ap.add_argument("--open-match-rules", action="store_true",
                    help="ticketless sessions also get victory items, the timer and a match end")
    ap.add_argument("--no-world-collision", action="store_true",
                    help="ignore match/data/<map>.collision: walls do not stop bullets")
    ap.add_argument("--no-dispersion", action="store_true", help="shoot along the exact view")
    ap.add_argument("--no-recoil", action="store_true", help="do not replay the weapon recoil")
    ap.add_argument("--spread-growth", action="store_true",
                    help="apply the configs' one_shoot_dispersion_amount (the retail client zeroes it)")
    ap.add_argument("--client-terrain", action="store_true",
                    help="terrain follows its material like in the client (grass lets bullets through)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = MatchConfig(respawn_time=args.respawn_time, match_time=args.match_time,
                      send_corrections=not args.no_corrections,
                      accept_unknown_sessions=not args.reject_unknown,
                      victory_items_count=args.victory_items, join_timeout_s=args.join_timeout,
                      friendly_fire=args.friendly_fire, open_match_rules=args.open_match_rules,
                      world_collision=not args.no_world_collision,
                      dispersion=not args.no_dispersion, recoil=not args.no_recoil,
                      spread_growth_from_config=args.spread_growth,
                      solid_terrain=not args.client_terrain)

    async def run() -> None:
        server = await start_match_server(None, args.match_host, args.match_port, cfg,
                                          tickets_path=args.tickets)
        try:
            await asyncio.Event().wait()
        finally:
            server.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
