"""Extract first-person camera (bullet direction) rotations produced by the
1st-view "look" and recoil additive animations of the Survarium v0.100b client.

Stdlib only.  Re-implements, from the binary-matched client sources:

  * .b-spline file layout ............ animation/sources/bi_spline_skeleton_animation_baked*.{h,cpp}
        u16 bones_count, u8 event_channels_count, u8 animation_type (0 full, 1 additive)
        bones_count * 0x48   (9 x 64-bit channel pointers, fixed up at load)
        event_channels_count * 0x10
        per bone, per channel (tx,ty,tz, rx,ry,rz, sx,sy,sz  -- anim_track_common.h enum_channel_id):
            u32 n, n * (f32 knot, f32 point)
        event channel knots/domains/names
  * spline -> cubic polynomial domains  poly_curve_inline.h get_spline_params / create_in_place_internals
  * domain lookup + clamp ............. time_channel_inline.h domain()
  * evaluation time  .................. bone_matrices_computer.cpp: animation_time * default_fps(30)
  * rotation channels are Euler angles (radians) -> math::quaternion(float3) (math_quaternion_inline.h)
  * per-layer mixing / layer stacking . bone_matrices_computer.cpp computed_local_bone_transform,
                                        computed_local_bone_matrix (bone_transform::apply)
  * local matrix = S * R(q) * T, object = local * parent (row vectors), compute_skeleton_branch
  * camera = calculated_head_matrix ... animation_entry_point.cpp (Ry(pi/2)*Rz(pi/2) * Head_obj * object)
  * forward axis = k row (weapon_core::get_dispersed_bullet_dir)

Writes match/data/view_animation_angles.json.
"""

import json
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
POC_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
GAME_DATA = os.path.normpath(os.path.join(POC_ROOT, "..", "game_data"))
EXTRACTED = os.path.join(GAME_DATA, "extracted")
WEAPONS_JSON = os.path.join(GAME_DATA, "json", "raw", "gameplay", "weapons")
OUT_JSON = os.path.join(POC_ROOT, "match", "data", "view_animation_angles.json")

sys.path.insert(0, GAME_DATA)
import binary_config  # noqa: E402

DEFAULT_FPS = 30.0
WEAPONS = ["ak_74u", "fort_17", "magnum", "rem_700", "rem_870", "toz_122",
           "toz_34", "toz_66", "tt_33", "uzi", "vityaz"]
SAMPLES = [0.0, 0.25, 0.5, 0.75, 1.0]
SKELETON = os.path.join(EXTRACTED, "animations", "skeletons", "scavengers_01.skeleton")

# ---------------------------------------------------------------- math (vostok conventions, row vectors)


def mat_mul(a, b):  # 3x3 rotation part + translation handled by mul4x3
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def mul4x3(a, b):
    r = mat_mul(a, b)
    r[0][3] = r[1][3] = r[2][3] = 0.0
    r[3][3] = 1.0
    return r


