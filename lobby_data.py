"""Static game data the lobby serves: items, slot rules, prices, factions, skills tree.

Sources, in order of preference:
  * resources/gameplay/db_static_dictionaries (vostok binary_config) from game_data/extracted/
    for items, factions, perks and boosters; FALLBACK_ITEMS below is a snapshot of it.
  * poc-server/data/*.json, copied from game_data/json/ (the data workstream's exports):
    player_templates.json (the real u16[11][13] starter loadouts from survarium.exe),
    lobby_static_tables.json (status 4/5), skills_tree.json/.bin (status 9) and
    shop_prices.json (status 6/10). The last two are invented balance, shared with the
    match server. Missing files fall back to the tables in this module.

Every dict_id the lobby sends MUST exist in the client's items dictionary:
items_dictionary::item_by_id is a bare map::find(...)->second, so an unknown id is a
client crash (profile_skin_visual_cook, relocate_item_func, player cook).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import binary_config as bc

log = logging.getLogger("poc.lobby")

# profile_slot_enum (game_core/profile_slot_enum.h)
HELMET, MASK, TORSO, BACK, PANTS, GLOVES, BOOTS = range(7)
WEAPON1, AMMO1_W1, AMMO2_W1, WEAPON2, AMMO1_W2, AMMO2_W2 = range(7, 13)
QUICK_SLOTS = tuple(range(13, 19))
MAX_SLOTS = 19
STORAGE_SLOT = 100          # lobby_client::can_move_item: target 100 is always allowed
AMMO_SLOTS = {AMMO1_W1: WEAPON1, AMMO2_W1: WEAPON1, AMMO1_W2: WEAPON2, AMMO2_W2: WEAPON2}

# item_category values seen in items_dict (dictionary_item::is_ammo lists 9, 18..21)
AMMO_CATEGORIES = (9, 18, 19, 20, 21)
PRIMARY_CATEGORIES = (13, 14, 15, 16)   # sniper rifle, shotgun, rifle/smg, (machine gun)
PISTOL_CATEGORIES = (17,)
QUICK_CATEGORIES = (10, 11, 12)         # drugs, traps, artefacts

SLOT_RULES: dict[int, tuple[int, ...]] = {
    HELMET: (1,), MASK: (2,), TORSO: (3,), BACK: (4,), PANTS: (5,), GLOVES: (6,), BOOTS: (7,),
    WEAPON1: PRIMARY_CATEGORIES + PISTOL_CATEGORIES, WEAPON2: PRIMARY_CATEGORIES + PISTOL_CATEGORIES,
    AMMO1_W1: AMMO_CATEGORIES, AMMO2_W1: AMMO_CATEGORIES,
    AMMO1_W2: AMMO_CATEGORIES, AMMO2_W2: AMMO_CATEGORIES,
    **{s: QUICK_CATEGORIES for s in QUICK_SLOTS},
}

# Snapshot of items_dict: dict_id -> (item_category, is_stack, cfg_name)
FALLBACK_ITEMS: dict[int, tuple[int, bool, str]] = {
    7: (9, True, "gameplay/weapons/ammo/ammo_5.45x39_fmj"),
    9: (4, False, "gameplay/items/armour/back/scavenger_oxygen_1"),
    12: (13, False, "gameplay/weapons/toz_122.options"),
    13: (15, False, "gameplay/weapons/ak_74u.options"),
    14: (13, False, "gameplay/weapons/rem_700.options"),
    15: (14, False, "gameplay/weapons/rem_870.options"),
    16: (14, False, "gameplay/weapons/toz_34.options"),
    17: (14, False, "gameplay/weapons/toz_66.options"),
    18: (17, False, "gameplay/weapons/tt_33.options"),
    19: (15, False, "gameplay/weapons/vityaz.options"),
    20: (20, True, "gameplay/weapons/ammo/ammo_.357m"),
    22: (19, True, "gameplay/weapons/ammo/ammo_12mm_buck"),
    24: (7, False, "gameplay/items/armour/boots/blackmarket_boots_1"),
    25: (6, False, "gameplay/items/armour/gloves/blackmarket_gloves_1"),
    27: (1, False, "gameplay/items/armour/helmet/blackmarket_helmet_1"),
    28: (5, False, "gameplay/items/armour/legs/blackmarket_legs_1"),
    29: (3, False, "gameplay/items/armour/torso/blackmarket_torso_1"),
    31: (3, False, "gameplay/items/armour/torso/scavenger_torso_1"),
    32: (3, False, "gameplay/items/armour/torso/blackmarket_torso_2"),
    33: (3, False, "gameplay/items/armour/torso/blackmarket_torso_3"),
    34: (3, False, "gameplay/items/armour/torso/scavenger_torso_2"),
    35: (7, False, "gameplay/items/armour/boots/blackmarket_boots_2"),
    36: (7, False, "gameplay/items/armour/boots/blackmarket_boots_3"),
    37: (7, False, "gameplay/items/armour/boots/scavenger_boots_1"),
    38: (7, False, "gameplay/items/armour/boots/scavenger_boots_2"),
    39: (7, False, "gameplay/items/armour/boots/scavenger_boots_3"),
    40: (6, False, "gameplay/items/armour/gloves/blackmarket_gloves_2"),
    41: (6, False, "gameplay/items/armour/gloves/scavenger_gloves_1"),
    42: (6, False, "gameplay/items/armour/gloves/scavenger_gloves_2"),
    43: (2, False, "gameplay/items/armour/mask/scavenger_resp_1"),
    44: (5, False, "gameplay/items/armour/legs/blackmarket_legs_2"),
    45: (5, False, "gameplay/items/armour/legs/scavenger_legs_1"),
    46: (5, False, "gameplay/items/armour/legs/scavenger_legs_2"),
    47: (5, False, "gameplay/items/armour/legs/scavenger_legs_3"),
    48: (3, False, "gameplay/items/armour/torso/scavenger_torso_3"),
    49: (4, False, "gameplay/items/armour/back/scavenger_back_1"),
    50: (20, True, "gameplay/weapons/ammo/ammo_7.62x25"),
    51: (18, True, "gameplay/weapons/ammo/ammo_7.62x51"),
    52: (18, True, "gameplay/weapons/ammo/ammo_7.62x51_ap"),
    53: (20, True, "gameplay/weapons/ammo/ammo_9x19p_fmj"),
    54: (12, False, "gameplay/items/artefacts/lifebone"),
    55: (15, False, "gameplay/weapons/uzi.options"),
    56: (17, False, "gameplay/weapons/magnum.options"),
    57: (12, False, "gameplay/items/artefacts/lifebone"),
    64: (17, False, "gameplay/weapons/fort_17.options"),
    65: (10, True, "gameplay/items/drugs/painkiller"),
    66: (10, True, "gameplay/items/drugs/bandages"),
    67: (10, True, "gameplay/items/drugs/medkit"),
    68: (11, True, "gameplay/items/base_trap"),
    69: (22, False, "gameplay/items/scopes/leupold"),
    70: (20, True, "gameplay/weapons/ammo/ammo_9x18_makarov"),
    71: (20, True, "gameplay/weapons/ammo/ammo_9x19p_hp"),
    72: (19, True, "gameplay/weapons/ammo/ammo_12mm_slug"),
    73: (19, True, "gameplay/weapons/ammo/ammo_12mm_buck2"),
}
FALLBACK_FACTION_LEVELS = {1: [100, 250, 400, 650, 1000], 2: [200, 500, 800], 3: [50, 450, 900, 1100],
                           4: [500, 1200], 5: [200, 800], 6: [300, 900]}

# Calibre table (weapon configs carry no ammo reference, so this is hand-made by name):
# weapon dict_id -> ammo dict_ids it accepts.
WEAPON_AMMO = {
    13: (7,),               # AK-74u      5.45x39
    19: (53, 71),           # Vityaz      9x19
    55: (53, 71),           # Uzi         9x19
    15: (22, 73, 72),       # Rem 870     12ga
    16: (22, 73, 72),       # TOZ-34      12ga
    17: (22, 73, 72),       # TOZ-66      12ga
    14: (51, 52),           # Rem 700     7.62x51
    12: (51, 52),           # TOZ-122     7.62x51
    18: (50,),              # TT-33       7.62x25
    64: (70,),              # Fort-17     9x18
    56: (20,),              # Magnum      .357
}

# Starting characters when data/player_templates.json is missing. The client has no
# create/rename op (lobby_client.h has none), so the server creates them on first
# sign-in. The client array holds 3 profiles max. (slot, dict_id, condition_or_stack)
FALLBACK_LOADOUTS: list[tuple[str, list[tuple[int, int, int]]]] = [
    ("", [  # assault (name = account nickname)
        (MASK, 43, 100), (TORSO, 31, 100), (BACK, 49, 100), (PANTS, 45, 100),
        (GLOVES, 41, 100), (BOOTS, 37, 100),
        (WEAPON1, 13, 100), (AMMO1_W1, 7, 120),
        (WEAPON2, 18, 100), (AMMO1_W2, 50, 50),
        (13, 66, 5), (14, 67, 2),
    ]),
    ("_cqb", [  # close quarters
        (HELMET, 27, 100), (TORSO, 29, 100), (PANTS, 28, 100), (GLOVES, 25, 100), (BOOTS, 24, 100),
        (WEAPON1, 15, 100), (AMMO1_W1, 22, 40), (AMMO2_W1, 72, 20),
        (WEAPON2, 64, 100), (AMMO1_W2, 70, 50),
        (13, 67, 3),
    ]),
    ("_sniper", [
        (TORSO, 34, 100), (PANTS, 46, 100), (GLOVES, 42, 100), (BOOTS, 38, 100),
        (WEAPON1, 14, 100), (AMMO1_W1, 51, 30),
        (WEAPON2, 56, 100), (AMMO1_W2, 20, 40),
        (13, 66, 5), (14, 65, 2),
    ]),
]
TEMPLATE_PROFILES = ((0, ""), (1, "_2"), (2, "_3"))   # player_templates index, name suffix
SLOT_NAMES = ["helmet_slot", "mask_slot", "torso_slot", "back_slot", "pants_slot", "gloves_slot",
              "boots_slot", "weapon1_slot", "ammo1_weapon1_slot", "ammo2_weapon1_slot", "weapon2_slot",
              "ammo1_weapon2_slot", "ammo2_weapon2_slot", "quick_slot1", "quick_slot2", "quick_slot3",
              "quick_slot4", "quick_slot5", "quick_slot6"]
# New accounts start without spare firearms: every weapon beyond the starter loadouts is earned
# (WEAPON_OFFERS) and bought. The scope (69) is the rem_700's addon item, not a usable one.
STARTING_STORAGE = [(53, 200), (7, 120), (65, 3), (68, 2), (54, 100), (69, 100), (32, 100), (44, 100),
                    (40, 100)]

# Weapon shop: dict_id -> (trader/faction id, reputation_level, cost). The retail shop window lists
# only the first two traders (GangShop.fillSellers: `param1.length = 2`), so every weapon is sold by
# trader 1 (Scavengers) or 2 (Black Market). reputation_level n needs the trader's reputation to
# reach factions_dict levels[n].value (n = 0: no requirement). Balance is invented, like
# data/shop_prices.json (generated from this table by tools/gen_shop_prices.py).
# Ammunition price PER ROUND (the shop tile shows price x clip_size, the buy dialog counts rounds).
# Cheap enough that a clip costs less than a match pays; AP / hollow point / slugs cost more.
AMMO_PRICES: dict[int, int] = {7: 10, 70: 8, 71: 14, 53: 10, 50: 10, 51: 16, 52: 24, 20: 15, 22: 12,
                               72: 16, 73: 14}

WEAPON_OFFERS: dict[int, tuple[int, int, int]] = {
    64: (1, 0, 600),     # Fort-17          pistol
    18: (1, 0, 800),     # TT-33            pistol
    16: (1, 0, 1500),    # TOZ-34           double-barrel shotgun   (starter loadout 2)
    17: (1, 1, 1800),    # TOZ-66           shotgun                 rep 250
    15: (1, 2, 2800),    # Remington 870    pump shotgun            rep 400
    13: (2, 0, 2500),    # AK-74u           rifle                   (starter loadout 1)
    12: (2, 0, 3000),    # TOZ-122          hunting rifle           (starter loadout 1)
    55: (2, 1, 3500),    # Uzi              sub-machine gun         rep 500
    56: (2, 1, 3000),    # Magnum           revolver                rep 500
    19: (2, 2, 4500),    # Vityaz           sub-machine gun         rep 800
    14: (2, 2, 5000),    # Remington 700    sniper rifle            rep 800 (starter loadout 3)
}

# skills_dict ids 1..5 and the boosters_dict ids each branch grants
SKILL_BOOSTERS = {1: (1, 2), 2: (4, 5, 6), 3: (11, 10), 4: (3, 7), 5: (9, 8)}
PERK_PREFIX_SKILL = {"st_snp_": 1, "st_phy_": 2, "st_eng_": 3, "st_med_": 4, "st_knw_": 5}
BOOSTER_STEP = 2.0  # value granted per level (boosters are "*_perc" corrections)


DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass
class Item:
    dict_id: int
    category: int
    is_stack: bool
    cfg_name: str
    weight: float = 1.0
    clip_size: int = 30


@dataclass
class GameData:
    items: dict[int, Item]
    faction_levels: dict[int, list[int]]
    perks_by_skill: dict[int, list[int]]
    boosters: set[int]
    source: str
    data_dir: Path | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.slot_rules: dict[int, tuple[int, ...]] = dict(SLOT_RULES)
        self._compat_list = [(w, a) for w, ammo in WEAPON_AMMO.items() for a in ammo]
        tables = self._json("lobby_static_tables.json")
        if tables:
            rules: dict[int, list[int]] = {}
            for r in tables["profile_slot_restrictions"]:
                rules.setdefault(r["slot_dict_id"], []).append(r["category_dict_id"])
            self.slot_rules = {k: tuple(v) for k, v in rules.items()}
            self._compat_list = [(r["first_item_dict_id"], r["second_item_dict_id"])
                                 for r in tables["items_compatibility"]]
        self._compat_list = [(a, b) for a, b in self._compat_list if a in self.items and b in self.items]
        self._compat = set(self._compat_list)

        tree = self._json("skills_tree.json")
        self.skills_tree = tree["tree"] if tree else build_skills_tree(self)
        self.skills_tree_blob = bc.dump(self.skills_tree)
        blob = self._bytes("skills_tree.bin")
        if blob is not None and bc.load(blob) == self.skills_tree:
            self.skills_tree_blob = blob            # the data workstream's validated serialisation
        self.perk_levels: dict[int, tuple[int, int]] = {}
        for branch in self.skills_tree.values():
            for lvl_name, lvl in branch["levels"].items():
                for perk in lvl.get("perks", []):
                    self.perk_levels[perk["id"]] = (branch["id"], int(lvl_name.rsplit("_", 1)[1]))

        shop = self._json("shop_prices.json")
        self.service_prices = (500, 0, 0)
        if shop:
            self.prices = {int(f): [(r["item_dict_id"], r["cost"], r["reputation_level"]) for r in rows]
                           for f, rows in shop["factions"].items()}
            sp = shop.get("service_prices", {})
            self.service_prices = (sp.get("reroll_cost", 500), sp.get("add_profile_cost", 0),
                                   sp.get("rename_account_cost", 0))
        else:
            self.prices = build_prices(self)
        # Nothing is sold that no slot can hold: the scope (dict 69, category 22) is only the
        # item the rem_700 config names in "addons"; the client has no way to attach one.
        self.prices = {f: [(d, c, lvl) for d, c, lvl in rows
                           if d in self.items and self.equippable(d)
                           and lvl < len(self.faction_levels.get(f, ()))]
                       for f, rows in self.prices.items() if f in self.faction_levels}

        self.loadouts = FALLBACK_LOADOUTS
        templates = self._json("player_templates.json")
        if templates:
            self.loadouts = [(suffix, self._template_loadout(templates["templates"][idx]["slots"]))
                             for idx, suffix in TEMPLATE_PROFILES]

    def _json(self, name: str):
        path = self.data_dir / name if self.data_dir else None
        if not path or not path.is_file():
            self.sources[name] = "built-in"
            return None
        self.sources[name] = str(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def _bytes(self, name: str) -> bytes | None:
        path = self.data_dir / name if self.data_dir else None
        return path.read_bytes() if path and path.is_file() else None

    def _template_loadout(self, slots: dict) -> list[tuple[int, int, int]]:
        """player_templates row -> (slot, dict_id, condition_or_stack). Weapons/armour get
        condition 100; ammo gets three clips."""
        out = []
        for slot_name, entry in slots.items():
            slot, d = SLOT_NAMES.index(slot_name), entry["dict_id"]
            if d in self.items:
                out.append((slot, d, self.items[d].clip_size * 3 if slot in AMMO_SLOTS else 100))
        return out

    # --- derived tables -----------------------------------------------------------------
    def slot_restrictions(self) -> list[tuple[int, int]]:
        return [(slot, cat) for slot, cats in sorted(self.slot_rules.items()) for cat in cats]

    def compatibilities(self) -> list[tuple[int, int]]:
        return list(self._compat_list)

    def equippable(self, dict_id: int) -> bool:
        """Some profile slot accepts this item's category (scopes: none does)."""
        item = self.items.get(dict_id)
        return item is not None and any(item.category in cats for cats in self.slot_rules.values())

    def slot_accepts(self, slot: int, dict_id: int) -> bool:
        if slot == STORAGE_SLOT:
            return True
        item = self.items.get(dict_id)
        return item is not None and item.category in self.slot_rules.get(slot, ())

    def compatible(self, a: int, b: int) -> bool:
        return (a, b) in self._compat or (b, a) in self._compat

    def item_label(self, dict_id: int) -> str:
        item = self.items.get(dict_id)
        return Path(item.cfg_name).name.removesuffix(".options") if item else f"item {dict_id}"

    # --- shop gating --------------------------------------------------------------------
    def reputation_threshold(self, faction: int, level: int) -> int | None:
        """Reputation points that reach `level` of a trader (factions_dict levels[n].value);
        None for a level the faction does not have, 0 for level 0 (no requirement)."""
        values = self.faction_levels.get(faction, ())
        if level <= 0:
            return 0
        return values[level] if level < len(values) else None

    def reputation_level(self, faction: int, points: int) -> int:
        """The client's own reading (lobby_menu::on_player_reputations_arrived): the highest
        level whose value the points reach, 0 when none does."""
        out = 0
        for i, value in enumerate(self.faction_levels.get(faction, ())):
            if points >= value:
                out = i
        return out

    def is_unlocked(self, faction: int, level: int, points: int) -> bool:
        need = self.reputation_threshold(faction, level)
        return need is not None and points >= need

    def offers(self, dict_id: int) -> list[tuple[int, int, int]]:
        """Every (trader, cost, reputation_level) selling this item, cheapest first."""
        rows = [(f, cost, lvl) for f, rows in sorted(self.prices.items())
                for d, cost, lvl in rows if d == dict_id]
        return sorted(rows, key=lambda r: (r[1], r[2], r[0]))

    def perk_level(self, perk: int) -> tuple[int, int] | None:
        return self.perk_levels.get(perk)

    def skill_levels(self, skill: int) -> int:
        return len(self.skills_tree.get(f"skill_{skill}", {}).get("levels", {}))

    def boosters_for(self, skills: dict[int, int]) -> dict[int, float]:
        """Booster id -> value for the given skill points (player_profile.boosters).
        The shared tree (data/skills_tree.json) lists each level's running total, so the
        highest level reached wins; the built-in tree grants one step per level."""
        cumulative = self.sources.get("skills_tree.json") == "built-in"
        out: dict[int, float] = {}
        for skill, points in skills.items():
            levels = self.skills_tree.get(f"skill_{skill}", {}).get("levels", {})
            for lvl in range(1, points + 1):
                for b in levels.get(f"skill_level_{lvl}", {}).get("boosters", []):
                    prev = out.get(b["id"], 0.0)
                    out[b["id"]] = prev + b["value"] if cumulative else max(prev, b["value"])
        return out


