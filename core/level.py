"""
core/level.py — assemble a mission's WALKABLE LEVEL with real world placement.

Placement was solved live 2026-06-12 (AC_1_USA_RE/disc_map/trace/PLACEMENT_SOLVED.md):
mission N's FDAT entry 2N+1 is a chunk stream [u32 len][payload];
  chunk 0 = the level geometry BLOCKS (PA format, LOCAL coords, PRE-ROTATED on disc)
  chunk 7 = the SECTION PLACEMENT TABLE: 52-byte records, terminated by s16 -1 @ +6:
      +0x00 s16[3] world bbox min   +0x08 s16[3] world bbox max
      +0x10 s16[3] PLACEMENT TRANSLATION   (world = block_local*0.25 + translation;
                                            blocks are authored 4x — see SCALE)
      +0x1e s16    geometry block index    (blocks are REUSED -> instancing)
      +0x28 u8     lighting-record index
No per-section rotation is needed. Some missions (e.g. m28 Destroy Gun Emplacement)
load a SHARED scene (FDAT e200-style) instead and have no chunk-7 table — those
return an empty mesh here (documented open item).

Faces are classified floor / ceiling / wall by world-space normal (PSX Y is DOWN:
a walkable floor's winding normal points -Y). Ceilings can be dropped for the
"see into the level from above" view (the AC1MOD_VISION ceiling toggle).
"""
from __future__ import annotations
import struct, math
from core import pa_parser as PP
from core.fdat import mission_entry

HORIZ = 0.7          # |ny| above this = horizontal surface (floor or ceiling)

# Block geometry is authored at 4x the placement scale; the engine renders it
# through a down-scaling GTE matrix (docs call it the "1/8 scale" numerics; the NET
# geometry scale is 1/4). CONFIRMED universal: every mission's chunk-7 section bbox
# is exactly 0.25x its referenced block's bbox, and 2500-unit cell translations only
# tile without overlap once the 10000-unit blocks are scaled to 2500. Without this,
# each block floods 4x into its neighbours -> the "redundant interior walls" bug.
SCALE = 0.25

CLASS_COLORS = {     # face tint per class (textures aren't rendered, so tint by role)
    "floor":   (104, 144, 110),
    "wall":    (150, 152, 168),
    "ceiling": (88, 84, 120),
}


def _chunks(buf, limit=64):
    off = 0
    for idx in range(limit):
        if off + 4 > len(buf):
            return
        ln = struct.unpack_from("<I", buf, off)[0]
        if ln == 0 or off + 4 + ln > len(buf):
            return
        yield idx, off + 4, ln
        off += 4 + ln


def _blocks(buf):
    """Chunk 0 = a run of size-prefixed PA blocks from +8 to u32[0]."""
    if len(buf) < 12:
        return []
    geom_end = struct.unpack_from("<I", buf, 0)[0]
    out, off = [], 8
    while off < geom_end and off + 12 <= len(buf):
        sz = struct.unpack_from("<I", buf, off)[0]
        if sz < 12 or off + sz > len(buf):
            break
        out.append(buf[off:off + sz])
        off += sz
    return out