def identity():
    return [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def create_rotation_euler(ax, ay, az):  # math_float4x4_inline.h create_rotation(float3)
    xs, xc = math.sin(ax), math.cos(ax)
    ys, yc = math.sin(ay), math.cos(ay)
    zs, zc = math.sin(az), math.cos(az)
    xsXyc, xcXyc = xs * yc, xc * yc
    xcXzs, xcXzc = xc * zs, xc * zc
    xsXzs, xsXzc = xs * zs, xs * zc
    ysXzs, ysXzc = ys * zs, ys * zc
    ycXzs, ycXzc = yc * zs, yc * zc
    return [
        [ycXzc, -ycXzs, ys, 0.0],
        [ys * xsXzc + xcXzs, -xs * ysXzs + xcXzc, -xsXyc, 0.0],
        [-xc * ysXzc + xsXzs, xc * ysXzs + xsXzc, xcXyc, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def quat_from_euler(ax, ay, az):  # math_quaternion_inline.h quaternion(float3 angles); (x,y,z,w)
    ax, ay, az = ax * .5, ay * .5, az * .5
    xs, xc = math.sin(ax), math.cos(ax)
    ys, yc = math.sin(ay), math.cos(ay)
    zs, zc = math.sin(az), math.cos(az)
    xsXyc, xcXys, xcXyc, xsXys = xs * yc, xc * ys, xc * yc, xs * ys
    return (-xsXyc * zc - xcXys * zs,
            -xcXys * zc + xsXyc * zs,
            -xcXyc * zs - xsXys * zc,
            -xcXyc * zc + xsXys * zs)


def quat_mul(l, r):  # operator*(quaternion, quaternion)
    lx, ly, lz, lw = l
    rx, ry, rz, rw = r
    return (lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz)


def quat_axis_angle(q):  # get_axis_and_angle
    x, y, z, w = q
    s = math.sqrt(x * x + y * y + z * z)
    if s > 1e-7:
        return (x / s, y / s, z / s), 2.0 * math.atan2(s, w)
    return (0.0, 0.0, 1.0), 0.0


def quat_from_axis_angle(axis, angle):
    s, c = math.sin(angle * .5), math.cos(angle * .5)
    return (axis[0] * s, axis[1] * s, axis[2] * s, c)


def quat_slerp(a, b, t):  # stand-in for ::slerp_optimized (shortest arc)
    d = sum(a[i] * b[i] for i in range(4))
    if d < 0:
        b, d = tuple(-v for v in b), -d
    if d > 0.9995:
        r = tuple(a[i] + t * (b[i] - a[i]) for i in range(4))
    else:
        th = math.acos(d)
        s = math.sin(th)
        wa, wb = math.sin((1 - t) * th) / s, math.sin(t * th) / s
        r = tuple(wa * a[i] + wb * b[i] for i in range(4))
    n = math.sqrt(sum(v * v for v in r))
    return tuple(v / n for v in r)


def create_matrix_q(q, pos=(0.0, 0.0, 0.0)):  # create_matrix(quaternion, float3)
    x, y, z, w = q
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return [
        [1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy), 0.0],
        [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx), 0.0],
        [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy), 0.0],
        [pos[0], pos[1], pos[2], 1.0],
    ]


def create_scale(s):
    m = identity()
    m[0][0], m[1][1], m[2][2] = s
    return m


def create_translation(t):
    m = identity()
    m[3][0], m[3][1], m[3][2] = t
    return m


# ---------------------------------------------------------------- b-spline animation


