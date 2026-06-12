"""
core/pa_parser.py — AC1 PA##.T stage/object geometry decoder for AC1mod.

Vendored from the reverse-engineering repo's validated decoder
(AC_1_USA_RE/tools/pa_obj.py). KEEP IN SYNC with that file — it is the source of
truth (see AC_1_USA_RE/docs/PA_FORMAT.md, REFERENCE.md §11).

A PA##.T file is a count-first ".T" container; entries 2..N are size-prefixed
geometry blocks. Each block holds a stride-28 sub-object table (RE'd from the
relocation walker FUN_800574D8): per sub-object a vertex pool (int16 x,y,z) and a
stream of variable-length primitive records (tri/quad, flat/textured) whose uint16
indices are pool-relative. We decode that into a flat Mesh for rendering/export.
"""
from __future__ import annotations
import struct, collections, math
from dataclasses import dataclass, field

RAW, SECDATA_OFF, DATA = 2352, 24, 2048   # Mode-2/Form-1 sector geometry

# Per primitive type (byte[3] & 0xBC): (first vertex-index byte offset, #verts, stride).
# RE'd LIVE from the relocation-walker per-type handlers (jump table 0x8004B184),
# disassembled from the running game (2026-06-11). In each handler a1 = record+4;
# halfwords shifted <<4 (×16) are VERTEX-position indices, <<3 (×8) are normal/colour
# indices. Flat/textured types pack their vertex indices CONTIGUOUSLY (stride 2);
# GOURAUD 0x34/0x3c interleave [normal_idx, vertex_idx] pairs → vertex stride 4.
# CONFIRMED: 0x20,0x28,0x24,0x2c.  RESOLVED 2026-06-11: gouraud 0x34/0x3c (was the
# 5-30% out-of-range bug — old contiguous-from-0x14 read normal indices as vertices;
# new layout = 0 OOR across all 72 PA files. See AC_1_USA_RE/docs/PA_FORMAT.md).
PRIM_VERTS = {
    0x20: (0x0a, 3, 2), 0x28: (0x0a, 4, 2),   # flat tri / quad        CONFIRMED
    0x24: (0x12, 3, 2), 0x2c: (0x16, 4, 2),   # textured tri / quad    CONFIRMED
    0x34: (0x12, 3, 4), 0x3c: (0x16, 4, 4),   # gouraud tri / quad     RESOLVED live
    0xa0: (0x0a, 3, 2), 0xa8: (0x0a, 4, 2),
    0xa4: (0x12, 3, 2), 0xac: (0x16, 4, 2),
    0xb4: (0x12, 3, 2), 0xbc: (0x12, 4, 2),   # high-bit textured variants (stride unverified)
}


def _textured(t: int) -> bool:
    return bool(t & 0x80) or t in (0x24, 0x2c, 0x34, 0x3c)


@dataclass
class Face:
    verts: tuple              # global indices into Mesh.vertices
    color: tuple = (170, 170, 170)
    textured: bool = False


@dataclass
class Mesh:
    vertices: list = field(default_factory=list)   # [(x,y,z)]
    faces: list = field(default_factory=list)      # [Face]
    groups: list = field(default_factory=list)     # [(label, vstart, vcount)]

    def bbox(self):
        if not self.vertices:
            return None
        xs = [v[0] for v in self.vertices]; ys = [v[1] for v in self.vertices]
        zs = [v[2] for v in self.vertices]
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def stats(self):
        return dict(verts=len(self.vertices), faces=len(self.faces),
                    groups=len(self.groups))


# ---------------------------------------------------------------- container ----

def read_container(bin_path, sector_start, sector_end):
    """Read a .T file's user-data sectors and split into entry payloads."""
    blob = bytearray()
    with open(bin_path, "rb") as f:
        for s in range(sector_start, sector_end + 1):
            f.seek(s * RAW + SECDATA_OFF)
            blob += f.read(DATA)
    blob = bytes(blob)
    n = struct.unpack_from("<H", blob, 0)[0]
    offs = list(struct.unpack_from(f"<{n+1}H", blob, 2))
    return [blob[offs[i] * 2048: offs[i + 1] * 2048] for i in range(n)]


def is_geometry_block(entry: bytes) -> bool:
    return len(entry) >= 12 and struct.unpack_from("<I", entry, 0)[0] == len(entry)


# ------------------------------------------------------------------ decode -----

def _parse_subobjects(block):
    a0 = struct.unpack_from("<I", block, 8)[0]
    reloc = a0 + 12
    if reloc + 8 > len(block):
        return reloc, []
    count = struct.unpack_from("<I", block, a0 + 8)[0]
    tbl = a0 + 12
    subs = []
    for i in range(count):
        o = tbl + i * 28
        if o + 28 > len(block):
            break
        f0, vcnt, f8 = struct.unpack_from("<3I", block, o)
        flags = struct.unpack_from("<H", block, o + 0x0e)[0]
        prim_off = struct.unpack_from("<I", block, o + 0x10)[0]
        prim_cnt_base = struct.unpack_from("<H", block, o + 0x14)[0]
        if flags & 0x8000:
            continue
        subs.append(dict(index=i, vtx_off=f0 + reloc, vtx_cnt=vcnt,
                         prim_off=prim_off + reloc,
                         prim_cnt=prim_cnt_base + (flags & 0x1ff) - 1))
    return reloc, subs


