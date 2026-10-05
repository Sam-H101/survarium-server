"""Static level collision for server-side hitscan: a triangle soup in a 2D (x/z) grid.

The cache (match/data/<level>.collision) is written offline by
match/tools/build_level_collision.py from the level's client_project collision_objects,
the same list the client turns into static Bullet bodies (project_cooker_simple.cpp:114-136,
base_project.cpp:42-60). Only the bodies a bullet ray can see are kept: the client traces
bullets with physics::world::ray_test(.., group 16, mask 8) (bullet.cpp:402), so an object
is hit when (cgroup & 8) and (cmask & 16): the "collision" meshes (8/16) and props (10/20),
never the "walker_collision" meshes (2/4).

Ray rules (bullet.cpp:388-560), applied by `LevelCollision.trace`:
  * ray_test sets kF_KeepUnflippedNormal (bullet_physics_world.cpp:342), so back faces are
    hit and then skipped (check_collision continues past them). Every triangle is stored
    with its front normal = e1 x e2, so a hit counts only when dot(dir, e1 x e2) < 0.
  * A front-face hit on a material whose resistance > bullet pierce stops the bullet;
    otherwise it pierces with speed *= clamp(pierce / resistance - 1, 0, 1)
    (collide_front_face). Grass, foliage, glass, cloth... have resistance 0..0.2.
  * A grazing hit (angle to the surface <= k_ricochet * ricochet_angle) ricochets
    (process_ray_query). The server treats a ricochet as the end of the line of fire.

File layout (little endian), see write_cache / LevelCollision.load:
  b"SVCOLL01", u32 header_size, header json (utf-8), then 4-byte aligned arrays:
  tris f32[n*9] (v0, e1, e2), tri_mat u16[n] (game material id | TERRAIN_FLAG), cell_start u32[nx*nz+1], cell_items u32[m],
  cell_y f32[nx*nz*2] (min y, max y of the triangles listed in a cell).
"""

from __future__ import annotations

import json
import math
import struct
import sys
from array import array
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]

MAGIC = b"SVCOLL01"
DATA_DIR = Path(__file__).resolve().parent / "data"
EPS = 1e-7
TERRAIN_FLAG = 0x8000        # tri_mat bit: the triangle belongs to a terrain_p_* model


def default_cache_path(level: str) -> Path:
    return DATA_DIR / f"{level}.collision"


# ------------------------------------------------------------------------- writing
def build_grid(tris: Sequence[float], cell: float) -> dict:
    """Bin triangles (flat v0,e1,e2 floats) into x/z cells by their bounding box."""
    n = len(tris) // 9
    min_x = min_z = math.inf
    max_x = max_z = -math.inf
    boxes = []
    for i in range(n):
        o = 9 * i
        x0, y0, z0 = tris[o], tris[o + 1], tris[o + 2]
        x1, y1, z1 = x0 + tris[o + 3], y0 + tris[o + 4], z0 + tris[o + 5]
        x2, y2, z2 = x0 + tris[o + 6], y0 + tris[o + 7], z0 + tris[o + 8]
        bx0, bx1 = min(x0, x1, x2), max(x0, x1, x2)
        bz0, bz1 = min(z0, z1, z2), max(z0, z1, z2)
        by0, by1 = min(y0, y1, y2), max(y0, y1, y2)
        boxes.append((bx0, bx1, by0, by1, bz0, bz1))
        min_x, max_x = min(min_x, bx0), max(max_x, bx1)
        min_z, max_z = min(min_z, bz0), max(max_z, bz1)
    min_x, min_z = math.floor(min_x) - 1.0, math.floor(min_z) - 1.0
    nx = max(1, int(math.ceil((max_x + 1.0 - min_x) / cell)))
    nz = max(1, int(math.ceil((max_z + 1.0 - min_z) / cell)))
    cells: List[List[int]] = [[] for _ in range(nx * nz)]
    cy = [[math.inf, -math.inf] for _ in range(nx * nz)]
    for i, (bx0, bx1, by0, by1, bz0, bz1) in enumerate(boxes):
        ix0 = max(0, int((bx0 - min_x) / cell))
        ix1 = min(nx - 1, int((bx1 - min_x) / cell))
        iz0 = max(0, int((bz0 - min_z) / cell))
        iz1 = min(nz - 1, int((bz1 - min_z) / cell))
        for iz in range(iz0, iz1 + 1):
            row = iz * nx
            for ix in range(ix0, ix1 + 1):
                c = row + ix
                cells[c].append(i)
                if by0 < cy[c][0]:
                    cy[c][0] = by0
                if by1 > cy[c][1]:
                    cy[c][1] = by1
    start = array("I", [0])
    items = array("I")
    ys = array("f")
    for c, lst in enumerate(cells):
        items.extend(lst)
        start.append(len(items))
        lo, hi = cy[c]
        ys.extend((lo, hi) if lst else (0.0, -1.0))
    return {"cell": cell, "min_x": min_x, "min_z": min_z, "nx": nx, "nz": nz,
            "start": start, "items": items, "cell_y": ys}