class Curve:
    """poly_curve<poly_curve_order3_domain<float,1>> built from a baked bi-spline channel."""

    EPS = 0.0000001

    def __init__(self, pairs):
        n = len(pairs)
        self.n = n

        def knot(i):
            return pairs[i if i < n else n - 1][0]

        def point(i):
            return pairs[i if i < n else n - 1][1]

        self.knots, self.domains = [], []
        for i in range(3, n - 1):
            if knot(i + 1) - knot(i) < self.EPS:
                continue
            self.domains.append(self._params(knot, point, i))
            self.knots.append(knot(i))
        self.knots.append(knot(n - 1))

    @staticmethod
    def _params(knot, point, index):
        dT = [knot(index - 1) - knot(index - 2), knot(index) - knot(index - 1),
              knot(index + 1) - knot(index), knot(index + 2) - knot(index + 1),
              knot(index + 3) - knot(index + 2)]
        j = 1
        sq = lambda v: v * v  # noqa: E731
        k0 = 1. / (dT[j + 1] * (dT[j + 1] + dT[j]) * (dT[j + 1] + dT[j] + dT[j - 1]))
        k10 = k0
        k11 = 1. / (dT[j + 1] * (dT[j + 1] + dT[j]) * (dT[j + 1] + dT[j] + dT[j + 2]))
        k12 = 1. / (dT[j + 1] * (dT[j + 1] + dT[j + 2]) * (dT[j + 1] + dT[j] + dT[j + 2]))
        k20, k21 = k11, k12
        k22 = 1. / (dT[j + 1] * (dT[j + 1] + dT[j + 2]) * (dT[j + 1] + dT[j + 2] + dT[j + 3]))
        k3 = k22
        f = [
            [k3, -k20 - k21 - k22, k10 + k11 + k12, -k0],
            [0.,
             k20 * (dT[j + 1] - 2 * dT[j]) + k21 * (dT[j + 1] + dT[j + 2] - dT[j]) + k22 * (dT[j + 1] + dT[j + 2] + dT[j + 3]),
             k10 * (dT[j - 1] + dT[j] - 2 * dT[j + 1]) + k11 * (-2 * dT[j + 1] - dT[j + 2] + dT[j]) + k12 * (-2 * (dT[j + 1] + dT[j + 2])),
             k0 * 3 * dT[j + 1]],
            [0.,
             k20 * (2 * dT[j] * dT[j + 1] - sq(dT[j])) + k21 * (dT[j] * (dT[j + 1] + dT[j + 2])),
             k10 * (sq(dT[j + 1]) - 2 * dT[j + 1] * (dT[j - 1] + dT[j])) + k11 * (-dT[j] * dT[j + 1] + (dT[j + 1] - dT[j]) * (dT[j + 1] + dT[j + 2])) + k12 * sq(dT[j + 1] + dT[j + 2]),
             -k0 * 3 * sq(dT[j + 1])],
            [0., k20 * (dT[j + 1] * sq(dT[j])), k10 * (sq(dT[j + 1]) * (dT[j - 1] + dT[j])) + k11 * (dT[j + 1] * dT[j] * (dT[j + 1] + dT[j + 2])), k0 * dT[j + 1] * sq(dT[j + 1])],
        ]
        p = [point(index), point(index - 1), point(index - 2), point(index - 3)]
        c3 = sum(f[0][m] * p[m] for m in range(4))
        c2 = sum(f[1][m] * p[m] for m in range(4))
        c1 = sum(f[2][m] * p[m] for m in range(4))
        c0 = sum(f[3][m] * p[m] for m in range(4))
        return (c0, c1, c2, c3)

    def min_param(self):
        return self.knots[0]

    def max_param(self):
        return self.knots[-1]

    def evaluate(self, t):
        t = min(max(t, self.knots[0]), self.knots[-1])
        for i in range(len(self.knots) - 1):
            if self.knots[i] <= t <= self.knots[i + 1]:
                c0, c1, c2, c3 = self.domains[i]
                u = t - self.knots[i]
                return u * (u * (u * c3 + c2) + c1) + c0
        raise ValueError("no domain")


class Animation:
    def __init__(self, resource_path):
        rel = resource_path
        if rel.startswith("resources/"):
            rel = rel[len("resources/"):]
        base = os.path.join(EXTRACTED, *rel.split("/"))
        self.path = resource_path
        buf = open(base + ".b-spline", "rb").read()
        names = binary_config.load(base + ".bones_names")["bones_names"]
        nb, nev, self.type = struct.unpack_from("<HBB", buf, 0)
        assert nb == len(names), (resource_path, nb, len(names))
        off = 4 + nb * 0x48 + nev * 0x10
        self.bones = {}
        for b in range(nb):
            chans = []
            for _c in range(9):
                n, = struct.unpack_from("<I", buf, off)
                off += 4
                flat = struct.unpack_from("<%df" % (2 * n), buf, off)
                off += 8 * n
                chans.append(Curve([(flat[2 * k], flat[2 * k + 1]) for k in range(n)]))
            self.bones[names[b]] = chans
        self.event_channels = nev
        first = self.bones[names[0]][0]  # max_time_in_frames: bone 0 translation_x
        self.length_frames = first.max_param() - first.min_param()

    def frame(self, bone, t_frames):
        ch = self.bones.get(bone)
        if ch is None:
            return (0., 0., 0.), (0., 0., 0.), (1., 1., 1.)  # identity_frame()
        v = [c.evaluate(t_frames) for c in ch]
        return tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9])


