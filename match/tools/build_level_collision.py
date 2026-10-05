"""Offline extractor: a level's static bullet collision -> match/data/<level>.collision.

    python match/tools/build_level_collision.py [--level level_03]
        [--game-data ..\\game_data\\extracted] [--cell 1] [--out PATH]

Sources, all read the way the client reads them:
  projects/<level>/client_project       binary_config; collision_objects[] = {lib_name,
                                         cgroup, cmask, position, rotation, scale}
                                         (project_cooker_simple.cpp:114-136)
  <lib_name>                             models/<m>.model/collision[#[sx][sy][sz]]: the
                                         suffix is a baked local scale
                                         (physics/sources/collision_shape_cook.cpp:26-44)
    .../vertices, indices                chunk 0x19 float3[], chunk 0x1a u32[] (model_format.h)
    .../face_data                        chunk 0x1b {u16 n, n x cstr maya_sg}, chunk 0x1c u16 per face
    .../exported_primitives              binary_config {primitives[] = {type, position,
                                         rotation, scale(=dims), mtl}, mtl_list[]};
                                         0 sphere r=x, 1 box half extents, 2 cylinder
                                         (Y axis, r=x, half height=y), 3 capsule
                                         (physics/sources/collision_shapes.cpp:97-180)
  models/<m>.model/settings              game_material_settings[maya_sg].game_material_id
  game_materials/game.materials          physic.resistance / k_ricochet per material id
Object transform: create_scale(scale) * create_rotation(rotation) * create_translation(pos)
(base_project.cpp:42-48, row vectors). Bullet keeps only the rotation and position
(from_vostok(float4x4) goes through a quaternion); the scale is the baked suffix.

Winding: bullet.cpp counts a hit as a front face when dot(hit normal, ray dir) < 0
(triangle_orientation_front_face = 0 = (0 <= cos_alpha) false, bullet.h:25). Following the
z-mirror of from_vostok/from_bullet through Bullet's (v1-v0)x(v2-v0) triangle normal on
paper gives the opposite sign, which would make every terrain triangle a back face from
above; the data settles it instead: all 18k terrain triangles and 954 of the 974
horizontal barracks triangles (the floor under 11 respawn points) have (v1-v0)x(v2-v0)
pointing up, so that is the front normal (the D3D clockwise convention). Triangles are stored as-is (e1 x e2 = front normal) and primitives are
tessellated with outward faces. The respawn ground check below exercises both.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

POC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(POC))

import binary_config  # noqa: E402  (poc-server/binary_config.py)
from match.level_collision import TERRAIN_FLAG, LevelCollision, default_cache_path, write_cache  # noqa: E402

DEFAULT_GAME_DATA = POC.parent / "game_data" / "extracted"
BULLET_RAY_GROUP, BULLET_RAY_MASK = 16, 8           # bullet.cpp:402 ray_test(.., 16, 8)
SPHERE_SEGMENTS, SPHERE_RINGS, CYLINDER_SEGMENTS = 12, 6, 12


def _load_cfg(path: Path):
    return binary_config.load(path.read_bytes())


def _chunks(data: bytes) -> Dict[int, bytes]:
    """memory::chunk_reader, chunk_type_sequential: {u32 id, u32 size, bytes}..."""
    out, pos = {}, 0
    while pos + 8 <= len(data):
        cid, size = struct.unpack_from("<II", data, pos)
        out[cid] = data[pos + 8:pos + 8 + size]
        pos += 8 + size
    return out


def rotation_rows(angles: Sequence[float]):
    """math::create_rotation(float3 angles) (math_float4x4_inline.h:253-281): rows i, j, k."""
    sx, cx = math.sin(angles[0]), math.cos(angles[0])
    sy, cy = math.sin(angles[1]), math.cos(angles[1])
    sz, cz = math.sin(angles[2]), math.cos(angles[2])
    i = (cy * cz, -cy * sz, sy)
    j = (sy * sx * cz + cx * sz, -sx * sy * sz + cx * cz, -sx * cy)
    k = (-cx * sy * cz + sx * sz, cx * sy * sz + sx * cz, cx * cy)
    return i, j, k


def transform(rows, pos, v):
    i, j, k = rows
    return (v[0] * i[0] + v[1] * j[0] + v[2] * k[0] + pos[0],
            v[0] * i[1] + v[1] * j[1] + v[2] * k[1] + pos[1],
            v[0] * i[2] + v[1] * j[2] + v[2] * k[2] + pos[2])


# ----------------------------------------------------------------- primitive meshes
def _box(h) -> List[Tuple[tuple, tuple, tuple]]:
    x, y, z = h
    c = [(sx * x, sy * y, sz * z) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    # corner index = 4*(sx>0) + 2*(sy>0) + (sz>0); quads listed counter-clockwise seen
    # from outside in a right-handed sense, so (b-a)x(c-a) points outward
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    out = []
    for a, b, cc, d in quads:
        out.append((c[a], c[b], c[cc]))
        out.append((c[a], c[cc], c[d]))
    return out


def _cylinder(r, hh, seg=CYLINDER_SEGMENTS):
    ring = [(r * math.cos(2 * math.pi * s / seg), r * math.sin(2 * math.pi * s / seg)) for s in range(seg)]
    out = []
    for s in range(seg):
        (x0, z0), (x1, z1) = ring[s], ring[(s + 1) % seg]
        b0, b1, t0, t1 = (x0, -hh, z0), (x1, -hh, z1), (x0, hh, z0), (x1, hh, z1)
        out += [(b0, t0, t1), (b0, t1, b1)]
        out.append(((0.0, hh, 0.0), t1, t0))
        out.append(((0.0, -hh, 0.0), b0, b1))
    return out


def _sphere(r, center=(0.0, 0.0, 0.0), seg=SPHERE_SEGMENTS, rings=SPHERE_RINGS, y_lo=None, y_hi=None):
    def p(ri, si):
        th = math.pi * ri / rings
        ph = 2 * math.pi * si / seg
        return (center[0] + r * math.sin(th) * math.cos(ph), center[1] + r * math.cos(th),
                center[2] + r * math.sin(th) * math.sin(ph))
    out = []
    for ri in range(rings):
        for si in range(seg):
            a, b, c, d = p(ri, si), p(ri, si + 1), p(ri + 1, si + 1), p(ri + 1, si)
            if ri > 0:
                out.append((a, b, c))
            if ri < rings - 1:
                out.append((a, c, d))
    return out


def _outward(tris, center=(0.0, 0.0, 0.0)):
    """Orient every triangle so that (b-a)x(c-a) points away from the shape centre."""
    out = []
    for a, b, c in tris:
        e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        m = ((a[0] + b[0] + c[0]) / 3 - center[0], (a[1] + b[1] + c[1]) / 3 - center[1],
             (a[2] + b[2] + c[2]) / 3 - center[2])
        out.append((a, b, c) if n[0] * m[0] + n[1] * m[1] + n[2] * m[2] >= 0 else (a, c, b))
    return out


def primitive_triangles(ptype: int, dims) -> List[tuple]:
    if ptype == 0:
        return _outward(_sphere(dims[0]))
    if ptype == 1:
        return _outward(_box(dims))
    if ptype == 2:
        return _outward(_cylinder(dims[0], dims[1]))
    if ptype == 3:                       # btCapsuleShape(radius, height): Y axis
        r, hh = dims[0], dims[1] / 2
        tris = _cylinder(r, hh)
        tris = [t for t in tris if not (t[0][0] == 0.0 and t[0][2] == 0.0)]
        caps = [t for t in _sphere(r, (0, hh, 0)) if min(v[1] for v in t) >= hh - 1e-6] + \
               [t for t in _sphere(r, (0, -hh, 0)) if max(v[1] for v in t) <= -hh + 1e-6]
        return _outward(tris + caps)
    return []


# ----------------------------------------------------------------- model loading
class Model:
    def __init__(self) -> None:
        self.local: List[Tuple[tuple, tuple, tuple, int]] = []   # front-wound tris + material
        self.mesh_tris = 0
        self.prim_count = 0
        self.has_mesh = False
        self.has_prims = False
        self.missing_materials = 0


def load_model(root: Path, lib_name: str) -> Model:
    path = lib_name.split("#[")[0]
    scale = (1.0, 1.0, 1.0)
    if "#[" in lib_name:
        parts = lib_name.split("#[", 1)[1].replace("]", " ").replace("[", " ").split()
        scale = tuple(float(x) for x in parts[:3])
    rel = path[len("resources/"):] if path.startswith("resources/") else path
    cdir = root / rel
    model_dir = root / rel[:rel.index(".model") + len(".model")]
    m = Model()
    settings = {}
    if (model_dir / "settings").exists():
        settings = _load_cfg(model_dir / "settings") or {}
    gms = settings.get("game_material_settings") or {}

    def game_mtl(maya_sg: str) -> int:
        e = gms.get(maya_sg)
        if isinstance(e, dict) and "game_material_id" in e:
            return int(e["game_material_id"])
        m.missing_materials += 1
        return 0xFFFF

    prims_path = cdir / "exported_primitives"
    if prims_path.exists():
        cfg = _load_cfg(prims_path)
        mtl_list = cfg.get("mtl_list") or []
        prims = cfg.get("primitives") or []
        m.has_prims = bool(prims)
        for p in prims:
            idx = int(p.get("mtl", 0) or 0)
            mat = game_mtl(mtl_list[idx]) if idx < len(mtl_list) and gms else 0
            rows = rotation_rows(p["rotation"])
            for a, b, c in primitive_triangles(int(p["type"]), p["scale"]):
                tri = tuple(transform(rows, p["position"], v) for v in (a, b, c))
                tri = tuple((v[0] * scale[0], v[1] * scale[1], v[2] * scale[2]) for v in tri)
                m.local.append((tri[0], tri[1], tri[2], mat))
            m.prim_count += 1
    if (cdir / "vertices").exists() and (cdir / "indices").exists():
        verts_raw = _chunks((cdir / "vertices").read_bytes()).get(0x19, b"")
        idx_raw = _chunks((cdir / "indices").read_bytes()).get(0x1A, b"")
        nv = len(verts_raw) // 12
        verts = [struct.unpack_from("<3f", verts_raw, 12 * i) for i in range(nv)]
        verts = [(v[0] * scale[0], v[1] * scale[1], v[2] * scale[2]) for v in verts]
        idx = struct.unpack("<%dI" % (len(idx_raw) // 4), idx_raw)
        ntri = len(idx) // 3
        face_mats = [0] * ntri
        if (cdir / "face_data").exists() and settings:
            ch = _chunks((cdir / "face_data").read_bytes())
            hdr = ch.get(0x1B, b"")
            (count,) = struct.unpack_from("<H", hdr, 0)
            pos, names = 2, []
            for _ in range(count):
                end = hdr.index(b"\0", pos)
                names.append(hdr[pos:end].decode("latin-1"))
                pos = end + 1
            remap = [game_mtl(n) for n in names]
            faces = ch.get(0x1C, b"")
            for f in range(min(ntri, len(faces) // 2)):
                (k,) = struct.unpack_from("<H", faces, 2 * f)
                face_mats[f] = remap[k] if k < len(remap) else 0xFFFF
        for f in range(ntri):
            a, b, c = idx[3 * f], idx[3 * f + 1], idx[3 * f + 2]
            if max(a, b, c) >= nv:
                continue
            # front normal = (v1-v0)x(v2-v0): see the module docstring
            m.local.append((verts[a], verts[b], verts[c], face_mats[f]))
        m.mesh_tris = ntri
        m.has_mesh = ntri > 0
    return m


def load_materials(root: Path) -> Dict[int, Tuple[str, float, float]]:
    cfg = _load_cfg(root / "game_materials" / "game.materials")
    out = {}
    for e in cfg["materials"]:
        if e.get("deleted"):
            continue
        ph = e.get("physic") or {}
        out[int(e["id"])] = (e["name"], float(ph.get("resistance", 1.0)), float(ph.get("k_ricochet", 0.0)))
    return out


# ----------------------------------------------------------------- main
def build(level: str, root: Path, out: Path, cell: float, verbose: bool = True) -> dict:
    t_start = time.time()
    project = _load_cfg(root / "projects" / level / "client_project")
    objects = project.get("collision_objects") or []
    materials = load_materials(root)
    models: Dict[str, Model] = {}
    tris: List[float] = []
    tri_mat: List[int] = []
    stats = Counter()
    per_kind = defaultdict(lambda: [0, 0])          # kind -> [objects, triangles]
    mat_count = Counter()
    missing = Counter()
    for o in objects:
        group, mask = int(o["cgroup"]), int(o["cmask"])
        if not (group & BULLET_RAY_MASK and mask & BULLET_RAY_GROUP):
            stats["skipped_not_bullet_visible"] += 1
            continue
        lib = o["lib_name"]
        if lib not in models:
            models[lib] = load_model(root, lib)
        m = models[lib]
        if not m.local:
            stats["objects_without_geometry"] += 1
            missing[lib.split("#[")[0]] += 1
            continue
        rows = rotation_rows(o["rotation"])
        pos = o["position"]
        name = lib.split("/models/", 1)[-1]
        terrain = "/terrain_p_" in lib
        kind = "terrain" if terrain else name.split("/")[0]
        per_kind[kind][0] += 1
        per_kind[kind][1] += len(m.local)
        stats["objects"] += 1
        stats["mesh_objects"] += m.has_mesh
        stats["primitive_objects"] += m.has_prims
        for a, b, c, mat in m.local:
            v0 = transform(rows, pos, a)
            v1 = transform(rows, pos, b)
            v2 = transform(rows, pos, c)
            e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
            e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
            nx = e1[1] * e2[2] - e1[2] * e2[1]
            ny = e1[2] * e2[0] - e1[0] * e2[2]
            nz = e1[0] * e2[1] - e1[1] * e2[0]
            if nx * nx + ny * ny + nz * nz < 1e-14:
                stats["degenerate_triangles"] += 1
                continue
            mid = mat if mat in materials else 0xFFFF
            if mid == 0xFFFF:
                stats["triangles_unknown_material"] += 1
                mid = 0x7FFF
            mat_count[materials.get(mid, ("unknown",))[0]] += 1
            tris.extend(v0 + e1 + e2)
            tri_mat.append(mid | (TERRAIN_FLAG if terrain else 0))
    mats = dict(materials)
    mats[0x7FFF] = ("unknown (solid)", 1.0, 0.0)
    meta = {
        "level": level, "source": "projects/%s/client_project collision_objects" % level,
        "bullet_filter": {"ray_group": BULLET_RAY_GROUP, "ray_mask": BULLET_RAY_MASK},
        "stats": {
            "collision_objects_total": len(objects),
            "bullet_visible_objects": stats["objects"] + stats["objects_without_geometry"],
            "objects_with_geometry": stats["objects"],
            "objects_with_mesh": stats["mesh_objects"],
            "objects_with_primitives": stats["primitive_objects"],
            "objects_without_geometry": stats["objects_without_geometry"],
            "walker_only_objects_skipped": stats["skipped_not_bullet_visible"],
            "unique_collision_models": len(models),
            "degenerate_triangles_dropped": stats["degenerate_triangles"],
            "triangles_unknown_material": stats["triangles_unknown_material"],
            "by_kind": {k: {"objects": v[0], "triangles": v[1]} for k, v in sorted(per_kind.items())},
            "triangles_by_material": dict(mat_count.most_common()),
            "models_without_geometry": dict(missing.most_common()),
        },
    }
    header = write_cache(out, tris, tri_mat, mats, cell, meta)
    header["build_seconds"] = round(time.time() - t_start, 1)
    if verbose:
        s = header["stats"]
        print(f"{level}: {header['triangles']} triangles from {s['objects_with_geometry']} objects "
              f"({s['unique_collision_models']} models), grid {header['nx']}x{header['nz']} "
              f"cells of {cell} m, {header['cell_items']} cell entries -> {out} "
              f"({out.stat().st_size / 1e6:.1f} MB, {header['build_seconds']} s)")
        print(json.dumps(s, indent=1))
    return header


def spawn_check(level: str, cache: Path, maps_json: Path) -> dict:
    """Ray straight down from every respawn point: ground within 2 m?"""
    col = LevelCollision.load(cache)
    maps = {m["project_name"]: m for m in json.loads(maps_json.read_text(encoding="utf-8"))["maps"]}
    pts = maps[level].get("respawn_points") or []
    drops = []
    misses = []
    for p in pts:
        pos = p["position"] if isinstance(p["position"], (list, tuple)) else p["position"]["float3"]
        g = col.ground_below(tuple(pos), up=0.5, depth=50.0)
        if g is None:
            misses.append(p.get("id", p.get("point_id")))
        else:
            drops.append(pos[1] - g)
    within = sum(1 for d in drops if -0.5 <= d <= 2.0)
    res = {"points": len(pts), "ground_found": len(drops), "within_2m": within,
           "drop_min": round(min(drops), 3) if drops else None,
           "drop_max": round(max(drops), 3) if drops else None,
           "drop_mean": round(sum(drops) / len(drops), 3) if drops else None,
           "no_ground": misses}
    print("spawn check:", json.dumps(res))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--level", default="level_03")
    ap.add_argument("--game-data", type=Path, default=DEFAULT_GAME_DATA)
    ap.add_argument("--cell", type=float, default=1.0, help="grid cell size in metres")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--maps-json", type=Path, default=POC.parent / "game_data" / "json" / "maps.json")
    args = ap.parse_args()
    out = args.out or default_cache_path(args.level)
    header = build(args.level, args.game_data, out, args.cell)
    if args.maps_json.exists():
        check = spawn_check(args.level, out, args.maps_json)
        stats_path = out.with_suffix(".collision.json")
        header["spawn_check"] = check
        stats_path.write_text(json.dumps({k: v for k, v in header.items() if k != "materials"},
                                         indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
