#!/usr/bin/env python3
"""Rewrite the weapon rows (lobby_data.WEAPON_OFFERS) and ammunition prices (AMMO_PRICES) of
data/shop_prices.json.

Everything else in the file (armour, consumables, service prices) is kept. The
retail shop window lists only traders 1 and 2, so every weapon is sold by one of them and
the other traders carry no weapons. Run from poc-server/:

    python tools/gen_shop_prices.py            # rewrite data/shop_prices.json
    python tools/gen_shop_prices.py --check    # exit 1 if the file is out of date
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lobby_data as ld  # noqa: E402

SHOP = ROOT / "data" / "shop_prices.json"
ITEMS = ROOT.parent / "game_data" / "json" / "items.json"
WEAPON_CATEGORIES = ld.PRIMARY_CATEGORIES + ld.PISTOL_CATEGORIES


def generate(doc: dict, names: dict[int, str]) -> dict:
    factions = {f: [dict(r, cost=ld.AMMO_PRICES.get(r["item_dict_id"], r["cost"])) for r in rows
                    if r["item_dict_id"] not in ld.WEAPON_OFFERS]
                for f, rows in doc["factions"].items()}
    for dict_id, (trader, level, cost) in sorted(ld.WEAPON_OFFERS.items()):
        factions.setdefault(str(trader), []).append(
            {"item_dict_id": dict_id, "cost": cost, "reputation_level": level,
             "name_ru": names.get(dict_id, "")})
    for rows in factions.values():
        rows.sort(key=lambda r: (r["reputation_level"], r["item_dict_id"]))
    for f in ("1", "2", "3", "4"):
        factions.setdefault(f, [])
    out = dict(doc)
    out["factions"] = dict(sorted(factions.items()))
    out["note"] = doc["note"].split(" Weapons:")[0] + (
        " Weapons: every weapon is sold by trader 1 or 2 (the retail shop lists only those) and "
        "unlocks at the reputation_level shown (tools/gen_shop_prices.py, lobby_data.WEAPON_OFFERS).")
    return out


def main(argv: list[str]) -> int:
    doc = json.loads(SHOP.read_text(encoding="utf-8"))
    names = {}
    if ITEMS.is_file():
        names = {i["dict_id"]: i.get("name_ru") or "" for i in json.loads(ITEMS.read_text(encoding="utf-8"))["items"]}
    new = generate(doc, names)
    text = json.dumps(new, indent=1, ensure_ascii=False) + "\n"
    if "--check" in argv:
        return 0 if json.loads(text) == doc else 1
    SHOP.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