# ---------------------------------------------------------------- skeleton & mixer


def load_head_chain():
    root = binary_config.load(SKELETON)
    path = []

    def walk(node, name, stack):
        stack = stack + [name]
        if name == "Head":
            path.extend(stack)
            return True
        for k, v in node.items():
            if isinstance(v, dict) and walk(v, k, stack):
                return True
        return False

    walk(root["object_movement"], "object_movement", [])
    assert path, "Head not found"
    return path[1:]  # object_movement is the skeleton root, never computed (compute_bones_matrices)


HEAD_CHAIN = load_head_chain()


def layer_transform(entries, bone, layer):
    """computed_local_bone_transform for one layer. entries: [(anim, t_frames, weight)]"""
    trs, rots, scs = [], [], []
    for anim, t, w in entries:
        if w == 0.0:
            continue
        tr, ro, sc = anim.frame(bone, t)
        trs.append((tr, w))
        rots.append((ro, w))
        scs.append((sc, w))
    if layer == 0 and trs:
        tot = sum(w for _, w in trs)
        if abs(tot - 1.0) > 1e-6:
            trs = [(v, w / tot) for v, w in trs]
    translation = tuple(sum(v[i] * w for v, w in trs) for i in range(3))
    scale = [1.0, 1.0, 1.0]
    for v, w in scs:
        for i in range(3):
            scale[i] *= v[i] ** w
    q = mix_rotations(rots, layer < 2)
    return translation, q, tuple(scale)


def mix_rotations(rots, do_normalization):
    qs = [(quat_from_euler(*v), w) for v, w in rots if abs(w) >= 1e-5]
    if not qs:
        return (0., 0., 0., 1.)
    if len(qs) == 1:
        if do_normalization:
            return qs[0][0]
        axis, ang = quat_axis_angle(qs[0][0])
        return quat_from_axis_angle(axis, ang * qs[0][1])
    if len(qs) == 2:
        tot = qs[0][1] + qs[1][1]
        mix = quat_slerp(qs[0][0], qs[1][0], qs[1][1] / tot)
        if do_normalization:
            return mix
        axis, ang = quat_axis_angle(mix)
        return quat_from_axis_angle(axis, ang * tot)
    raise NotImplementedError("extrapolated_slerp (3+ animations in one layer) not needed here")


def camera(layers):
    """layers: {priority: [(anim, t_frames, weight)]} -> calculated_head_matrix with identity object."""
    n_layers = max(layers) + 1
    parent = identity()
    for bone in HEAD_CHAIN:
        res_t, res_q, res_s = None, None, None
        for layer in range(n_layers):
            t, q, s = layer_transform(layers.get(layer, []), bone, layer)
            if res_t is None:
                res_t, res_q, res_s = list(t), q, list(s)
            else:  # bone_transform::apply
                res_t = [res_t[i] + t[i] for i in range(3)]
                res_q = quat_mul(res_q, q)
                res_s = [res_s[i] * s[i] for i in range(3)]
        local = mat_mul(mat_mul(create_scale(res_s), create_matrix_q(res_q)), create_translation(res_t))
        parent = mul4x3(local, parent)
    head = parent
    fix = mul4x3(create_rotation_euler(0., math.pi / 2, 0.), create_rotation_euler(0., 0., math.pi / 2))
    return mul4x3(fix, head)


def angles(m):
    """forward = k row; pitch = asin(k.y) (+ up), yaw = atan2(k.x, k.z) (+ toward +x)."""
    k = m[2][:3]
    n = math.sqrt(sum(v * v for v in k))
    k = [v / n for v in k]
    return math.degrees(math.asin(max(-1., min(1., k[1])))), math.degrees(math.atan2(k[0], k[2]))


