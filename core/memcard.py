"""
core/memcard.py — PS1 memory-card (.mcd/.mcr/.srm/raw 128K) reader/writer.

Presents a card as a directory of save files. Decodes each save's standard PS1
icon (palette@0x60, 16x16 4bpp@0x80, BGR555) so the browser can show a thumbnail.
Identifies Armored Core 1 saves (product code SCUS-94182 / SLUS-01323, title
"ARMOREDCORE01") — only those are openable for emblem editing; everything else is
listed read-only / out of scope.

Card layout (standard 128 KiB, 16 blocks × 8192 B):
  block 0 = directory. Frame 0 (128 B) = 'MC' header.
            Frames 1..15 (128 B each) = directory entry for blocks 1..15:
              +0x00 u32 state  (0x51 first/in-use, 0x52 mid-link, 0x53 end-link,
                                0xA0 free)
              +0x04 u32 file size (bytes; in the first-block entry)
              +0x08 u16 next-block link (0xFFFF = none)
              +0x0A filename  ASCII, NUL-terminated (e.g. "BASCUS-94182A")
              +0x7F u8 XOR checksum of bytes 0x00..0x7E
  blocks 1..15 = save data (8192 B). First block begins with the title frame:
              +0x00 'SC'; +0x02 icon flag (0x11/12/13 = 1/2/3 frames);
              +0x04 title Shift-JIS (64 B); +0x60 icon CLUT (16×u16 BGR555);
              +0x80.. icon frame(s), 16x16 4bpp = 128 B each.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from pathlib import Path

BLOCK = 8192
FRAME = 128
N_BLOCKS = 16

# AC1 (USA) save signatures. The on-card product code is fixed by the game
# regardless of disc reprint; we also accept the title string as a fallback.
AC1_CODES = ("SCUS-94182", "SLUS-01323", "SCUS94182", "SLUS01323")
AC1_TITLES = ("ARMOREDCORE01", "ARMOREDCORE")


def _bgr555(w: int):
    if w == 0:
        return (0, 0, 0, 0)            # 0x0000 = transparent on PS1 icons
    r = (w & 0x1F) << 3
    g = ((w >> 5) & 0x1F) << 3
    b = ((w >> 10) & 0x1F) << 3
    return (r, g, b, 255)


@dataclass
class SaveFile:
    slot: int                  # directory frame / first block index (1..15)
    code: str                  # raw filename / product code
    title: str                 # decoded Shift-JIS title
    size_blocks: int
    block_indices: list[int]   # data blocks making up this file (in link order)
    icon_frames: int
    is_ac1: bool
    data_offset: int           # byte offset of the first data block in the card
    is_emblem: bool = False    # AC1 "ARMORED CORE EMBLEM DATA" file (holds 7 emblems)

    @property
    def label(self) -> str:
        return self.title or self.code


class MemoryCard:
    def __init__(self, path):
        self.path = Path(path)
        self.raw = bytearray(self.path.read_bytes())
        if len(self.raw) < BLOCK * N_BLOCKS:
            raise ValueError(f"not a 128K PS1 card: {len(self.raw)} bytes")
        self.saves: list[SaveFile] = []
        self._parse_directory()

    # ---- directory ---------------------------------------------------------
    def _dir_frame(self, i: int) -> bytes:
        return bytes(self.raw[i * FRAME:(i + 1) * FRAME])

    def _parse_directory(self):
        self.saves.clear()
        for slot in range(1, N_BLOCKS):
            fr = self._dir_frame(slot)
            state = struct.unpack_from("<I", fr, 0)[0]
            if state != 0x51:                      # only first-block entries
                continue
            size = struct.unpack_from("<I", fr, 4)[0]
            code = fr[0x0A:0x0A + 20].split(b"\x00")[0].decode("ascii", "replace")
            # follow the link chain for multi-block saves
            blocks = [slot]
            link = struct.unpack_from("<h", fr, 8)[0]
            guard = 0
            while link != -1 and 1 <= link + 1 < N_BLOCKS and guard < N_BLOCKS:
                b = link + 1
                blocks.append(b)
                lf = self._dir_frame(b)
                link = struct.unpack_from("<h", lf, 8)[0]
                guard += 1
            data_off = slot * BLOCK
            title, frames = self._read_title(data_off)
            code_u = code.upper()
            is_ac1 = (any(c in code_u for c in AC1_CODES) or
                      any(t in title.upper().replace(" ", "") for t in AC1_TITLES))
            is_emblem = is_ac1 and "EMBLEM" in title.upper()
            self.saves.append(SaveFile(
                slot=slot, code=code, title=title,
                size_blocks=max(1, size // BLOCK), block_indices=blocks,
                icon_frames=frames, is_ac1=is_ac1, data_offset=data_off,
                is_emblem=is_emblem,
            ))

    def _read_title(self, off: int):
        blk = self.raw[off:off + BLOCK]
        if blk[0:2] != b"SC":
            return "", 0
        frames = {0x11: 1, 0x12: 2, 0x13: 3}.get(blk[2], 1)
        raw = bytes(blk[0x04:0x04 + 64]).split(b"\x00")[0]
        try:
            title = raw.decode("shift_jis").strip()
        except Exception:
            title = raw.decode("ascii", "replace")
        # PS1 titles use full-width chars; normalise to ASCII where possible
        title = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E
                        else (" " if c == "　" else c) for c in title).strip()
        return title, frames

    # ---- icon --------------------------------------------------------------
    def icon_rgba(self, save: SaveFile, frame: int = 0):
        """Return (w, h, rgba_bytes) for an icon frame, or None."""
        off = save.data_offset
        blk = self.raw[off:off + BLOCK]
        if blk[0:2] != b"SC":
            return None
        pal = [_bgr555(struct.unpack_from("<H", blk, 0x60 + i * 2)[0]) for i in range(16)]
        ic = 0x80 + frame * 0x80
        out = bytearray()
        for i in range(16 * 16):
            b = blk[ic + i // 2]
            idx = (b & 0x0F) if i % 2 == 0 else (b >> 4)
            out += bytes(pal[idx])
        return 16, 16, bytes(out)

    # ---- raw block access (for emblem read/write) --------------------------
    def block_bytes(self, slot: int) -> bytes:
        return bytes(self.raw[slot * BLOCK:(slot + 1) * BLOCK])

    def write_block(self, slot: int, data: bytes):
        if len(data) != BLOCK:
            raise ValueError("block must be 8192 bytes")
        self.raw[slot * BLOCK:(slot + 1) * BLOCK] = data

    def patch(self, slot: int, offset: int, data: bytes):
        """Write `data` at `offset` within block `slot`'s data."""
        base = slot * BLOCK + offset
        self.raw[base:base + len(data)] = data

    # ---- multi-block save access (emblem file spans 2 blocks) --------------
    def file_bytes(self, save: SaveFile) -> bytes:
        """Concatenate a save's data blocks in link order (file-relative bytes)."""
        return b"".join(self.block_bytes(b) for b in save.block_indices)

    def write_file_bytes(self, save: SaveFile, data: bytes):
        """Write file-relative bytes back across the save's blocks. A single
        emblem's 2048-B pixel run can straddle the 8192-B block boundary, so
        edits must round-trip through the whole file, not one block."""
        span = BLOCK * len(save.block_indices)
        if len(data) != span:
            raise ValueError(f"data must be {span} bytes for this {len(save.block_indices)}-block file")
        for n, b in enumerate(save.block_indices):
            self.write_block(b, data[n * BLOCK:(n + 1) * BLOCK])

    def emblem_file(self) -> "SaveFile | None":
        """The AC1 'ARMORED CORE EMBLEM DATA' save, if present on the card."""
        return next((s for s in self.saves if s.is_emblem), None)

    def _fix_dir_checksum(self, slot: int):
        base = slot * FRAME
        x = 0
        for i in range(0x7F):
            x ^= self.raw[base + i]
        self.raw[base + 0x7F] = x

    def save(self, path=None):
        """Recompute directory checksums and write the card back to disk."""
        for slot in range(1, N_BLOCKS):
            self._fix_dir_checksum(slot)
        Path(path or self.path).write_bytes(bytes(self.raw))