def build_skills_tree(gd: GameData) -> dict:
    """The q_player_skills_tree (type 9) config, shaped for lobby_menu::fill_skills_tree:

    skill_1..skill_5 { id, levels { skill_level_1..N { boosters [ {id, value} ], perks [ {id} ] } } }
    Every name it then looks up in db_static_dictionaries (skills_dict.skill_<id>,
    boosters_dict.booster_<id>, perks_dict.perk_<id>) exists in the shipped dictionary.
    """
    tree = {}
    for skill in range(1, 6):
        boosters = [b for b in SKILL_BOOSTERS[skill] if b in gd.boosters] or [0]
        levels = {}
        for lvl, perk in enumerate(gd.perks_by_skill.get(skill, []), start=1):
            levels[f"skill_level_{lvl}"] = {
                "boosters": [{"id": boosters[(lvl - 1) % len(boosters)], "value": BOOSTER_STEP}],
                "perks": [{"id": perk}],
            }
        tree[f"skill_{skill}"] = {"id": skill, "levels": levels}
    return tree


def _cost(item: Item) -> int:
    c = item.category
    if c in AMMO_CATEGORIES:
        return 3
    if c in PRIMARY_CATEGORIES or c in PISTOL_CATEGORIES:
        return min(60000, 600 + int(item.weight * 400))
    if c == 10:
        return 60
    if c == 11:
        return 150
    if c == 12:
        return 2500
    if c == 22:
        return 800
    return 150 + int(item.weight * 120)   # armour