def relative_angles(m0, m1):
    """pitch/yaw of m1's forward expressed in m0's (i, j, k) basis."""
    f = m1[2][:3]
    x = sum(f[i] * m0[0][i] for i in range(3))
    y = sum(f[i] * m0[1][i] for i in range(3))
    z = sum(f[i] * m0[2][i] for i in range(3))
    n = math.sqrt(x * x + y * y + z * z)
    return math.degrees(math.asin(max(-1., min(1., y / n)))), math.degrees(math.atan2(x, z))


def wrap(d):
    return (d + 180.) % 360. - 180.


# ---------------------------------------------------------------- driver

_cache = {}


def anim(path):
    if path not in _cache:
        _cache[path] = Animation(path)
    return _cache[path]


def r4(v):
    return round(v, 4)


STATES = {
    # state -> (movement/look list, hands_only list)
    "stand_hip": ("stand_hud", "stand_hands_only_hud"),
    "stand_aimed": ("aimed_stand_hud", "aimed_stand_hands_only_hud"),
    "crouch_hip": ("crouch_hud", "crouch_hands_only_hud"),
    "crouch_aimed": ("aimed_crouch_hud", "aimed_crouch_hands_only_hud"),
}
LOOK_FRACTION_MAX = 1.0 - 1e-5