def write_cache(path: Path, tris: Sequence[float], tri_mat: Sequence[int],
                materials: Dict[int, Tuple[str, float, float]], cell: float,
                meta: dict) -> dict:
    grid = build_grid(tris, cell)
    n = len(tris) // 9
    header = dict(meta)
    header.update({
        "version": 1, "triangles": n, "cell": grid["cell"], "min_x": grid["min_x"],
        "min_z": grid["min_z"], "nx": grid["nx"], "nz": grid["nz"],
        "cell_items": len(grid["items"]),
        "materials": {str(k): {"name": v[0], "resistance": v[1], "k_ricochet": v[2]}
                      for k, v in sorted(materials.items())},
    })
    hdr = json.dumps(header, sort_keys=True).encode("utf-8")
    hdr += b" " * (-(len(MAGIC) + 4 + len(hdr)) % 4)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(MAGIC + struct.pack("<I", len(hdr)) + hdr)
        for a in (array("f", tris), array("H", tri_mat), grid["start"], grid["items"], grid["cell_y"]):
            if sys.byteorder != "little":
                a = array(a.typecode, a)
                a.byteswap()
            b = a.tobytes()
            f.write(b + b"\0" * (-len(b) % 4))
    return header


# ------------------------------------------------------------------------- runtime
class RayHit:
    __slots__ = ("distance", "material", "triangle", "point")

    def __init__(self, distance: float, material: int, triangle: int, point: Vec3) -> None:
        self.distance = distance
        self.material = material
        self.triangle = triangle
        self.point = point

    def __repr__(self) -> str:
        return f"RayHit({self.distance:.3f} m, material {self.material}, tri {self.triangle})"


