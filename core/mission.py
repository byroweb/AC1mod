"""
core/mission.py — assemble a mission's populated 3D scene for AC1mod.

Reads mission N's spawn table (FDAT entry 2N+1, chunk 12: 256×40-byte records),
and builds a Scene of placed objects. Until block-index→geometry resolution lands
(agent task), each spawn is a typed 3D MARKER at its world position; metadata
(type, position, rotation, block, params) feeds the info dropdown. Designed so the
marker mesh can later be swapped for the real PA geometry block per spawn.

Spawn record layout (20 int16, verified): hw2-4 = X,Y,Z; hw5 = geometry block index
(-1 = none); hw7 = rotation (0/1024/2048/3072 = 0/90/180/270°); hw9 = type id.
"""
from __future__ import annotations
import struct, math, colorsys
from core import pa_parser as PP
from core.scene import Scene, SceneObject
from core.fdat import mission_entry

SPAWN_CHUNK = 12
REC = 40

# Filled in as object/MT stat tables are reverse-engineered (agent task). Maps a
# spawn type id -> a human label; empty -> shown as "type N".
TYPE_LABELS: dict = {}

# Per-instance spawn params hw8..hw19 (index 0..11). RE found NO global per-type
# stat table — these in-record values carry per-instance numbers. Labels are
# best-guess pending a DuckStation HP/damage confirm; hw11 (index 3) is the
# leading HP candidate.
PARAM_LABELS = {3: "HP? (hw11)", 4: "hw12 (range/area?)", 0: "hw8 (AI?)"}

MIS_SECTORS = (101920, 103449)   # MIS.T (mission text corpus)


def mission_names(bin_path, index_path=None):
    """{mission_number: name} from MIS.T entry 0 (0x20-byte stride name array)."""
    import re
    try:
        ents = PP.read_container(bin_path, *MIS_SECTORS)
    except Exception:
        return {}
    e0 = ents[0] if ents else b""
    names = {}
    for m in range(64):
        off = 0x100 + m * 0x20
        if off + 2 > len(e0):
            break
        end = e0.find(b">", off, off + 0x20)
        if end <= off:
            continue
        raw = e0[off:end]
        if all(0x20 <= c < 0x7f for c in raw):
            names[m] = raw.decode("ascii", "replace")
    return names


def _walk_chunks(buf, limit=64):
    off = 0
    for idx in range(limit):
        if off + 4 > len(buf):
            return
        ln = struct.unpack_from("<I", buf, off)[0]
        if ln == 0 or off + 4 + ln > len(buf):
            return
        yield idx, off + 4, ln
        off += 4 + ln


def spawns(bin_path, n, index_path=None):
    buf = mission_entry(bin_path, n, odd=True, index_path=index_path)
    sp = next((c for c in _walk_chunks(buf) if c[0] == SPAWN_CHUNK), None)
    if not sp:
        return []
    _, off, ln = sp
    out = []
    for i in range(ln // REC):
        hw = struct.unpack_from("<20h", buf, off + i * REC)
        # at the true chunk payload start: X,Y,Z,blk = hw0..3; rot = hw5; type = hw7
        x, y, z, blk, rot, typ = hw[0], hw[1], hw[2], hw[3], hw[5], hw[7]
        if typ <= 0:                 # type 0 = empty/sentinel slot
            continue
        out.append(dict(i=i, x=x, y=y, z=z, blk=blk, rot=rot, typ=typ,
                        params=list(hw[8:])))
    return out


def type_color(t):
    r, g, b = colorsys.hsv_to_rgb((t * 0.137) % 1.0, 0.6, 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def _marker_mesh(size=400.0):
    """A small upright diamond (octahedron) as a spawn marker."""
    s = size
    v = [(0, -s, 0), (s, 0, 0), (0, 0, s), (-s, 0, 0), (0, 0, -s), (0, s, 0)]
    f = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1),
         (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4)]
    m = PP.Mesh()
    m.vertices = [(float(a), float(b), float(c)) for a, b, c in v]
    m.faces = [PP.Face(tuple(t)) for t in f]
    return m


def mission_scene(bin_path, n, index_path=None, marker_size=None):
    """Build a Scene of typed markers at each spawn (info-only metadata attached)."""
    sc = Scene()
    sp = spawns(bin_path, n, index_path)
    if marker_size is None and sp:
        xs = [s["x"] for s in sp]; zs = [s["z"] for s in sp]
        extent = max(max(xs) - min(xs), max(zs) - min(zs), 1000)
        marker_size = max(extent * 0.018, 300)     # ~2% of world span
    for s in sp:
        meta = {
            "type": s["typ"], "block": s["blk"],
            "pos": (s["x"], s["y"], s["z"]),
            "rot_deg": round(s["rot"] * 360 / 4096) % 360,
            "params": s["params"],
        }
        sc.add(SceneObject(
            name=f"#{s['i']} type{s['typ']}",
            mesh=_marker_mesh(marker_size),
            rot_y=s["rot"] * 2 * math.pi / 4096,
            trans=(s["x"], s["y"], s["z"]),
            color=type_color(s["typ"]),
            meta=meta,
        ))
    return sc, sp