def analyse(ua):
    look_out, recoil_out, src, details = {}, {}, {}, {}
    for state, (hud, hands) in STATES.items():
        base = anim(ua[hud][0])          # idle_anim (movement lexeme always uses animation_index)
        look = anim(ua[hud][2])          # idle_look_anim = movement index + 2, additivity_priority 4
        rec = {"vert": anim(ua[hands][0]), "horiz": anim(ua[hands][1]), "back": anim(ua[hands][2])}
        src[state] = {"base": base.path, "look": look.path,
                      **{k: v.path for k, v in rec.items()}}
        base_t = 0.0

        def look_layer(frac):
            return [(look, min(frac, LOOK_FRACTION_MAX) * look.length_frames, 1.0)]

        # (a) look: absolute camera pitch/yaw vs look fraction, on top of base idle at frame 0
        rows = []
        for f in SAMPLES:
            p, y = angles(camera({0: [(base, base_t, 1.0)], 4: look_layer(f)}))
            rows.append([f, r4(2 * f - 1), r4(p), r4(y)])
        p_nolook, y_nolook = angles(camera({0: [(base, base_t, 1.0)]}))
        # idle sway of the base pose (no look): min/max pitch over the idle clip
        sway = [angles(camera({0: [(base, base.length_frames * s / 20., 1.0)], 4: look_layer(.5)}))
                for s in range(21)]
        fine = []
        for s in range(41):
            f = s / 40.
            fine.append(angles(camera({0: [(base, base_t, 1.0)], 4: look_layer(f)}))[0])
        p_mid = rows[2][2]
        lin_err = max(abs(fine[s] - (p_mid + (rows[4][2] - rows[0][2]) / 2 * (2 * s / 40. - 1))) for s in range(41))
        look_out[state] = {
            "samples_t_lookpitch_pitch_yaw": rows,
            "pitch_without_look_deg": r4(p_nolook),
            "yaw_without_look_deg": r4(y_nolook),
            "pitch_per_unit_look_pitch_deg": r4((rows[4][2] - rows[0][2]) / 2),
            "max_deviation_from_linear_deg": r4(lin_err),
            "idle_sway_pitch_range_deg": [r4(min(s[0] for s in sway)), r4(max(s[0] for s in sway))],
            "idle_sway_yaw_range_deg": [r4(min(s[1] for s in sway)), r4(max(s[1] for s in sway))],
            "look_clip_frames": look.length_frames,
            "fine_table_lookpitch_pitch": [[r4(2 * s / 40. - 1), r4(fine[s])] for s in range(41)],
            "slope_down_deg_per_unit": r4(-rows[0][2]),
            "slope_up_deg_per_unit": r4(rows[4][2]),
        }

        # (b) recoil deltas relative to the same pose without the recoil animation
        rstate = {}
        for name, a in rec.items():
            layer = 2 if name == "back" else 3
            rows = []
            for lp in (0.5,):
                ref_layers = {0: [(base, base_t, 1.0)], 4: look_layer(lp)}
                p0, y0 = angles(camera(ref_layers))
                for f in SAMPLES:
                    lay = dict(ref_layers)
                    lay[layer] = [(a, f * a.length_frames, 1.0)]
                    p, y = angles(camera(lay))
                    rows.append([f, r4(p - p0), r4(wrap(y - y0))])
            rstate[name] = rows
            details.setdefault(state, {})[name + "_clip_frames"] = a.length_frames
            details[state][name + "_type"] = "additive" if a.type == 1 else "full"
        # dependence on look pitch: recoil deltas expressed in the un-recoiled camera's own frame
        rel = {}
        for name, a in rec.items():
            layer = 2 if name == "back" else 3
            per_lp = {}
            for lp in (-0.5, 0.0, 0.5):
                ref_layers = {0: [(base, base_t, 1.0)], 4: look_layer(lp / 2 + .5)}
                m0 = camera(ref_layers)
                rows = []
                for f in SAMPLES:
                    lay = dict(ref_layers)
                    lay[layer] = [(a, f * a.length_frames, 1.0)]
                    p, y = relative_angles(m0, camera(lay))
                    rows.append([f, r4(p), r4(y)])
                per_lp["%+.1f" % lp] = rows
            rel[name] = per_lp
        details[state]["camera_relative_by_look_pitch"] = rel
        # both horiz and vert active (layer 3 mix, weights 1) vs the sum of the individual deltas
        ref_layers = {0: [(base, base_t, 1.0)], 4: look_layer(.5)}
        m0 = camera(ref_layers)
        combo = []
        for fv, fh in ((0.75, 0.75), (1.0, 0.0), (0.6, 0.4)):
            lay = dict(ref_layers)
            lay[3] = [(rec["horiz"], fh * rec["horiz"].length_frames, 1.0), (rec["vert"], fv * rec["vert"].length_frames, 1.0)]
            combo.append([fv, fh] + [r4(v) for v in relative_angles(m0, camera(lay))])
        details[state]["combined_vert_horiz_[vert_t,horiz_t,pitch,yaw]"] = combo
        recoil_out[state] = rstate
    return look_out, recoil_out, src, details


def check_math():
    # quaternion(float3) -> create_matrix must equal create_rotation(float3) (same engine convention)
    worst = 0.
    for ang in [(0.3, 0., 0.), (0., 0.4, 0.), (0., 0., 0.5), (0.2, -0.7, 1.1)]:
        a = create_rotation_euler(*ang)
        b = create_matrix_q(quat_from_euler(*ang))
        worst = max(worst, max(abs(a[r][c] - b[r][c]) for r in range(3) for c in range(3)))
    return worst


def main():
    math_err = check_math()
    per_weapon, families = {}, {}
    for w in WEAPONS:
        ua = json.load(open(os.path.join(WEAPONS_JSON, w + ".user_animations.json")))
        key = json.dumps([ua[h] + ua[ho] for h, ho in STATES.values()])
        if key not in families:
            families[key] = analyse(ua)
        look, recoil, src, details = families[key]
        per_weapon[w] = {"look": look, "recoil": recoil, "recoil_details": details, "sources": src}
    out = {
        "notes": NOTES + ["euler->quaternion vs create_rotation max element diff: %.2e" % math_err,
                          "distinct 1st-view look/recoil animation families across the 11 weapons: %d" % len(families)],
    }
    out.update(per_weapon)
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w") as fh:
        json.dump(out, fh, indent=1)
    print("wrote", OUT_JSON, "families:", len(families), "math check:", math_err)
    for st in STATES:
        lk = per_weapon["ak_74u"]["look"][st]
        print(st, "look", lk["samples_t_lookpitch_pitch_yaw"], "nolook", lk["pitch_without_look_deg"],
              "lin_err", lk["max_deviation_from_linear_deg"], "sway", lk["idle_sway_pitch_range_deg"])
        for n in ("vert", "horiz", "back"):
            print("   ", n, per_weapon["ak_74u"]["recoil"][st][n])