# ---------------------------------------------------------------------------
# Armored Core 1 emblems (player-drawn decals)
# ---------------------------------------------------------------------------
# Ground-truthed against a real AC1 (regular, SLUS-01323) DuckStation card
# (2026-06-13): the emblems do NOT live in the main save — they are stored in a
# separate card file, code ...Z, title "ARMORED CORE EMBLEM DATA" (2 blocks /
# 16384 B). It holds 7 emblems, each a SELF-CONTAINED record (its own palette,
# like a 4bpp TIM's CLUT + pixels) laid out back-to-back at stride 0x820:
#     +0x00   32 B   palette: 16 x u16 BGR555. Index 0 = 0x0000 = transparent
#                     (the emblem background); index 1 is the first drawable colour
#                     (red on a fresh card) ... index 15 white.
#     +0x20   2048 B pixels: 64x64, 4bpp, low-nibble first, row-major.
# Record 0 starts at file offset 0x204; record i = 0x204 + i*0x820. (Offsets are
# file-relative, i.e. into the file's blocks joined in link order.) The 7 records
# end at 0x3ae4, followed by 0xFF padding.
#
# A u32 LE byte-sum checksum of all 7 records (0x204..0x3AE4) sits at file offset
# 0x200, just before record 0. The game validates it on load — a stale value
# yields "DATA READ FAILED" in-game even though the pixels are correct. Any write
# MUST recompute it (write_emblem / patch_mcs_emblem do, via _fix_emblem_checksum).
#
# NB each emblem has its OWN palette — editing one emblem's colours does NOT touch
# the others. They currently all carry the identical default rainbow ramp, which
# is why a wrong "single shared palette @0x204, stride 0x800" read still decoded
# plausibly; it does not. A record's 2048-B pixel run can straddle the 8192-B
# block boundary, so always operate on the joined file bytes (MemoryCard.file_bytes
# / write_file_bytes).
EMBLEM_W = EMBLEM_H = 64
EMBLEM_BPP = 4
EMBLEM_COUNT = 7
EMBLEM_REC0 = 0x204            # file offset of emblem 0's record (its palette)
EMBLEM_STRIDE = 0x820          # 2080 = 32-B palette + 2048-B pixels, per emblem
EMBLEM_PAL_LEN = 32            # 16 x u16 BGR555; index 0 = 0x0000 transparent
EMBLEM_BYTES = EMBLEM_W * EMBLEM_H // 2   # 2048
# u32 LE at file offset 0x200 (just before record 0). The game validates it on
# load and shows "DATA READ FAILED" on mismatch: it is the plain byte-sum of all
# 7 emblem records (0x204..0x3AE4). Must be recomputed after editing any emblem.
EMBLEM_CKSUM_OFF = 0x200