def build_prices(gd: GameData) -> dict[int, list[tuple[int, int, int]]]:
    """faction_id -> [(item_dict_id, cost, reputation_level)] when data/shop_prices.json is
    missing. Only traders 1 and 2 are listed by the retail shop window, so they carry
    everything: weapons per WEAPON_OFFERS, ammunition on both, armour by its brand
    (scavenger_* / blackmarket_*), consumables with the Scavengers, artefacts with the
    Black Market. Armour and goods are all on level 0."""
    prices: dict[int, list[tuple[int, int, int]]] = {f: [] for f in gd.faction_levels}
    for item in sorted(gd.items.values(), key=lambda i: i.dict_id):
        if item.dict_id in WEAPON_OFFERS:
            f, level, cost = WEAPON_OFFERS[item.dict_id]
            if f in prices:
                prices[f].append((item.dict_id, cost, level))
            continue
        if item.category in PRIMARY_CATEGORIES + PISTOL_CATEGORIES:
            continue                                    # a weapon nobody sells
        n = item.cfg_name
        traders = (1, 2) if item.category in AMMO_CATEGORIES else             (1,) if "scavenger_" in n or item.category in (10, 11) else             (2,) if "blackmarket_" in n or item.category == 12 else (1,)
        for f in traders:
            if f in prices:
                prices[f].append((item.dict_id, AMMO_PRICES.get(item.dict_id, _cost(item)), 0))
    return prices