class LevelCollision:
    def __init__(self, header: dict, tris: array, tri_mat: array, start: array, items: array,
                 cell_y: array) -> None:
        self.header = header
        self.tris = tris
        self.tri_mat = tri_mat
        self.start = start
        self.items = items
        self.cell_y = cell_y
        self.cell = float(header["cell"])
        self.inv_cell = 1.0 / self.cell
        self.min_x = float(header["min_x"])
        self.min_z = float(header["min_z"])
        self.nx = int(header["nx"])
        self.nz = int(header["nz"])
        self.max_x = self.min_x + self.nx * self.cell
        self.max_z = self.min_z + self.nz * self.cell
        mats = header.get("materials", {})
        self.resistance: Dict[int, float] = {int(k): float(v["resistance"]) for k, v in mats.items()}
        self.k_ricochet: Dict[int, float] = {int(k): float(v["k_ricochet"]) for k, v in mats.items()}
        self.material_names: Dict[int, str] = {int(k): v["name"] for k, v in mats.items()}
        self.rays_cast = 0
        self.triangles_tested = 0

    @property
    def triangle_count(self) -> int:
        return len(self.tri_mat)

    @classmethod
    def load(cls, path: Path) -> "LevelCollision":
        """Map the cache file read-only (little-endian hosts): the 37 MB of arrays are
        then shared by every match worker process through the OS page cache instead of
        being copied into each one. Falls back to reading into arrays."""
        if sys.byteorder == "little":
            try:
                return cls._load_mapped(Path(path))
            except (OSError, ValueError, TypeError):
                pass
        data = Path(path).read_bytes()
        header, spans = cls._layout(data, path)
        out = []
        for code, pos, size in spans:
            a = array(code)
            a.frombytes(data[pos:pos + size])
            if sys.byteorder != "little":
                a.byteswap()
            out.append(a)
        return cls(header, *out)

    @staticmethod
    def _layout(data, path):
        if bytes(data[:8]) != MAGIC:
            raise ValueError(f"{path}: not a level collision cache")
        (hlen,) = struct.unpack_from("<I", data, 8)
        header = json.loads(bytes(data[12:12 + hlen]).decode("utf-8"))
        pos = 12 + hlen
        n = int(header["triangles"])
        ncell = int(header["nx"]) * int(header["nz"])
        spans = []
        for code, count in (("f", 9 * n), ("H", n), ("I", ncell + 1), ("I", int(header["cell_items"])),
                            ("f", 2 * ncell)):
            size = array(code).itemsize * count
            if pos + size > len(data):
                raise ValueError(f"{path}: truncated")
            spans.append((code, pos, size))
            pos += size + (-size % 4)
        return header, spans

    @classmethod
    def _load_mapped(cls, path: Path) -> "LevelCollision":
        import mmap
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        view = memoryview(mm)
        header, spans = cls._layout(view, path)
        arrays = [view[pos:pos + size].cast(code) for code, pos, size in spans]
        col = cls(header, *arrays)
        col._mmap = mm                    # keep the mapping alive with the views
        return col

    # -- queries ------------------------------------------------------------------
    def trace(self, origin: Vec3, direction: Vec3, max_dist: float,
              pierce: Optional[float] = None, ricochet_angle: float = 0.0,
              terrain_solid: bool = True) -> Optional[RayHit]:
        """First point where a bullet along origin + t*direction (|direction| = 1) is
        stopped, t <= max_dist. pierce None: every front face stops the ray (line of
        sight). Otherwise the client's material rules (module docstring);
        ricochet_angle in radians (ammo ricochet_angle * pi / 180)."""
        self.rays_cast += 1
        ox, oy, oz = origin
        dx, dy, dz = direction
        cell = self.cell
        # clip the x/z extent of the segment against the grid
        t0, t1 = 0.0, max_dist
        for o, d, lo, hi in ((ox, dx, self.min_x, self.max_x), (oz, dz, self.min_z, self.max_z)):
            if abs(d) < 1e-12:
                if o < lo or o >= hi:
                    return None
            else:
                ta, tb = (lo - o) / d, (hi - o) / d
                if ta > tb:
                    ta, tb = tb, ta
                if ta > t0:
                    t0 = ta
                if tb < t1:
                    t1 = tb
        if t0 > t1:
            return None
        px, pz = ox + dx * t0, oz + dz * t0
        ix = min(self.nx - 1, max(0, int((px - self.min_x) * self.inv_cell)))
        iz = min(self.nz - 1, max(0, int((pz - self.min_z) * self.inv_cell)))
        if dx > 0:
            step_x, t_max_x, t_dx = 1, (self.min_x + (ix + 1) * cell - ox) / dx, cell / dx
        elif dx < 0:
            step_x, t_max_x, t_dx = -1, (self.min_x + ix * cell - ox) / dx, -cell / dx
        else:
            step_x, t_max_x, t_dx = 0, math.inf, math.inf
        if dz > 0:
            step_z, t_max_z, t_dz = 1, (self.min_z + (iz + 1) * cell - oz) / dz, cell / dz
        elif dz < 0:
            step_z, t_max_z, t_dz = -1, (self.min_z + iz * cell - oz) / dz, -cell / dz
        else:
            step_z, t_max_z, t_dz = 0, math.inf, math.inf

        tris, tri_mat, start, items, cell_y = self.tris, self.tri_mat, self.start, self.items, self.cell_y
        nx = self.nx
        tested = set()
        pending: List[Tuple[float, int]] = []      # (t, triangle) front hits not yet resolved
        speed = 1.0
        t_enter = t0
        tests = 0
        while True:
            t_exit = min(t_max_x, t_max_z, t1)
            c = iz * nx + ix
            a, b = start[c], start[c + 1]
            if a != b:
                # skip the cell if the ray's y range inside it misses the triangles' y range
                ya, yb = oy + dy * t_enter, oy + dy * t_exit
                if ya > yb:
                    ya, yb = yb, ya
                if not (yb < cell_y[2 * c] - 1e-3 or ya > cell_y[2 * c + 1] + 1e-3):
                    for k in range(a, b):
                        i = items[k]
                        if i in tested:
                            continue
                        tested.add(i)
                        tests += 1
                        o = 9 * i
                        e1x, e1y, e1z = tris[o + 3], tris[o + 4], tris[o + 5]
                        e2x, e2y, e2z = tris[o + 6], tris[o + 7], tris[o + 8]
                        # Moller-Trumbore, front faces only (det > 0 <=> dot(d, e1 x e2) < 0)
                        qx = dy * e2z - dz * e2y
                        qy = dz * e2x - dx * e2z
                        qz = dx * e2y - dy * e2x
                        det = e1x * qx + e1y * qy + e1z * qz
                        if det <= EPS:
                            continue
                        sx, sy, sz = ox - tris[o], oy - tris[o + 1], oz - tris[o + 2]
                        u = sx * qx + sy * qy + sz * qz
                        if u < 0.0 or u > det:
                            continue
                        rx = sy * e1z - sz * e1y
                        ry = sz * e1x - sx * e1z
                        rz = sx * e1y - sy * e1x
                        v = dx * rx + dy * ry + dz * rz
                        if v < 0.0 or u + v > det:
                            continue
                        t = (e2x * rx + e2y * ry + e2z * rz) / det
                        if t < 0.0 or t > max_dist:
                            continue
                        pending.append((t, i))
            if pending:
                pending.sort()
                while pending and pending[0][0] <= t_exit + 1e-6:
                    t, i = pending.pop(0)
                    raw = tri_mat[i]
                    mat = raw & 0x7FFF
                    if pierce is not None:
                        res = self.resistance.get(mat, 1.0)
                        if terrain_solid and raw & TERRAIN_FLAG:
                            res = math.inf
                        if res <= pierce:
                            if ricochet_angle and self._ricochets(i, direction, mat, ricochet_angle):
                                pass                        # ricochet ends the line of fire
                            else:
                                speed *= min(1.0, max(0.0, pierce / res - 1.0)) if res > 0 else 1.0
                                if speed > 1e-3:
                                    continue
                    self.triangles_tested += tests
                    return RayHit(t, mat, i, (ox + dx * t, oy + dy * t, oz + dz * t))
            if t_exit >= t1:
                break
            if t_max_x < t_max_z:
                ix += step_x
                t_enter = t_max_x
                t_max_x += t_dx
                if ix < 0 or ix >= nx:
                    break
            else:
                iz += step_z
                t_enter = t_max_z
                t_max_z += t_dz
                if iz < 0 or iz >= self.nz:
                    break
        self.triangles_tested += tests
        return None

    def _ricochets(self, tri: int, direction: Vec3, mat: int, ricochet_angle: float) -> bool:
        k = self.k_ricochet.get(mat, 0.0)
        if k <= 0.0:
            return False
        o = 9 * tri
        t = self.tris
        nx_ = t[o + 4] * t[o + 8] - t[o + 5] * t[o + 7]
        ny_ = t[o + 5] * t[o + 6] - t[o + 3] * t[o + 8]
        nz_ = t[o + 3] * t[o + 7] - t[o + 4] * t[o + 6]
        ln = math.sqrt(nx_ * nx_ + ny_ * ny_ + nz_ * nz_)
        if ln == 0.0:
            return False
        cos_alpha = (nx_ * direction[0] + ny_ * direction[1] + nz_ * direction[2]) / ln
        grazing = math.acos(max(-1.0, min(1.0, cos_alpha))) - math.pi / 2
        return grazing <= k * ricochet_angle

    def line_of_sight(self, a: Vec3, b: Vec3) -> bool:
        """True when no front face (any material) lies between a and b."""
        d = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        dist = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
        if dist < 1e-6:
            return True
        return self.trace(a, (d[0] / dist, d[1] / dist, d[2] / dist), dist) is None

    def ground_below(self, pos: Vec3, up: float = 0.5, depth: float = 50.0) -> Optional[float]:
        """Height of the first upward-facing surface below pos (starting `up` above it)."""
        hit = self.trace((pos[0], pos[1] + up, pos[2]), (0.0, -1.0, 0.0), up + depth)
        return None if hit is None else hit.point[1]


_CACHE: Dict[str, Optional[LevelCollision]] = {}


def load_level(level: str, path: Optional[Path] = None) -> Optional[LevelCollision]:
    """Cached LevelCollision for a level, or None if no cache was built."""
    key = str(path) if path else level
    if key not in _CACHE:
        p = Path(path) if path else default_cache_path(level)
        _CACHE[key] = LevelCollision.load(p) if p.exists() else None
    return _CACHE[key]