NOTES = [
    "Camera = animation::calculated_head_matrix(Head object matrix, user transform); forward = k row. "
    "Object frame: y up, z forward (yaw=atan2(k.x,k.z), +yaw toward +x), pitch=asin(k.y) (+ = up). "
    "Angles in degrees.",
    "Bones are FK'd through object_movement/Root/Hip/Spin/Spin_1/Chest/Neck/Head; the skeleton file carries no "
    "bind pose, every bone's local TRS comes from the animations (bones missing from an animation get identity).",
    "Layers (additivity_priority): 0 = movement idle (base pose, sampled at frame 0), 2 = recoil_back, "
    "3 = recoil_horiz + recoil_vert (same layer!), 4 = look. Layer transforms are stacked in priority order with "
    "translation +=, rotation = acc * layer (quaternion product), scale *= (bone_transform::apply). Additive "
    "clips are applied as raw deltas: there is no subtraction of a reference/first frame anywhere in the loader "
    "or mixer.",
    "look: 'samples_t_lookpitch_pitch_yaw' rows are [clip fraction t, look_pitch=2t-1, absolute camera pitch, yaw]. "
    "Clip fraction 1.0 is evaluated at 1-1e-5 as the client does.",
    "recoil: rows are [clip fraction, pitch delta, yaw delta] relative to the same pose (idle frame 0 + look at "
    "t=0.5) without that recoil clip, each recoil clip alone with weight 1. When both horiz and vert are active "
    "they share layer 3 and are mixed by mix_rotations(do_normalization=false): slerp(q_h, q_v, 0.5) with the "
    "angle doubled ~= sum of the two rotation vectors for small angles (not the product).",
    "Weapon time fractions (weapon_core::selected_animations): vert_t = clamp(v)+0.5, horiz_t = 0.5-clamp(h), "
    "back_t = clamp(b, e, 1-e).",
    "Findings: all 11 weapons reference the same ak/player 1st-view idle/look/recoil clips (pistols differ only "
    "in sprint_hud). Hip and aimed clips have identical Root..Head curves, so hip==aimed. The base idle does not "
    "move the head chain (camera fixed, identity orientation at look t=0.5, eye at y=1.485 m incl. the +0.1*j "
    "offset).",
    "look: piecewise linear in look_pitch with a kink at 0 (two spline domains of 18 frames each, linear within "
    "each): stand down 75.43 deg/unit, up 70.74 deg/unit; crouch down 70.54, up 72.97. look_pitch is NOT radians.",
    "recoil: only the Head bone moves (layer 3); deltas are camera-relative and independent of look pitch. "
    "vert: -21.34 deg (t=0) .. +21.60 deg (t=1), linear, ~43.0 deg per unit of v (t=v+0.5). horiz: +18.71 deg "
    "yaw (t=0, toward +x/camera right) .. -18.63 (t=1), linear, horiz_t=0.5-h so +h -> +yaw. back: no head-chain "
    "channels (camera unaffected; it only moves the arms/weapon).",
    "Assumptions: lexeme weights are 1 (look weight follows the main lexeme; recoil lexemes have no explicit "
    "weight). ::slerp_optimized is not in the sources; a standard shortest-arc slerp is used (affects only the "
    "combined horiz+vert case).",
]

if __name__ == "__main__":
    main()