def load(extracted: Path | None, data_dir: Path | None = DATA_DIR) -> GameData:
    dicts_path = extracted / "gameplay" / "db_static_dictionaries" if extracted else None
    if dicts_path and dicts_path.is_file():
        try:
            return _from_dictionary(extracted, bc.load(dicts_path.read_bytes()), str(dicts_path), data_dir)
        except Exception as e:  # noqa: BLE001 - any parse problem falls back to the snapshot
            log.warning("cannot parse %s (%r); using built-in item table", dicts_path, e)
    else:
        log.warning("no %s; using built-in item table", dicts_path or "game data dir")
    return _fallback(data_dir)


def _from_dictionary(extracted: Path, d: dict, source: str, data_dir: Path | None) -> GameData:
    items = {}
    for entry in d["items_dict"].values():
        item = Item(entry["dict_id"], entry["item_category"], bool(entry["is_stack"]), entry["cfg_name"])
        cfg = extracted / item.cfg_name
        if cfg.is_file():
            try:
                params = bc.load(cfg.read_bytes()).get("parameters", {})
                item.weight = float(params.get("weight", params.get("clip_weight", 1.0)))
                item.clip_size = int(params.get("clip_size", item.clip_size))
            except Exception:  # noqa: BLE001 - weight only feeds prices
                pass
        items[item.dict_id] = item
    factions = {f["id"]: [lvl["value"] for lvl in f["levels"]] for f in d["factions_dict"].values()}
    perks_by_skill: dict[int, list[int]] = {s: [] for s in range(1, 6)}
    for perk in sorted(d["perks_dict"].values(), key=lambda p: p["id"]):
        for prefix, skill in PERK_PREFIX_SKILL.items():
            if perk["name"].startswith(prefix):
                perks_by_skill[skill].append(perk["id"])
    boosters = {b["id"] for b in d["boosters_dict"].values()}
    gd = GameData(items, factions, perks_by_skill, boosters, source, data_dir)
    log.info("game data: %d items, %d factions, %d perks from %s; tables: %s",
             len(items), len(factions), sum(map(len, perks_by_skill.values())), source, gd.sources)
    return gd


def _fallback(data_dir: Path | None) -> GameData:
    items = {k: Item(k, c, s, n) for k, (c, s, n) in FALLBACK_ITEMS.items()}
    perks = {s: list(range((s - 1) * 7 + 1, s * 7 + 1)) for s in range(1, 6)}
    return GameData(items, dict(FALLBACK_FACTION_LEVELS), perks, set(range(12)), "built-in snapshot", data_dir)
