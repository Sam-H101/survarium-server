"""Survarium v0.100b match server (UDP, network_core::udp_match_connection port).

    from match import start_match_server, MatchConfig
    server = await start_match_server(loop, "0.0.0.0", 25103)

or standalone: ``python -m match --match-port 25103`` (run from poc-server/).
"""

from .match_state import MatchConfig, MatchCore
from .server import DEFAULT_MATCH_PORT, MatchServer, start_match_server

__all__ = ["start_match_server", "MatchServer", "MatchCore", "MatchConfig",
           "DEFAULT_MATCH_PORT"]