def placements(buf, nblocks):
    """[(block_index, (tx,ty,tz), light_idx)] from the chunk-7 table (empty if none)."""
    ch = {i: (o, l) for i, o, l in _chunks(buf)}
    if 7 not in ch:
        return []
    toff, tlen = ch[7]
    out = []
    for i in range(tlen // 52):
        r = buf[toff + i * 52: toff + i * 52 + 52]
        if len(r) < 52:
            break
        if struct.unpack_from("<h", r, 6)[0] == -1:   # terminator
            break
        p2 = struct.unpack_from("<3h", r, 0x10)
        blk = struct.unpack_from("<h", r, 0x1e)[0]
        if 0 <= blk < nblocks:
            out.append((blk, p2, r[0x28]))
    return out


def _face_class(verts, face):
    a, b, c = (verts[i] for i in face[:3])
    ux, uy, uz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
    vx, vy, vz = c[0]-a[0], c[1]-a[1], c[2]-a[2]
    nx, ny, nz = uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx
    m = math.sqrt(nx*nx + ny*ny + nz*nz) or 1.0
    ny /= m
    if abs(ny) > HORIZ:
        # PSX Y is DOWN (larger Y = lower in the world). A walkable FLOOR sits at the
        # BOTTOM of a room (larger Y) and its winding normal points +Y here; the CEILING
        # is up top (smaller Y) with the −Y normal. CORRECTED 2026-06-18 (was inverted —
        # confirmed by mean-Y of classified faces: the old "floor" set was the higher one;
        # the earlier "verified" note was wrong). The RE repo's assemble_levels.py still
        # has the old swapped convention and should be flipped to match.
        return "ceiling" if ny < 0 else "floor"
    return "wall"


def _canonical(tri_world):
    """Winding-preserving key for a triangle's 3 (rounded) world vertices.

    The lexicographically-smallest of the 3 cyclic rotations — identical for two
    faces with the SAME winding at the same place, but different for the reverse
    winding. Lets us drop same-wound duplicates while keeping a genuine two-sided
    (opposite-wound) pair, which both modes need."""
    a, b, c = tri_world
    return min(((a, b, c), (b, c, a), (c, a, b)))


def _dedupe(out):
    """Drop same-wound coincident duplicate faces in-place.

    Sections instanced edge-to-edge author the SHARED wall/floor face twice at the
    same place with the same winding (m30: ~2800 such faces). Two coincident faces
    z-fight under the two-sided z-buffer (the speckled shimmer) and double the
    apparent wall count; the game never shows both (NCLIP/OT draw one). We keep one
    per (position, winding) — so the rare genuine zero-thickness two-sided wall
    (opposite-wound pair, m30: 70) survives intact for the NCLIP view."""
    seen, kept = set(), []
    for fc in out.faces:
        # round on a quarter-unit grid (world coords are multiples of SCALE=0.25)
        # so distinct faces 0.25 apart aren't falsely merged.
        tri = tuple(tuple(round(c * 4) for c in out.vertices[i]) for i in fc.verts)
        key = _canonical(tri)
        if key in seen:
            continue
        seen.add(key)
        kept.append(fc)
    out.faces = kept
    return out


def level_mesh(bin_path, n, index_path=None, ceilings=True, tint=True, dedupe=True):
    """
    Mesh of mission N's assembled level (world coords). One group per placed
    section ("s<i>_b<blk>"); faces tinted by class; ceilings dropped if
    ceilings=False. Empty mesh if the mission has no chunk-7 placement table
    (shared-scene missions). dedupe=True merges same-wound coincident duplicate
    faces from instanced-section seams (see _dedupe)."""
    buf = mission_entry(bin_path, n, odd=True, index_path=index_path)
    blocks = _blocks(buf)
    plc = placements(buf, len(blocks))
    out = PP.Mesh()
    if not plc:
        return out
    cache = {}
    for si, (blk, (tx, ty, tz), light) in enumerate(plc):
        if blk not in cache:
            cache[blk] = PP.parse_block(blocks[blk])
        m = cache[blk]
        if not m.vertices:
            continue
        base = len(out.vertices)
        out.groups.append((f"s{si}_b{blk}", base, len(m.vertices)))
        out.vertices.extend((v[0]*SCALE+tx, v[1]*SCALE+ty, v[2]*SCALE+tz)
                            for v in m.vertices)
        for fc in m.faces:
            cls = _face_class(m.vertices, fc.verts)
            if cls == "ceiling" and not ceilings:
                continue
            color = CLASS_COLORS[cls] if tint else fc.color
            out.faces.append(PP.Face(tuple(base + i for i in fc.verts),
                                     color, fc.textured))
    return _dedupe(out) if dedupe else out


def level_obj_lines(bin_path, n, index_path=None):
    """OBJ export with `o floor/ceiling/wall` groups (matches the RE-repo extractor),
    so ceilings stay toggleable in external 3D tools."""
    buf = mission_entry(bin_path, n, odd=True, index_path=index_path)
    blocks = _blocks(buf)
    plc = placements(buf, len(blocks))
    V, grouped = [], {"floor": [], "ceiling": [], "wall": []}
    cache = {}
    for (blk, (tx, ty, tz), light) in plc:
        if blk not in cache:
            cache[blk] = PP.parse_block(blocks[blk])
        m = cache[blk]
        base = len(V)
        V.extend((v[0]*SCALE+tx, v[1]*SCALE+ty, v[2]*SCALE+tz) for v in m.vertices)
        for fc in m.faces:
            grouped[_face_class(m.vertices, fc.verts)].append(
                tuple(base + i for i in fc.verts))
    L = [f"# AC1mod level export — mission {n} (world coords, placement chunk 7)"]
    for v in V:
        L.append(f"v {v[0]} {v[1]} {v[2]}")
    for name in ("floor", "ceiling", "wall"):
        L.append(f"o {name}")
        for f in grouped[name]:
            L.append("f " + " ".join(str(i + 1) for i in f))
    return L, len(V), {k: len(v) for k, v in grouped.items()}