def _read_verts(block, off, cnt):
    out = []
    for i in range(cnt):
        p = off + i * 8
        if p + 8 > len(block):
            break
        x, y, z, _ = struct.unpack_from("<4h", block, p)
        out.append((x, y, z))
    return out


def _read_prims(block, off, cnt):
    o, n, out = off, 0, []
    while n < cnt and o + 4 <= len(block):
        b1 = block[o + 1]
        typ = block[o + 3] & 0xbc
        reclen = 4 + b1 * 4
        if reclen < 4 or o + reclen > len(block):
            break
        info = PRIM_VERTS.get(typ)
        if info:
            voff, nv, stride = info
            idx = [struct.unpack_from("<H", block, o + voff + stride * k)[0]
                   for k in range(nv) if o + voff + stride * k + 2 <= len(block)]
            # flat types carry an RGB colour word right after the 4-byte header
            color = (170, 170, 170)
            if not _textured(typ) and o + 7 <= len(block):
                color = (block[o + 4], block[o + 5], block[o + 6])
            out.append((typ, idx, color))
        o += reclen
        n += 1
    return out, o


def parse_block(block) -> Mesh:
    """Decode one geometry block into a Mesh (sub-objects concatenated)."""
    m = Mesh()
    _, subs = _parse_subobjects(block)
    for s in subs:
        verts = _read_verts(block, s["vtx_off"], s["vtx_cnt"])
        prims, _ = _read_prims(block, s["prim_off"], s["prim_cnt"])
        base = len(m.vertices)
        m.groups.append((f"sub{s['index']}", base, len(verts)))
        m.vertices.extend(verts)
        nv = len(verts)
        for (typ, idx, color) in prims:
            idx = [i for i in idx if i < nv]
            if len(set(idx)) < 3:
                continue
            g = tuple(base + i for i in idx)
            if len(g) == 3:
                m.faces.append(Face(g, color, _textured(typ)))
            elif len(g) == 4:
                # PSX 4-pt polys use Z/N vertex order (diagonal = v1-v2), so the
                # two tris are (0,1,2) and (1,3,2) — NOT a (0,1,2)+(0,2,3) fan.
                m.faces.append(Face((g[0], g[1], g[2]), color, _textured(typ)))
                m.faces.append(Face((g[1], g[3], g[2]), color, _textured(typ)))
    return m


def parse_pa_blocks(bin_path, sector_start, sector_end):
    """Return [(entry_index, Mesh)] for every geometry block in the PA file."""
    ents = read_container(bin_path, sector_start, sector_end)
    out = []
    for i, e in enumerate(ents):
        if is_geometry_block(e):
            try:
                mesh = parse_block(e)
            except Exception:
                continue
            if mesh.vertices:
                out.append((i, mesh))
    return out


def largest_block(blocks):
    """Pick the (entry_index, Mesh) with the most faces — the 'main' object."""
    return max(blocks, key=lambda b: len(b[1].faces), default=None)


def contact_sheet(bin_path, sector_start, sector_end, cell=100.0, gap=1.5) -> Mesh:
    """
    All geometry blocks as a NORMALIZED GRID: each block is centred and scaled so
    its largest dimension == `cell`, then placed in a square grid. A genuine
    contact sheet of every object in the PA file (small props and big stages show
    at comparable size), for figuring out *what* a PA file contains.
    """
    blocks = parse_pa_blocks(bin_path, sector_start, sector_end)
    out = Mesh()
    n = len(blocks)
    if not n:
        return out
    cols = max(1, math.ceil(math.sqrt(n)))
    step = cell * gap
    for k, (ei, m) in enumerate(blocks):
        bb = m.bbox()
        if not bb:
            continue
        cx, cy, cz = (bb[0]+bb[3])/2, (bb[1]+bb[4])/2, (bb[2]+bb[5])/2
        dim = max(bb[3]-bb[0], bb[4]-bb[1], bb[5]-bb[2], 1.0)
        s = cell / dim
        gx = (k % cols) * step
        gy = -(k // cols) * step
        base = len(out.vertices)
        out.groups.append((f"e{ei}", base, len(m.vertices)))
        out.vertices.extend(((v[0]-cx)*s + gx, (v[1]-cy)*s + gy, (v[2]-cz)*s)
                            for v in m.vertices)
        for fc in m.faces:
            out.faces.append(Face(tuple(base + i for i in fc.verts),
                                  fc.color, fc.textured))
    return out


def scene_mesh(bin_path, sector_start, sector_end, coord_limit=12000) -> Mesh:
    """
    Assemble a STAGE: merge all geometry blocks at their RAW (world) coordinates.

    AC1 authors a stage's environment geometry directly in world space, so merging
    the blocks unmodified reconstructs the actual level layout. Blocks whose bbox
    exceeds `coord_limit` are skipped — those are the object/effect slots (e125+,
    not-yet-cleanly-decoded sprites/MT models that share a local origin and would
    pile up at the centre); they're placed per-instance by mission data, not here.
    """
    out = Mesh()
    for ei, m in parse_pa_blocks(bin_path, sector_start, sector_end):
        bb = m.bbox()
        if not bb or max(abs(v) for v in bb) > coord_limit:
            continue
        base = len(out.vertices)
        out.groups.append((f"e{ei}", base, len(m.vertices)))
        out.vertices.extend(m.vertices)
        for f in m.faces:
            out.faces.append(Face(tuple(base + i for i in f.verts), f.color, f.textured))
    return out


# back-compat alias (old name)
combined_mesh = contact_sheet
