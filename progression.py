"""Progression rules: player levels, match rewards, faction reputation.

The shipped client only DISPLAYS progression (docs/match_protocol.md section 14): the lobby
serves experience (query 8: total / next level / previous level), money and skill points
(query 7), reputation points per faction (query 11) and per-trader price lists with a
reputation level per item (query 6). How experience, money and reputation are earned is the
server's business and the retail balance is not known, so the numbers here are invented,
documented defaults. Every one can be changed in data/progression.json (``--progression``)
and scaled with ``--reward-scale``.

Level n starts at ``xp_first_level + xp_level_step * (n - 2)`` experience more than level n-1
(level 1 = 0 xp), up to ``max_level``; each level reached grants ``skill_points_per_level``.

A finished lobby match is turned into a reward by ``Progression.reward``. The match server
reports one result per roster player (match/match_state.py ``player_result``):

    team, won, draw, finished, present_at_end, kills, deaths, items_stored, play_s
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("poc.progression")

DEFAULT_CONFIG: dict = {
    "note": "Invented balance (the retail numbers are not in the client). experience, money and "
            "reputation of a match = base + per_kill * kills + per_item * victory items stored "
            "+ win / draw bonus, times the completion factor.",
    "levels": {"max_level": 30, "xp_first_level": 500, "xp_level_step": 200,
               "skill_points_per_level": 1},
    "match": {
        # a player who was in the round less than this long earns nothing; one who left before
        # the end (or whose match never finished) earns this fraction and no win/draw bonus
        "min_play_s": 60,
        "abandon_factor": 0.5,
        "experience": {"base": 120, "per_kill": 25, "per_item": 40, "win": 150, "draw": 60},
        "money": {"base": 250, "per_kill": 60, "per_item": 120, "win": 400, "draw": 150},
        # reputation points per faction id (1 Scavengers, 2 Black Market, 3 Renaissance,
        # 4 Border, 5 Scientists, 6 Mercenaries); only traders 1 and 2 sell anything
        "reputation": {
            "1": {"base": 45, "per_kill": 6, "per_item": 10, "win": 20},
            "2": {"base": 25, "per_kill": 12, "per_item": 15, "win": 15},
            "3": {"base": 10, "per_kill": 8, "per_item": 0, "win": 30},
            "4": {"base": 10, "per_kill": 3, "per_item": 20, "win": 10},
            "5": {"base": 5, "per_kill": 0, "per_item": 25, "win": 5},
            "6": {"base": 10, "per_kill": 10, "per_item": 0, "win": 0},
        },
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Reward:
    experience: int = 0
    money: int = 0
    reputation: dict[int, int] = field(default_factory=dict)
    factor: float = 1.0
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.experience or self.money or any(self.reputation.values()))


class Progression:
    def __init__(self, config: dict | None = None, scale: float = 1.0):
        self.config = _merge(DEFAULT_CONFIG, config or {})
        self.scale = max(0.0, float(scale))
        lv = self.config["levels"]
        self.max_level = max(1, int(lv["max_level"]))
        self.skill_points_per_level = int(lv["skill_points_per_level"])
        self._start = [0, 0]                       # _start[n] = experience at which level n starts
        for n in range(2, self.max_level + 1):
            step = int(lv["xp_first_level"]) + int(lv["xp_level_step"]) * (n - 2)
            self._start.append(self._start[-1] + step)

    # --- levels -------------------------------------------------------------------------
    def level_for(self, experience: int) -> int:
        level = 1
        for n in range(2, self.max_level + 1):
            if experience >= self._start[n]:
                level = n
        return level

    def bounds(self, experience: int) -> tuple[int, int]:
        """(experience at the start of the current level, at the start of the next one).
        At the top level both are the same, which the client shows as a full bar."""
        level = self.level_for(experience)
        nxt = self._start[level + 1] if level < self.max_level else self._start[level]
        return self._start[level], nxt

    def level_start(self, level: int) -> int:
        return self._start[max(1, min(level, self.max_level))]

    # --- match rewards ------------------------------------------------------------------
    def reward(self, result: dict) -> Reward:
        m = self.config["match"]
        play_s = float(result.get("play_s", 0))
        if play_s < float(m["min_play_s"]):
            return Reward(reason=f"in the round {play_s:.0f}s < {m['min_play_s']}s")
        complete = bool(result.get("finished")) and bool(result.get("present_at_end"))
        factor = 1.0 if complete else float(m["abandon_factor"])
        kills = max(0, int(result.get("kills", 0)))
        items = max(0, int(result.get("items_stored", 0)))
        won = complete and bool(result.get("won"))
        draw = complete and bool(result.get("draw"))

        def amount(rule: dict, scale: float) -> int:
            v = rule["base"] + rule["per_kill"] * kills + rule["per_item"] * items \
                + (rule["win"] if won else rule["draw"] if "draw" in rule and draw else 0)
            return int(round(v * factor * scale))

        rep = {int(f): amount({"draw": 0, **rule}, self.scale) for f, rule in m["reputation"].items()}
        return Reward(amount(m["experience"], self.scale), amount(m["money"], self.scale),
                      {f: v for f, v in rep.items() if v}, factor,
                      "won" if won else "draw" if draw else "completed" if complete else "left early")


def load(path: Path | None, scale: float = 1.0) -> Progression:
    """data/progression.json overrides DEFAULT_CONFIG key by key; a missing or unreadable
    file leaves the defaults."""
    config = None
    if path is not None and path.is_file():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("cannot read %s (%r); using the built-in progression rules", path, e)
    return Progression(config, scale)