def _emblem_checksum(file_bytes) -> int:
    end = EMBLEM_REC0 + EMBLEM_COUNT * EMBLEM_STRIDE   # 0x3AE4
    return sum(file_bytes[EMBLEM_REC0:end]) & 0xFFFFFFFF


def _fix_emblem_checksum(fb: bytearray) -> None:
    """Rewrite the 0x200 byte-sum checksum over the 7 emblem records in-place."""
    struct.pack_into("<I", fb, EMBLEM_CKSUM_OFF, _emblem_checksum(fb))


def emblem_record_off(index: int) -> int:
    """File offset of emblem `index`'s record (its 32-B palette)."""
    if not 0 <= index < EMBLEM_COUNT:
        raise IndexError(f"emblem index {index} out of range 0..{EMBLEM_COUNT-1}")
    return EMBLEM_REC0 + index * EMBLEM_STRIDE


def emblem_pix_off(index: int) -> int:
    """File offset of emblem `index`'s 2048-B pixel data."""
    return emblem_record_off(index) + EMBLEM_PAL_LEN


def _emblem_pal_rgba(file_bytes: bytes, index: int = 0):
    """16 (r,g,b,a) tuples from emblem `index`'s own palette; idx 0 -> alpha 0."""
    rec = emblem_record_off(index)
    return [_bgr555(struct.unpack_from("<H", file_bytes, rec + i * 2)[0])
            for i in range(16)]


