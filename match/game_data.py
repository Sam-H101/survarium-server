"""Static game data for the match server: items, respawn points, loadouts, tickets.

Reads F:/Software/survarium/game_data/json (override with SURVARIUM_GAME_DATA).
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import messages as M

log = logging.getLogger("match.data")

POC_DIR = Path(__file__).resolve().parent.parent
GAME_DATA_DIR = Path(os.environ.get("SURVARIUM_GAME_DATA",
                                    POC_DIR.parent / "game_data" / "json"))
RAW_DIR = GAME_DATA_DIR / "raw"           # every gameplay binary_config exported as JSON
DEFAULT_TICKETS_PATH = POC_DIR / "state" / "match_tickets.json"

# spec 2.5: weapons whose config has a chamber_a_round state -> one extra byte
CHAMBERED_WEAPON_CFGS = ("rem_700", "rem_870", "toz_122")

ITEM_WEAPON = "weapon"
ITEM_SIMPLE = "simple"        # u16 amount: ammo, medkit, oxygen tank, booby trap
ITEM_ARTEFACT = "artefact"    # 0 bytes
ITEM_NONE = "none"            # scopes etc.: must not be in a slot


@dataclass
class ItemInfo:
    dict_id: int
    cfg_name: str
    kind: str
    allowed_slots: Tuple[int, ...]
    clip_size: int = 0
    has_chamber: bool = False


@dataclass
class WeaponInfo:
    """gameplay/weapons/<w>.options: parameters (weapon_core_cook) + fsm states."""
    bullet_damage: float = 0.4
    bullet_pierce: float = 0.5
    rounds_per_minute: float = 600.0
    reload_time: float = 2.5
    magazine_capacity: int = 30
    fire_queue_types: Tuple[int, ...] = (-1,)     # -1 (0xff) = automatic, n = n-round queue
    has_chamber: bool = False
    chamber_on_reload: bool = False
    dispersion: Optional[dict] = None             # cfg "dispersion" (weapon_dispersion_params)
    recoil: Optional[dict] = None                 # cfg "recoil" (weapon_recoil_params)
    double_handed: bool = True                    # parameters.double_handed (weapon_core_cook.cpp:66)

    @property
    def fire_interval_ms(self) -> float:
        return 60000.0 / max(1.0, self.rounds_per_minute)


@dataclass
class AmmoInfo:
    """gameplay/weapons/ammo/*: data (bullet.cpp uses k_damage, k_arp, buck_shot)."""
    k_damage: float = 1.0
    k_arp: float = 1.0
    buck_shot: int = 1
    distance_m: float = 1000.0
    dispersion: float = 1.0                       # multiplies base_dispersion (dispersion_calculator.cpp:40)
    ricochet_angle_deg: float = 0.0               # bullet.cpp:52


@dataclass
class HitTypeParams:
    armor: float
    reduce: float
    absorption: float
    bdb: Tuple[Tuple[str, float], ...] = ()       # (body part, coefficient)


@dataclass
class BodyPartParams:
    name: str
    health: float
    regeneration_speed: float
    regeneration_timeout: float
    can_be_assigned: bool
    hit_types: Dict[str, HitTypeParams]
    thresholds: Tuple[Tuple[float, str, Tuple[int, ...]], ...]   # (value, target part, affects)


@dataclass
class ArmourMods:
    """player_parameters_modifyer: per body part (health, regeneration_speed,
    {hit_type: (armor, reduce, absorption)}), summed over the equipped items."""
    parts: Dict[str, Tuple[float, float, Dict[str, Tuple[float, float, float]]]]


@dataclass
class MedkitInfo:
    """gameplay/items/drugs/*: data (medkit::load)."""
    activity_time_ms: int
    delay_ms: int
    influences: Tuple[Tuple[str, float], ...]     # (body part, total health amount)
    remove_affects: Tuple[Tuple[str, int], ...]


@dataclass
class VictoryContainer:
    container_id: int
    team: int                  # wire game_team_id of the owner
    position: Tuple[float, float, float]


@dataclass
class RespawnPoint:
    point_id: int
    team: int                  # wire game_team_id (0 team_1, 1 team_2)
    priority: int
    position: Tuple[float, float, float]
    yaw: float


def _load(name: str):
    with open(GAME_DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


class GameData:
    def __init__(self) -> None:
        self.items: Dict[int, ItemInfo] = {}
        for it in _load("items.json")["items"]:
            cfg = it.get("cfg_name", "")
            if it.get("is_weapon"):
                kind = ITEM_WEAPON
            elif "/artefacts/" in cfg:
                kind = ITEM_ARTEFACT
            elif "/scopes/" in cfg:
                kind = ITEM_NONE
            else:
                kind = ITEM_SIMPLE
            self.items[it["dict_id"]] = ItemInfo(
                it["dict_id"], cfg, kind, tuple(it.get("allowed_slots") or ()),
                int(it.get("clip_size") or 0),
                kind == ITEM_WEAPON and any(c in cfg for c in CHAMBERED_WEAPON_CFGS))

        self.maps = {m["project_name"]: m for m in _load("maps.json")["maps"]}
        self.templates = _load("player_templates.json")["templates"]

        # combat data (M3): weapons, ammo, armour, drugs, human hit params
        self.weapons: Dict[int, WeaponInfo] = {}
        self.ammo: Dict[int, AmmoInfo] = {}
        self.armour: Dict[int, dict] = {}
        self.medkits: Dict[int, MedkitInfo] = {}
        for dict_id, info in self.items.items():
            raw = _load_raw(info.cfg_name)
            if raw is None:
                if info.kind == ITEM_WEAPON:
                    log.warning("no config for weapon %d (%s); using defaults", dict_id, info.cfg_name)
                    self.weapons[dict_id] = WeaponInfo(magazine_capacity=info.clip_size or 30)
                continue
            if info.kind == ITEM_WEAPON:
                self.weapons[dict_id] = _weapon_info(raw)
            elif "/ammo/" in info.cfg_name:
                d = raw.get("data", {})
                self.ammo[dict_id] = AmmoInfo(float(d.get("k_damage", 1)), float(d.get("k_arp", 1)),
                                              max(1, int(d.get("buck_shot", 1))),
                                              1000.0 * float(d.get("distance_coef", 1)),
                                              float(d.get("dispersion", 1)),
                                              float(d.get("ricochet_angle", 0)))
            elif "/armour/" in info.cfg_name and isinstance(raw.get("hit_params"), dict):
                self.armour[dict_id] = raw["hit_params"]
            elif "/drugs/" in info.cfg_name:
                d = raw.get("data", {})
                act = max(0.001, float(d.get("activity_time_sec", 1)))
                infl = d.get("influences") or []
                rem = d.get("remove_affects") or []
                self.medkits[dict_id] = MedkitInfo(
                    int(1000 * act), int(1000 * float(d.get("activation_delay_sec", 0))),
                    tuple((e["body_part"], float(e["amount"])) for e in infl),
                    tuple((e["body_part"], int(e["affect"])) for e in rem))
        self.body_parts = _hit_params(_load_raw("gameplay/hit_params/human_hit_params.options"))
        # character_dispersion_params / character_recoil_params of every match player
        self.character = (_load_raw("gameplay/players/default.player") or {}).get("player", {})

    def respawn_points(self, map_name: str) -> List[RespawnPoint]:
        m = self.maps.get(map_name) or self.maps["level_03"]
        out = []
        for p in m.get("respawn_points", []):
            out.append(RespawnPoint(p["point_id"], p["team"], p.get("priority", 0),
                                    tuple(p["position"]), float(p["rotation"][1])))
        return out

    def victory_containers(self, map_name: str) -> List[VictoryContainer]:
        m = self.maps.get(map_name) or self.maps["level_03"]
        return [VictoryContainer(int(c["id"]), int(c["team"]), tuple(c["position"]))
                for c in m.get("victory_items_containers", [])]

    def victory_spawners(self, map_name: str) -> List[Tuple[float, float, float]]:
        m = self.maps.get(map_name) or self.maps["level_03"]
        return [tuple(v["position"]) for v in m.get("victory_item_spawners", [])]

    def armour_mods(self, slots: Dict[int, M.ItemInstance]) -> ArmourMods:
        """player_parameters_modifyer_cook::translate_query over the profile slots. Its
        body-part lookup always succeeds (it searches right after inserting), so the
        per-hit-type armor/reduce/absorption are SUMMED over all items, and apply()
        then REPLACES the base hit-type parameters of that part with the sums."""
        parts: Dict[str, list] = {}
        for slot in sorted(slots):
            item = slots[slot]
            cfg = self.armour.get(item.dict_id)
            if not item.id or not cfg:
                continue
            for part, pcfg in cfg.items():
                entry = parts.setdefault(part, [0.0, 0.0, {}])
                entry[0] += float(pcfg.get("health", 0.0))
                entry[1] += float(pcfg.get("regeneration_speed", 0.0))
                for htype, h in (pcfg.get("hit_types") or {}).items():
                    a = entry[2].setdefault(htype, [0.0, 0.0, 0.0])
                    a[0] += float(h.get("armor", 0.0))
                    a[1] += float(h.get("reduce", 0.0))
                    a[2] += float(h.get("absorption", 0.0))
        return ArmourMods({k: (v[0], v[1], {t: tuple(x) for t, x in v[2].items()})
                           for k, v in parts.items()})

    def pick_respawn(self, map_name: str, team: int, rng: random.Random) -> RespawnPoint:
        points = [p for p in self.respawn_points(map_name) if p.team == team]
        if not points:
            points = self.respawn_points(map_name)
        front = [p for p in points if p.priority == 2] or points
        return rng.choice(front)

    def first_respawn(self, map_name: str, team: int) -> RespawnPoint:
        """Deterministic choice (lowest point_id of the team) for tests and M2."""
        points = sorted((p for p in self.respawn_points(map_name) if p.team == team),
                        key=lambda p: p.point_id)
        return points[0]

    # ------------------------------------------------------------ loadouts
    def slot_problem(self, slot: int, item: M.ItemInstance) -> Optional[str]:
        """Why this item cannot be sent in this slot (spec 2.2), or None."""
        if not 0 <= slot < M.MAX_SLOTS:
            return f"slot {slot} out of range"
        info = self.items.get(item.dict_id)
        if info is None:
            return f"unknown dict_id {item.dict_id} in slot {slot}"
        if info.kind == ITEM_NONE:
            return f"dict_id {item.dict_id} ({info.cfg_name}) cannot occupy a slot"
        if info.allowed_slots and slot not in info.allowed_slots:
            return f"dict_id {item.dict_id} not allowed in slot {slot}"
        if item.id == 0:
            return f"slot {slot} has item id 0 (counts as empty)"
        return None

    def validate_loadout(self, slots: Dict[int, M.ItemInstance]) -> Optional[str]:
        """Return an error string, or None if the loadout is safe to send (spec 2.2/2.3)."""
        for slot, item in slots.items():
            err = self.slot_problem(slot, item)
            if err:
                return err
        if self.active_weapon_slot(slots) is None:
            return "no weapon in slot 7 or 10"
        return None

    def sanitize_loadout(self, slots: Dict[int, M.ItemInstance]
                         ) -> Tuple[Dict[int, M.ItemInstance], List[str]]:
        """Keep every slot that is safe to send and drop only the offenders, so one bad
        item (an unknown dict_id, a scope in a quick slot) does not cost the player the
        rest of the equipped loadout. Returns (slots, reasons for the dropped slots)."""
        kept: Dict[int, M.ItemInstance] = {}
        dropped: List[str] = []
        for slot in sorted(slots):
            err = self.slot_problem(slot, slots[slot])
            if err:
                dropped.append(err)
            else:
                kept[slot] = slots[slot]
        return kept, dropped

    def active_weapon_slot(self, slots: Dict[int, M.ItemInstance]) -> Optional[int]:
        """player::insert picks weapon1 if filled, else weapon2 (player.cpp:220-225)."""
        for s in (M.WEAPON1_SLOT, M.WEAPON2_SLOT):
            item = slots.get(s)
            if item and item.id and self.items.get(item.dict_id, None) and \
                    self.items[item.dict_id].kind == ITEM_WEAPON:
                return s
        return None

    def template_loadout(self, index: int) -> Dict[int, M.ItemInstance]:
        t = self.templates[index]
        slots = {}
        next_id = 1
        for name, v in t["slots"].items():
            slot = M.SLOT_IDS[name]
            info = self.items[v["dict_id"]]
            if info.kind == ITEM_WEAPON:
                cond, amount = self._magazine(slot, t["slots"]), 1
            elif slot in M.ARMOUR_SLOTS:
                cond, amount = 100, 1
            else:
                clip = info.clip_size or 1
                cond, amount = clip, clip * 3
            slots[slot] = M.ItemInstance(v["dict_id"], next_id, cond, amount)
            next_id += 1
        return slots

    def _magazine(self, weapon_slot: int, tslots: dict) -> int:
        ammo_name = "ammo1_weapon1_slot" if weapon_slot == M.WEAPON1_SLOT else "ammo1_weapon2_slot"
        ammo = tslots.get(ammo_name)
        if ammo:
            return self.items[ammo["dict_id"]].clip_size or 1
        return 1


def _load_raw(cfg_name: str) -> Optional[dict]:
    try:
        with open(RAW_DIR / (cfg_name + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _weapon_info(raw: dict) -> WeaponInfo:
    p = raw.get("parameters", {})
    states = raw.get("states", {})
    queues = tuple(int(q) for q in (p.get("fire_queue_types") or [-1]))
    return WeaponInfo(float(p.get("bullet_damage", 0.4)), float(p.get("bullet_pierce", 0.5)),
                      float(p.get("rounds_per_minute", 600)), float(p.get("reload_time", 2.5)),
                      int(p.get("magazine_capacity", 30)), queues or (-1,),
                      "chamber_a_round" in states, bool(p.get("chamber_a_round_on_reload")),
                      raw.get("dispersion"), raw.get("recoil"),
                      bool(p.get("double_handed", True)))


def _hit_params(raw: Optional[dict]) -> List[BodyPartParams]:
    """gameplay/hit_params/human_hit_params.options (damage_model_cook). The list is in
    config order, which is also the order of the client's damage_model::m_body_parts."""
    out: List[BodyPartParams] = []
    for e in (raw or {}).get("hit_params", []):
        hit_types = {}
        for name, h in (e.get("hit_types") or {}).items():
            bdb = tuple((k, float(v)) for k, v in (h.get("bdb_coeff") or {}).items())
            hit_types[name] = HitTypeParams(float(h.get("armor", 0)), float(h.get("reduce", 0)),
                                            float(h.get("absorption", 0)), bdb)
        thresholds = tuple((float(t["value"]), t["target_bodypart"], tuple(int(a) for a in t["affects"]))
                           for t in e.get("thresholds", []))
        out.append(BodyPartParams(e["name"], float(e["health"]), float(e.get("regeneration_speed", 0)),
                                  float(e.get("regeneration_timeout", 0)), bool(e.get("can_be_assigned")),
                                  hit_types, thresholds))
    return out


def default_loadout() -> Dict[int, M.ItemInstance]:
    """Spec 2.6 worked example: AK-74u (dict 13) in weapon1 + 5.45x39 FMJ (dict 7)."""
    return {
        M.WEAPON1_SLOT: M.ItemInstance(13, 1, 30, 1),
        8: M.ItemInstance(7, 2, 30, 90),
    }


def loadout_summary(slots: Dict[int, M.ItemInstance]) -> str:
    """'7:12 8:51 10:13 11:7' (slot:dict_id, armour slots 0..6 left out) for logs."""
    return " ".join(f"{s}:{i.dict_id}" for s, i in sorted(slots.items()) if s >= M.WEAPON1_SLOT)


DEFAULT_PLAYER_NAME = "Stalker"
DEFAULT_TEAM = M.TEAM_1


# ---------------------------------------------------------------- tickets
@dataclass
class Ticket:
    session_id: int
    name: str
    team: int                                  # wire game_team_id: 0 or 1
    slots: Dict[int, M.ItemInstance]
    account: str = ""
    match_id: int = 0
    from_lobby: bool = False
    boosters: Dict[int, M.Booster] = None
    roster: Optional[List[int]] = None         # lobby match: every session_id, in roster order

    def __post_init__(self) -> None:
        if self.boosters is None:
            self.boosters = {}


def _ticket_team(d: dict) -> int:
    """0x92 needs the wire game_team_id (0 team_1, 1 team_2).  The lobby ticket carries it
    as team_id (same value as op 51); the contract field team is 1|2 and is the fallback."""
    try:
        if d.get("team_id") is not None:
            t = int(d["team_id"])
            return t if t in (M.TEAM_1, M.TEAM_2) else DEFAULT_TEAM
        t = int(d.get("team"))
    except (TypeError, ValueError):
        return DEFAULT_TEAM
    return {1: M.TEAM_1, 2: M.TEAM_2}.get(t, DEFAULT_TEAM)


def _ticket_boosters(d: dict) -> Dict[int, M.Booster]:
    """Ticket boosters {booster_id: value} -> player_profile.boosters[11] by index id-1
    (the client reads boosters[i].id from the wire, so the index only selects the bit)."""
    out: Dict[int, M.Booster] = {}
    raw = d.get("boosters") or {}
    items = raw.items() if isinstance(raw, dict) else ((e.get("id"), e.get("value")) for e in raw)
    spill = []
    for k, v in items:
        try:
            bid, val = int(k), float(v)
        except (TypeError, ValueError):
            continue
        if not 0 < bid < 256:
            continue
        if 1 <= bid <= 11 and (bid - 1) not in out:
            out[bid - 1] = M.Booster(bid, val)
        else:
            spill.append(M.Booster(bid, val))
    for b in spill:
        free = next((i for i in range(11) if i not in out), None)
        if free is None:
            log.warning("more than 11 boosters in ticket; extra dropped")
            break
        out[free] = b
    return out


def _ticket_slots(d: dict) -> Dict[int, M.ItemInstance]:
    slots: Dict[int, M.ItemInstance] = {}
    if isinstance(d.get("loadout"), list):          # contract form
        for e in d["loadout"]:
            slot = int(e["slot"])
            slots[slot] = M.ItemInstance(int(e["dict_id"]), int(e["id"]),
                                         int(e.get("condition_or_stack", 0)),
                                         int(e.get("amount", e.get("amount_in_inventory", 0))))
    elif isinstance(d.get("slots"), dict):          # older lobby.py form
        for name, e in d["slots"].items():
            slot = int(e.get("slot_id", M.SLOT_IDS.get(name, -1)))
            slots[slot] = M.ItemInstance(int(e["dict_id"]), int(e.get("item_id", e.get("id", 0))),
                                         int(e.get("condition_or_stack", 0)),
                                         int(e.get("amount_in_inventory", e.get("amount", 0))))
    return slots


def ticket_from_dict(session_id: int, d: dict) -> Ticket:
    return Ticket(session_id=session_id,
                  name=str(d.get("profile_name") or d.get("account") or DEFAULT_PLAYER_NAME),
                  team=_ticket_team(d),
                  slots=_ticket_slots(d),
                  boosters=_ticket_boosters(d),
                  account=str(d.get("account", "")),
                  match_id=int(d.get("match_id", 0) or 0),
                  from_lobby=True,
                  roster=_ticket_roster(d))


def _ticket_roster(d: dict) -> Optional[List[int]]:
    raw = d.get("roster")
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [int(x) for x in raw][:M.MAX_PLAYERS]
    except (TypeError, ValueError):
        return None


def make_ticket_lookup(tickets_path: Optional[Path] = DEFAULT_TICKETS_PATH
                       ) -> Callable[[int], Optional[dict]]:
    """lobby.get_match_ticket(session_id) if importable in-process, else the JSON file."""
    fn = None
    try:
        import lobby  # type: ignore
        fn = getattr(lobby, "get_match_ticket", None)
    except Exception:
        fn = None

    def lookup(session_id: int) -> Optional[dict]:
        if fn is not None:
            try:
                d = fn(session_id)
                if d:
                    return d
            except Exception as exc:
                log.warning("lobby.get_match_ticket(%d) failed: %s", session_id, exc)
        if tickets_path and Path(tickets_path).exists():
            try:
                with open(tickets_path, encoding="utf-8") as f:
                    return json.load(f).get(str(session_id))
            except (OSError, ValueError) as exc:
                log.warning("cannot read %s: %s", tickets_path, exc)
        return None

    return lookup