def emblem_palette(file_bytes: bytes, index: int = 0):
    """16 (r,g,b) tuples from emblem `index`'s own palette (for UI swatches).
    Entry 0 is the transparent background."""
    return [c[:3] for c in _emblem_pal_rgba(file_bytes, index)]


def decode_emblem(file_bytes: bytes, index: int = 0):
    """Decode one 64x64 4bpp emblem to (w, h, rgba_bytes) using its own palette.
    Palette index 0 renders transparent (alpha 0), matching the in-game emblem
    background."""
    pal = _emblem_pal_rgba(file_bytes, index)
    pix = emblem_pix_off(index)
    out = bytearray()
    for i in range(EMBLEM_W * EMBLEM_H):
        b = file_bytes[pix + i // 2]
        idx = (b & 0x0F) if i % 2 == 0 else (b >> 4)
        out += bytes(pal[idx])
    return EMBLEM_W, EMBLEM_H, bytes(out)


def is_emblem_blank(file_bytes: bytes, index: int = 0) -> bool:
    pix = emblem_pix_off(index)
    seg = file_bytes[pix:pix + EMBLEM_BYTES]
    return all(b == 0xFF for b in seg) or all(b == 0x00 for b in seg)


def _to_bgr555(r: int, g: int, b: int) -> int:
    """Pack opaque RGB to PS1 BGR555. 0x0000 is the transparent sentinel, so pure
    black is emitted as 0x8000 (the 'solid'/STP bit set) to stay visible — this is
    how the game's own palette stores opaque black."""
    w = ((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3)
    return w if w else 0x8000


def _nearest(rgb, palette) -> int:
    """Index of the closest colour in `palette` (list of (r,g,b)) to `rgb`."""
    r, g, b = rgb
    best_i, best_d = 0, 1 << 30
    for i, (pr, pg, pb) in enumerate(palette):
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def build_emblem(image_path, alpha_threshold: int = 128):
    """Quantise an image (GIF/PNG/…, any size) into a *custom* 16-colour emblem
    and return (palette_bytes[32], pixel_bytes[2048]).

    Transparency is honoured: a source pixel with alpha < `alpha_threshold` (GIF
    transparent index or PNG alpha) maps to palette index 0, which is reserved as
    transparent (0x0000). Only the OPAQUE pixels drive the palette, so the colour
    sitting behind the transparent area never steals a slot. The remaining up-to-15
    colours come from the image itself: if it already has <=15 distinct opaque
    colours they are kept exactly, otherwise they're median-cut to 15. Each emblem
    owns its palette, so this only affects the emblem being written."""
    from PIL import Image
    img = Image.open(image_path)
    try:
        img.seek(0)                       # first frame of an animated GIF
    except Exception:
        pass
    img = img.convert("RGBA").resize((EMBLEM_W, EMBLEM_H), Image.NEAREST)
    px = list(img.getdata())              # [(r,g,b,a), ...]
    opaque = [(r, g, b) for (r, g, b, a) in px if a >= alpha_threshold]
    uniq = list(dict.fromkeys(opaque))    # distinct opaque colours, in first-seen order
    if len(uniq) <= 15:
        colors = uniq                     # few enough — keep them exactly
    else:
        strip = Image.new("RGB", (len(opaque), 1)); strip.putdata(opaque)
        q = strip.quantize(colors=15, method=Image.Quantize.MEDIANCUT)
        qpal = q.getpalette()
        used = max(q.getdata()) + 1 if opaque else 0
        colors = [tuple(qpal[i * 3:i * 3 + 3]) for i in range(min(used, 15))]
    pal_u16 = [0x0000] + [_to_bgr555(*c) for c in colors]
    pal_u16 += [0x0000] * (16 - len(pal_u16))
    pal_bytes = b"".join(struct.pack("<H", c) for c in pal_u16)
    # map every pixel: transparent -> 0, else nearest of the chosen colours (+1).
    cache: dict = {}

    def to_index(c):
        j = cache.get(c)
        if j is None:
            j = _nearest(c, colors) + 1 if colors else 0
            cache[c] = j
        return j

    pixels = bytearray(EMBLEM_BYTES)
    for i, (r, g, b, a) in enumerate(px):
        idx = 0 if a < alpha_threshold else to_index((r, g, b))
        if i % 2 == 0:
            pixels[i // 2] |= idx
        else:
            pixels[i // 2] |= idx << 4
    return pal_bytes, bytes(pixels)


def write_emblem(file_bytes: bytes, index: int, image_path) -> bytes:
    """Return a copy of the joined file bytes with emblem `index` replaced by
    `image_path`, using a custom palette generated from the image. Writes the
    emblem's own 32-B palette + 2048-B pixels; the other emblems are untouched.
    Pass the result to MemoryCard.write_file_bytes."""
    pal_bytes, pixels = build_emblem(image_path)
    fb = bytearray(file_bytes)
    rec = emblem_record_off(index)
    fb[rec:rec + EMBLEM_PAL_LEN] = pal_bytes
    fb[rec + EMBLEM_PAL_LEN:rec + EMBLEM_PAL_LEN + EMBLEM_BYTES] = pixels
    _fix_emblem_checksum(fb)            # else the game rejects it: "DATA READ FAILED"
    return bytes(fb)


def read_card(path) -> MemoryCard:
    return MemoryCard(path)


# ---------------------------------------------------------------------------
# Single-save (.mcs) emblem patch — for writing back to a LIVE virtual card via
# DuckStation's import_memory_card_save (which ingests a single save file, not a
# whole .mcd). A .mcs is a 128-B directory frame followed by the save's data
# blocks, so the emblem records sit at MCS_HEADER + emblem_*_off(index).
# ---------------------------------------------------------------------------
MCS_HEADER = 128


def patch_mcs_emblem(mcs_path, index: int, image_path, out_path=None) -> str:
    """Write a PC image into emblem `index` of an exported single-save .mcs
    (the 'ARMORED CORE EMBLEM DATA' file), generating a custom palette from the
    image. The emblem's own 32-B palette and 2048-B pixels are written; the other
    emblems are untouched. Returns the output path. Push the result onto the
    running card with DuckStation import_memory_card_save."""
    raw = bytearray(Path(mcs_path).read_bytes())
    body = bytes(raw[MCS_HEADER:])
    if body[0:2] != b"SC":
        raise ValueError("not a single-save .mcs (no 'SC' after the 128-B frame)")
    # title is full-width Shift-JIS; normalise to ASCII before checking
    traw = body[0x04:0x44].split(b"\x00")[0]
    try:
        title = traw.decode("shift_jis")
    except Exception:
        title = traw.decode("ascii", "replace")
    title = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
                    for c in title).upper()
    if "EMBLEM" not in title:
        raise ValueError(f"this .mcs is not an ARMORED CORE EMBLEM DATA save (title={title!r})")
    pal_bytes, pixels = build_emblem(image_path)
    rec = MCS_HEADER + emblem_record_off(index)
    raw[rec:rec + EMBLEM_PAL_LEN] = pal_bytes
    raw[rec + EMBLEM_PAL_LEN:rec + EMBLEM_PAL_LEN + EMBLEM_BYTES] = pixels
    # recompute the records byte-sum (offset shifted past the 128-B .mcs header)
    s = MCS_HEADER + EMBLEM_REC0
    e = MCS_HEADER + EMBLEM_REC0 + EMBLEM_COUNT * EMBLEM_STRIDE
    struct.pack_into("<I", raw, MCS_HEADER + EMBLEM_CKSUM_OFF, sum(raw[s:e]) & 0xFFFFFFFF)
    out = out_path or mcs_path
    Path(out).write_bytes(bytes(raw))
    return out
