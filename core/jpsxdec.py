"""
Wrapper around the jPSXdec JAR for subprocess calls.
All output paths are relative to the caller's working directory.
"""
import subprocess
import re
import struct
import math
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("psxmod")


JPSXDEC_JAR = Path(__file__).parent.parent / "jpsxdec" / "jpsxdec.jar"


def _run(args: list[str], cwd: Path = None) -> tuple[int, str, str]:
    """Run jPSXdec with given args. If cwd is None, use repository root.

    Returns (returncode, stdout, stderr).
    """
    # Ensure cwd is a Path or None
    repo_root = Path(__file__).parent.parent.resolve()
    if cwd is None:
        cwd = repo_root
    else:
        cwd = Path(cwd).resolve()

    cmd = ["java", "-jar", str(JPSXDEC_JAR)] + args
    log.debug(f"Running jpsxdec: {' '.join(cmd)}  cwd={cwd}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))
    except FileNotFoundError as e:
        log.error("Java not found: %s", e)
        return 1, "", str(e)

    if result.returncode != 0:
        log.warning("jPSXdec returned %s: stdout=%s stderr=%s", result.returncode, result.stdout[:200], result.stderr[:200])
    else:
        log.debug("jPSXdec finished: stdout=%s", result.stdout[:200])
    return result.returncode, result.stdout, result.stderr


def build_index(bin_path: Path, index_path: Path) -> tuple[bool, str]:
    """Run jPSXdec -f <bin> -x <index> to build the index file."""
    rc, out, err = _run(["-f", str(bin_path), "-x", str(index_path)])
    return rc == 0, err or out


def extract_all(index_path: Path, out_dir: Path) -> tuple[bool, str]:
    """
    Extract every item in the index to out_dir.
    Uses -x <index> -a to process all items.
    Images are saved as PNG. Files extracted as-is.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Remove existing .tim/.png files to enforce overwrite policy
    try:
        for p in out_dir.rglob("*.png"):
            try:
                p.unlink()
            except OSError:
                log.debug("Could not remove %s", p)
        for p in out_dir.rglob("*.tim"):
            try:
                p.unlink()
            except OSError:
                log.debug("Could not remove %s", p)
    except Exception:
        # keep going even if cleanup fails
        log.debug("Cleanup of existing images failed or skipped")

    # Run jPSXdec from repository root and let the -dir option control output
    # Process only image items so TIM/Images are decoded
    # Request saving original TIM files for image items (no decoding)
    rc, out, err = _run([
        "-x", str(index_path), "-a", "image", "-imgfmt", "tim", "-dir", str(out_dir)
    ])
    return rc == 0, err or out


def replace_tim(index_path: Path, item_number: int, new_tim_path: Path, bin_path: Path) -> tuple[bool, str]:
    """
    Replace a TIM image in the BIN using jPSXdec's -replacetim flag.
    jPSXdec writes directly into the BIN file at bin_path.
    """
    rc, out, err = _run([
        "-x", str(index_path),
        "-i", str(item_number),
        "-replacetim", str(new_tim_path),
    ])
    return rc == 0, err or out


def save_tim_for_item(index_path: Path, item_number: int, out_dir: Path) -> tuple[bool, str]:
    """Invoke jPSXdec to save the TIM for a single index item number into out_dir.

    Returns (ok, output_message).
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run([
        "-x", str(index_path),
        "-i", str(item_number),
        "-imgfmt", "tim",
        "-dir", str(out_dir),
    ])
    return rc == 0, err or out


# ---------------------------------------------------------------------------
# Index file parser
# ---------------------------------------------------------------------------

@dataclass
class IndexEntry:
    number: int
    name: str
    full_id: str           # full jPSXdec ID e.g. "GG/COM/RTIM.T[0]"
    entry_type: str        # "File", "Image", "Video", "Audio"
    sector_start: int
    sector_end: int
    details: str           # raw details string
    parent_name: Optional[str] = None

    # Parsed image fields (only for Image type)
    width: Optional[int] = None
    height: Optional[int] = None
    palette_count: Optional[int] = None

    # Parsed audio fields (only for Audio type)
    audio_sample_rate: Optional[int] = None
    audio_channels: Optional[int] = None

    # Paths set after extraction
    png_path: Optional[Path] = None
    tim_path: Optional[Path] = None
    wav_path: Optional[Path] = None

    # Multiple extracted variants (palette/frame variants)
    png_paths: list[Path] = field(default_factory=list)
    tim_paths: list[Path] = field(default_factory=list)
    wav_paths: list[Path] = field(default_factory=list)

    # Replacement state
    replacement_gif: Optional[Path] = None
    replacement_valid: Optional[bool] = None
    validation_errors: list[str] = field(default_factory=list)

    @property
    def is_image(self) -> bool:
        return self.entry_type == "Image"

    @property
    def is_audio(self) -> bool:
        return self.entry_type == "Audio"

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def indent_level(self) -> int:
        return 1 if re.search(r'\[\d+\]', self.name) else 0


def parse_index_file(index_path: Path) -> list[IndexEntry]:
    """
    Parse jPSXdec's .idx file into a list of IndexEntry objects.

    jPSXdec v2.x writes key:value pipe-delimited lines like:
      #:330|ID:GG/COM/RTIM.T[0]|Sectors:85348-85364|Type:Tim|Dimensions:128x256|Palettes:1
      #:4|ID:GG/BGM/BGM00.XA|Sectors:124-22795|Type:File|Size:46432256|Path:...|...
      #:2579|ID:GG/STR/ACED1.STR[0]|Sectors:...|Type:Video|Dimensions:320x240|...

    TIM image types appear as Type:Tim (not Type:Image).
    The ID field contains the full path including directory prefix.
    """
    entries = []
    if not index_path.exists():
        return entries

    current_parent = None  # most recent non-child entry name

    with open(index_path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("Log") or line.startswith("jPSXdec"):
                continue

            # Parse all key:value fields from the pipe-delimited line
            fields: dict[str, str] = {}
            for part in line.split("|"):
                part = part.strip()
                if ":" in part:
                    k, _, v = part.partition(":")
                    fields[k.strip()] = v.strip()

            # Must have at minimum # and ID and Type
            if "#" not in fields or "ID" not in fields or "Type" not in fields:
                continue

            try:
                number = int(fields["#"])
            except ValueError:
                continue

            full_id = fields["ID"]          # e.g. "GG/COM/RTIM.T[8]"
            # Use just the filename portion as display name
            name = full_id.split("/")[-1]   # e.g. "RTIM.T[8]"

            raw_type = fields["Type"]       # File, Tim, XA, Video, etc.

            # Normalise type for our purposes
            if raw_type == "Tim":
                entry_type = "Image"
            elif raw_type in ("XA", "SPU", "Audio", "ADPCM", "IKI_Audio"):
                entry_type = "Audio"
            elif raw_type == "Video":
                entry_type = "Video"
            else:
                entry_type = "File"

            # Sectors: "85348-85364"
            sector_start = sector_end = 0
            sectors_str = fields.get("Sectors", "")
            m = re.match(r'(\d+)-(\d+)', sectors_str)
            if m:
                sector_start = int(m.group(1))
                sector_end = int(m.group(2))

            # Build a details string for display
            details = fields.get("Size", "")

            # Determine parent — child items have bracketed suffix like [0] or [0.0]
            # Use base name (strip bracket suffix) so parent lookup is robust
            is_child = bool(re.search(r'\[[\d.]+\]', name))
            base_name = re.sub(r'\[[\d.]+\].*$', '', name)
            if is_child:
                parent = base_name
            else:
                parent = None
                current_parent = name

            entry = IndexEntry(
                number=number,
                name=name,
                full_id=full_id,
                entry_type=entry_type,
                sector_start=sector_start,
                sector_end=sector_end,
                details=details,
                parent_name=parent,
            )

            # Parse image dimensions for TIM entries
            if entry_type == "Image":
                dim = fields.get("Dimensions", "")
                m = re.match(r'(\d+)x(\d+)', dim)
                if m:
                    entry.width = int(m.group(1))
                    entry.height = int(m.group(2))
                pal = fields.get("Palettes", "")
                if pal.isdigit():
                    entry.palette_count = int(pal)

            # Parse audio metadata where jPSXdec provides it
            if entry_type == "Audio":
                for key in ("SampleRate", "SamplesPerSecond", "Freq"):
                    if key in fields and fields[key].isdigit():
                        entry.audio_sample_rate = int(fields[key])
                        break
                for key in ("Channels", "Ch"):
                    if key in fields and fields[key].isdigit():
                        entry.audio_channels = int(fields[key])
                        break

            entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# TIM file header parser  (Sony Psy-Q File Formats spec, Chapter 3)
# ---------------------------------------------------------------------------

@dataclass
class TimInfo:
    bpp: int               # 4, 8, 16, or 24
    has_clut: bool
    clut_x: int
    clut_y: int
    clut_width: int        # number of CLUT entries per row
    clut_height: int       # number of CLUT rows (palettes)
    pixel_x: int           # VRAM destination X
    pixel_y: int           # VRAM destination Y
    pixel_width: int       # stored width in VRAM 16-bit words
    pixel_height: int
    image_width: int       # actual pixel width (accounts for bpp packing)
    image_height: int
    file_size: int


def decode_tim_to_rgba(tim_path: Path, palette_index: int = 0) -> Optional[tuple[int, int, bytes]]:
    """Decode a TIM file to raw RGBA8888 bytes.

    Returns (width, height, rgba_bytes) or None on failure.
    For 4/8bpp with no embedded CLUT, falls back to grayscale.
    palette_index selects which CLUT row for multi-palette TIMs.
    """
    try:
        data = tim_path.read_bytes()
    except OSError:
        return None

    if len(data) < 12 or data[0] != 0x10 or data[1] != 0x00:
        return None

    flag = struct.unpack_from("<I", data, 4)[0]
    bpp_code = flag & 0x03
    has_clut = bool(flag & 0x08)
    bpp_map = {0: 4, 1: 8, 2: 16, 3: 24}
    bpp = bpp_map.get(bpp_code, 0)
    if bpp == 0:
        return None

    offset = 8
    clut: list[tuple[int, int, int, int]] = []
    entries_per_palette = 0

    if has_clut:
        if len(data) < offset + 12:
            return None
        clut_block_len = struct.unpack_from("<I", data, offset)[0]
        clut_w = struct.unpack_from("<H", data, offset + 8)[0]
        clut_h = struct.unpack_from("<H", data, offset + 10)[0]
        entries_per_palette = clut_w
        base = offset + 12
        for i in range(clut_w * clut_h):
            pos = base + i * 2
            if pos + 2 > len(data):
                break
            word = struct.unpack_from("<H", data, pos)[0]
            r = (word & 0x1F) << 3
            g = ((word >> 5) & 0x1F) << 3
            b = ((word >> 10) & 0x1F) << 3
            a = 0 if word == 0 else 255
            clut.append((r, g, b, a))
        offset += clut_block_len

    if len(data) < offset + 12:
        return None

    pix_w = struct.unpack_from("<H", data, offset + 8)[0]
    pix_h = struct.unpack_from("<H", data, offset + 10)[0]
    pix_start = offset + 12

    if bpp == 4:
        img_w = pix_w * 4
    elif bpp == 8:
        img_w = pix_w * 2
    elif bpp == 16:
        img_w = pix_w
    else:
        img_w = (pix_w * 2) // 3

    if img_w == 0 or pix_h == 0:
        return None

    pal_off = palette_index * entries_per_palette if entries_per_palette > 0 else 0
    row_bytes = pix_w * 2
    rgba = bytearray(img_w * pix_h * 4)

    if bpp in (4, 8):
        for y in range(pix_h):
            row_start = pix_start + y * row_bytes
            for x in range(img_w):
                if bpp == 4:
                    byte_pos = row_start + x // 2
                    if byte_pos >= len(data):
                        continue
                    nibble = (data[byte_pos] >> ((x & 1) * 4)) & 0x0F
                    idx = pal_off + nibble
                else:
                    byte_pos = row_start + x
                    if byte_pos >= len(data):
                        continue
                    idx = pal_off + data[byte_pos]
                if clut and idx < len(clut):
                    r, g, b, a = clut[idx]
                else:
                    raw = idx - pal_off
                    v = raw * 17 if bpp == 4 else raw
                    r, g, b, a = v, v, v, 255
                out = (y * img_w + x) * 4
                rgba[out] = r; rgba[out + 1] = g; rgba[out + 2] = b; rgba[out + 3] = a

    elif bpp == 16:
        for y in range(pix_h):
            for x in range(img_w):
                pos = pix_start + y * row_bytes + x * 2
                if pos + 2 > len(data):
                    continue
                word = struct.unpack_from("<H", data, pos)[0]
                r = (word & 0x1F) << 3
                g = ((word >> 5) & 0x1F) << 3
                b = ((word >> 10) & 0x1F) << 3
                a = 0 if word == 0 else 255
                out = (y * img_w + x) * 4
                rgba[out] = r; rgba[out + 1] = g; rgba[out + 2] = b; rgba[out + 3] = a

    else:  # 24bpp: 3 bytes/pixel packed into 16-bit word rows
        for y in range(pix_h):
            for x in range(img_w):
                pos = pix_start + y * row_bytes + x * 3
                if pos + 3 > len(data):
                    continue
                out = (y * img_w + x) * 4
                rgba[out] = data[pos]; rgba[out + 1] = data[pos + 1]
                rgba[out + 2] = data[pos + 2]; rgba[out + 3] = 255

    return img_w, pix_h, bytes(rgba)


def parse_tim(tim_path: Path) -> Optional[TimInfo]:
    """
    Parse a TIM file header per the Sony spec.
    Returns None if the file is not a valid TIM.
    """
    try:
        data = tim_path.read_bytes()
    except OSError:
        return None

    if len(data) < 12:
        return None

    # Magic: 0x10 at byte 0, version 0x00 at byte 1
    if data[0] != 0x10 or data[1] != 0x00:
        return None

    flag = struct.unpack_from("<I", data, 4)[0]
    bpp_code = flag & 0x03
    has_clut = bool(flag & 0x08)

    bpp_map = {0: 4, 1: 8, 2: 16, 3: 24}
    bpp = bpp_map.get(bpp_code, 0)
    if bpp == 0:
        return None

    offset = 8
    clut_x = clut_y = clut_w = clut_h = 0

    if has_clut:
        if len(data) < offset + 12:
            return None
        # clut_length = struct.unpack_from("<I", data, offset)[0]  # not needed
        clut_x = struct.unpack_from("<H", data, offset + 4)[0]
        clut_y = struct.unpack_from("<H", data, offset + 6)[0]
        clut_w = struct.unpack_from("<H", data, offset + 8)[0]
        clut_h = struct.unpack_from("<H", data, offset + 10)[0]
        clut_block_len = struct.unpack_from("<I", data, offset)[0]
        offset += clut_block_len

    if len(data) < offset + 12:
        return None

    # pixel_length = struct.unpack_from("<I", data, offset)[0]
    pix_x = struct.unpack_from("<H", data, offset + 4)[0]
    pix_y = struct.unpack_from("<H", data, offset + 6)[0]
    pix_w = struct.unpack_from("<H", data, offset + 8)[0]   # VRAM words wide
    pix_h = struct.unpack_from("<H", data, offset + 10)[0]

    # Convert stored VRAM width to actual pixel width
    if bpp == 4:
        img_w = pix_w * 4
    elif bpp == 8:
        img_w = pix_w * 2
    else:
        img_w = pix_w  # 16bpp and 24bpp: 1 word = 1 pixel (approx)

    return TimInfo(
        bpp=bpp,
        has_clut=has_clut,
        clut_x=clut_x,
        clut_y=clut_y,
        clut_width=clut_w,
        clut_height=clut_h,
        pixel_x=pix_x,
        pixel_y=pix_y,
        pixel_width=pix_w,
        pixel_height=pix_h,
        image_width=img_w,
        image_height=pix_h,
        file_size=len(data),
    )


# ---------------------------------------------------------------------------
# Audio info / extraction / validation
# ---------------------------------------------------------------------------

@dataclass
class AudioInfo:
    sample_rate: int        # Hz, e.g. 37800
    channels: int           # 1 = mono, 2 = stereo
    sample_width: int       # bytes per sample (1=8-bit, 2=16-bit, 3=24-bit, 4=32-bit)
    num_frames: int         # total PCM frames
    duration_secs: float
    file_size: int

    @property
    def bit_depth(self) -> int:
        return self.sample_width * 8

    @property
    def duration_str(self) -> str:
        total = int(self.duration_secs)
        m, s = divmod(total, 60)
        ms = int((self.duration_secs - total) * 100)
        return f"{m}:{s:02d}.{ms:02d}"


def parse_wav_info(wav_path: Path) -> Optional[AudioInfo]:
    import wave as _wave
    try:
        with _wave.open(str(wav_path), 'rb') as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
        duration = nframes / framerate if framerate > 0 else 0.0
        return AudioInfo(
            sample_rate=framerate,
            channels=channels,
            sample_width=sampwidth,
            num_frames=nframes,
            duration_secs=duration,
            file_size=wav_path.stat().st_size,
        )
    except Exception as e:
        log.debug("parse_wav_info %s: %s", wav_path, e)
        return None


def extract_all_audio(index_path: Path, out_dir: Path) -> tuple[bool, str]:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run([
        "-x", str(index_path), "-a", "audio", "-soundfmt", "wav", "-dir", str(out_dir)
    ])
    return rc == 0, err or out


def extract_audio_for_item(index_path: Path, item_number: int, out_dir: Path) -> tuple[bool, str]:
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run([
        "-x", str(index_path),
        "-i", str(item_number),
        "-soundfmt", "wav",
        "-dir", str(out_dir),
    ])
    return rc == 0, err or out


def validate_and_encode_audio(
    input_path: Path,
    ref: AudioInfo,
    out_path: Path,
) -> tuple[bool, str, Optional["AudioInfo"]]:
    """
    Validate and prepare a WAV replacement for a PS1 audio stream.

    Rules:
      - Only WAV accepted; anything else → error.
      - Sample rate mismatch → error (must match for XA injection).
      - Channel count mismatch → error.
      - Input longer than reference → error, import refused.
      - Input shorter → pad tail with silence to exactly match ref length.
      - Bit depth > 16 → clamped to 16-bit PCM (lossy).
      - Bit depth 8 → expanded to 16-bit signed.

    Returns (ok, message, output_AudioInfo).
    """
    import wave as _wave
    import array as _array

    if input_path.suffix.lower() != '.wav':
        return False, "Only WAV files are accepted for audio replacement.", None

    src = parse_wav_info(input_path)
    if src is None:
        return False, "Could not read WAV file — is it a valid PCM WAV?", None

    if src.sample_rate != ref.sample_rate:
        return (
            False,
            f"Sample rate mismatch: file is {src.sample_rate} Hz, "
            f"original is {ref.sample_rate} Hz. "
            f"Re-export your audio at exactly {ref.sample_rate} Hz.",
            None,
        )

    if src.channels != ref.channels:
        ch_hint = "Convert to mono." if ref.channels == 1 else "Convert to stereo."
        return (
            False,
            f"Channel count mismatch: file has {src.channels} channel(s), "
            f"original has {ref.channels}. {ch_hint}",
            None,
        )

    if src.num_frames > ref.num_frames:
        over_secs = (src.num_frames - ref.num_frames) / ref.sample_rate
        return (
            False,
            f"Audio is too long by {over_secs:.2f}s "
            f"({src.num_frames} frames vs {ref.num_frames} max). "
            f"Trim the file before importing.",
            None,
        )

    # Read raw PCM bytes
    with _wave.open(str(input_path), 'rb') as wf:
        raw = wf.readframes(src.num_frames)

    clamped_msg = ""
    out_sampwidth = src.sample_width

    if src.sample_width == 4:
        # 32-bit → 16-bit: shift right 16
        arr = _array.array('i')
        arr.frombytes(raw)
        out_arr = _array.array('h', (v >> 16 for v in arr))
        raw = out_arr.tobytes()
        out_sampwidth = 2
        clamped_msg = " (bit depth clamped 32→16)"
    elif src.sample_width == 3:
        # 24-bit → 16-bit: drop low byte
        n = len(raw) // 3
        out_arr = _array.array('h')
        for i in range(n):
            b0, b1, b2 = raw[i*3], raw[i*3+1], raw[i*3+2]
            val = b0 | (b1 << 8) | (b2 << 16)
            if val >= 0x800000:
                val -= 0x1000000
            out_arr.append(val >> 8)
        raw = out_arr.tobytes()
        out_sampwidth = 2
        clamped_msg = " (bit depth clamped 24→16)"
    elif src.sample_width == 1:
        # 8-bit unsigned → 16-bit signed
        out_arr = _array.array('h', ((b - 128) * 256 for b in raw))
        raw = out_arr.tobytes()
        out_sampwidth = 2
        clamped_msg = " (bit depth expanded 8→16)"

    # Pad with silence if shorter than reference
    frame_bytes = src.channels * out_sampwidth
    current_frames = len(raw) // frame_bytes
    padded_msg = ""
    if current_frames < ref.num_frames:
        pad_frames = ref.num_frames - current_frames
        raw = raw + bytes(frame_bytes * pad_frames)
        pad_secs = pad_frames / src.sample_rate
        padded_msg = f" (padded +{pad_secs:.2f}s silence)"

    # Ensure output is exactly ref.num_frames long
    raw = raw[:ref.num_frames * frame_bytes]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with _wave.open(str(out_path), 'wb') as wf:
        wf.setnchannels(src.channels)
        wf.setsampwidth(out_sampwidth)
        wf.setframerate(src.sample_rate)
        wf.writeframes(raw)

    result = AudioInfo(
        sample_rate=src.sample_rate,
        channels=src.channels,
        sample_width=out_sampwidth,
        num_frames=ref.num_frames,
        duration_secs=ref.num_frames / src.sample_rate,
        file_size=out_path.stat().st_size,
    )
    return True, f"ok{clamped_msg}{padded_msg}", result


# ---------------------------------------------------------------------------
# XA ADPCM encoder + BIN sector injection
# ---------------------------------------------------------------------------

_XA_FILTER_COEF = [(0, 0), (60, 0), (115, -52), (98, -55)]
_CD_SECTOR_BYTES = 2352
_XA_DATA_OFFSET  = 24       # bytes 24–2347 = 2324 bytes of data
_XA_DATA_SIZE    = 2324
_XA_AUDIO_BYTES  = 2304     # 18 sound groups × 128 bytes
_XA_GROUPS       = 18
_XA_SUS          = 8        # sound units per group
_XA_SU_SAMPLES   = 28       # PCM samples per sound unit


def _xa_range_for_residuals(residuals: list) -> int:
    """Smallest range r (0–12) s.t. 7 × 2^r ≥ max(|residuals|)."""
    max_abs = max(abs(e) for e in residuals) if residuals else 0
    r = 0
    while r < 12 and (7 << r) < max_abs:
        r += 1
    return r


def _encode_su(
    samples: list, fi: int, r: int, p1: int, p2: int
) -> tuple:
    """Encode 28 int16 PCM samples → (nibbles[28], new_p1, new_p2)."""
    k0, k1 = _XA_FILTER_COEF[fi]
    nibs = []
    for s in samples:
        pred = (k0 * p1 + k1 * p2 + 32) >> 6
        err  = s - pred
        n    = ((err + (1 << (r - 1))) >> r) if r > 0 else err
        n    = max(-8, min(7, n))
        decoded = max(-32768, min(32767, (n << r) + pred))
        nibs.append(n & 0xF)
        p2, p1 = p1, decoded
    return nibs, p1, p2


def _best_su_params(samples: list, p1: int, p2: int) -> tuple:
    """Return (filter_idx, range_val) minimising MSE over 28 samples."""
    best = (0, 0, float('inf'))
    for fi in range(4):
        k0, k1 = _XA_FILTER_COEF[fi]
        pp1, pp2 = p1, p2
        residuals = []
        for s in samples:
            pred = (k0 * pp1 + k1 * pp2 + 32) >> 6
            residuals.append(s - pred)
            pp2, pp1 = pp1, max(-32768, min(32767, s))
        r = _xa_range_for_residuals(residuals)
        # Measure actual MSE at this (fi, r)
        nibs, _, _ = _encode_su(samples, fi, r, p1, p2)
        dp1, dp2, mse = p1, p2, 0
        for s, nb in zip(samples, nibs):
            signed_n = nb if nb < 8 else nb - 16
            pred = (k0 * dp1 + k1 * dp2 + 32) >> 6
            dec  = max(-32768, min(32767, (signed_n << r) + pred))
            mse += (s - dec) ** 2
            dp2, dp1 = dp1, dec
        if mse < best[2]:
            best = (fi, r, mse)
    return best[0], best[1]


def _build_sound_group(
    left_chunk: list,
    right_chunk: list,
    su_state: list,
) -> bytes:
    """
    Build one 128-byte XA ADPCM sound group.
    left_chunk : 224 samples (mono) or 112 samples (stereo L)
    right_chunk: []            (mono) or 112 samples (stereo R)
    su_state   : list of 8 (p1, p2) tuples, updated in-place.
    """
    stereo = bool(right_chunk)
    if stereo:
        su_samples, li, ri = [], 0, 0
        for i in range(_XA_SUS):
            if i % 2 == 0:
                su_samples.append(left_chunk[li:li + _XA_SU_SAMPLES]); li += _XA_SU_SAMPLES
            else:
                su_samples.append(right_chunk[ri:ri + _XA_SU_SAMPLES]); ri += _XA_SU_SAMPLES
    else:
        su_samples = [
            left_chunk[i * _XA_SU_SAMPLES:(i + 1) * _XA_SU_SAMPLES]
            for i in range(_XA_SUS)
        ]

    params  = bytearray(8)
    all_nibs = []
    for si in range(_XA_SUS):
        chunk = list(su_samples[si])
        if len(chunk) < _XA_SU_SAMPLES:
            chunk += [0] * (_XA_SU_SAMPLES - len(chunk))
        p1, p2 = su_state[si]
        fi, r  = _best_su_params(chunk, p1, p2)
        nibs, new_p1, new_p2 = _encode_su(chunk, fi, r, p1, p2)
        all_nibs.append(nibs)
        su_state[si] = (new_p1, new_p2)
        params[si] = ((fi & 0x3) << 4) | (r & 0xF)

    group = bytearray(128)
    group[0:8]  = params
    group[8:16] = params   # mandatory duplicate
    # Interleave: byte 16+j*4+b has nibbles for SU b*2 (lo) and SU b*2+1 (hi)
    for j in range(_XA_SU_SAMPLES):
        base = 16 + j * 4
        for b in range(4):
            group[base + b] = (all_nibs[b * 2][j] & 0xF) | ((all_nibs[b * 2 + 1][j] & 0xF) << 4)
    return bytes(group)


_EDC_TABLE: list | None = None

def _get_edc_table() -> list:
    global _EDC_TABLE
    if _EDC_TABLE is not None:
        return _EDC_TABLE
    table = []
    for i in range(256):
        v = i
        for _ in range(8):
            v = (v >> 1) ^ 0x8001801B if (v & 1) else (v >> 1)
        table.append(v & 0xFFFFFFFF)
    _EDC_TABLE = table
    return table


def _xa_edc(data: bytes) -> int:
    """CD-ROM EDC (CRC-32 variant, poly 0x8001801B) over a byte block."""
    table = _get_edc_table()
    crc = 0
    for b in data:
        crc = (crc >> 8) ^ table[(crc ^ b) & 0xFF]
    return crc & 0xFFFFFFFF


def inject_audio_into_bin(
    bin_path: Path,
    sector_start: int,
    sector_end: int,
    wav_path: Path,
) -> tuple:
    """
    Encode a 16-bit PCM WAV to XA ADPCM and inject into BIN sectors
    [sector_start..sector_end].

    The WAV must already be normalised (16-bit, correct sample rate + channels,
    correct frame count) — run validate_and_encode_audio first.

    Only sectors whose submode byte has bit 2 (audio) set are written.
    EDC is recalculated for each modified sector.
    Returns (ok, message).
    """
    import wave   as _wv
    import array  as _arr

    if not wav_path.exists():
        return False, f"WAV not found: {wav_path}"

    try:
        with _wv.open(str(wav_path), 'rb') as wf:
            n_ch     = wf.getnchannels()
            sw       = wf.getsampwidth()
            n_frames = wf.getnframes()
            raw      = wf.readframes(n_frames)
    except Exception as e:
        return False, f"Could not read WAV: {e}"

    if sw != 2:
        return False, f"WAV must be 16-bit PCM (got {sw * 8}-bit)"

    all_samples = list(_arr.array('h', raw))
    if n_ch == 1:
        left_samples, right_samples = all_samples, []
        left_per_group = _XA_SUS * _XA_SU_SAMPLES          # 224 mono samples
    else:
        left_samples  = all_samples[0::2]
        right_samples = all_samples[1::2]
        left_per_group = 4 * _XA_SU_SAMPLES                 # 112 per-channel samples

    su_state: list = [(0, 0)] * _XA_SUS
    left_pos = right_pos = 0
    injected_sectors = 0
    cur_sec = sector_start

    try:
        with open(str(bin_path), 'r+b') as f:
            for cur_sec in range(sector_start, sector_end + 1):
                offset = cur_sec * _CD_SECTOR_BYTES
                f.seek(offset)
                sector = bytearray(f.read(_CD_SECTOR_BYTES))
                if len(sector) < _CD_SECTOR_BYTES:
                    break

                submode = sector[18]
                if not (submode & 0x04):    # bit 2 = audio sector
                    continue

                # Pull samples for this sector
                lcount = _XA_GROUPS * left_per_group
                chunk_left  = left_samples[left_pos:left_pos + lcount]
                chunk_right = (
                    right_samples[right_pos:right_pos + lcount]
                    if n_ch > 1 else []
                )
                left_pos  += lcount
                right_pos += lcount

                # Encode 18 sound groups
                audio_data = bytearray()
                sg_lp = sg_rp = 0
                for _ in range(_XA_GROUPS):
                    g_l = list(chunk_left[sg_lp:sg_lp + left_per_group])
                    g_r = list(chunk_right[sg_rp:sg_rp + left_per_group]) if n_ch > 1 else []
                    sg_lp += left_per_group
                    sg_rp += left_per_group
                    audio_data += _build_sound_group(g_l, g_r, su_state)

                # Overwrite audio payload + zero-pad trailing 20 bytes
                sector[_XA_DATA_OFFSET:_XA_DATA_OFFSET + _XA_AUDIO_BYTES] = audio_data
                sector[_XA_DATA_OFFSET + _XA_AUDIO_BYTES:_XA_DATA_OFFSET + _XA_DATA_SIZE] = bytes(20)

                # Recalculate EDC (covers bytes 16..2347)
                struct.pack_into('<I', sector, 2348, _xa_edc(bytes(sector[16:2348])))

                f.seek(offset)
                f.write(bytes(sector))
                injected_sectors += 1

    except Exception as e:
        return False, f"Injection failed at sector {cur_sec}: {e}"

    return True, f"Injected {injected_sectors} audio sectors"


# ---------------------------------------------------------------------------
# TIM encoder — convert PNG/GIF/TIM → TIM matching a reference TimInfo
# ---------------------------------------------------------------------------

def _quantize_with_alpha(
    img_rgba,
    n_colors: int,
    alpha_threshold: int = 128,
) -> tuple:
    """
    Quantize an RGBA PIL Image to n_colors palette entries.

    When any pixel has alpha < alpha_threshold, index 0 is reserved for
    transparent pixels and the remaining n_colors-1 slots hold opaque colors.
    This mirrors the PS1 convention where CLUT[0] = 0x0000 = transparent.

    Returns (pixels, palette_rgb, has_transparency):
      pixels        : list[int] of palette indices, length = w*h
      palette_rgb   : list[(r,g,b)] of length n_colors
      has_transparency: bool
    """
    rgba_data = list(img_rgba.getdata())
    has_transparency = any(px[3] < alpha_threshold for px in rgba_data)

    max_opaque = n_colors - 1 if has_transparency else n_colors

    rgb = img_rgba.convert("RGB")
    q = rgb.quantize(colors=max_opaque)
    raw_pal = q.getpalette() or []
    q_pixels = list(q.getdata())

    if has_transparency:
        pixels = [
            0 if px[3] < alpha_threshold else q_pixels[i] + 1
            for i, px in enumerate(rgba_data)
        ]
        palette = [(0, 0, 0)]
        for i in range(max_opaque):
            if i * 3 + 2 < len(raw_pal):
                palette.append((raw_pal[i * 3], raw_pal[i * 3 + 1], raw_pal[i * 3 + 2]))
            else:
                palette.append((0, 0, 0))
    else:
        pixels = q_pixels
        palette = []
        for i in range(n_colors):
            if i * 3 + 2 < len(raw_pal):
                palette.append((raw_pal[i * 3], raw_pal[i * 3 + 1], raw_pal[i * 3 + 2]))
            else:
                palette.append((0, 0, 0))

    return pixels, palette, has_transparency


def encode_to_tim(input_path: Path, ref: TimInfo, out_path: Path) -> tuple[bool, str]:
    """Convert a PNG/GIF/TIM to a TIM file matching ref's structure.

    All positional/structural fields are taken from ref:
      pixel_x, pixel_y, pixel_width, pixel_height, bpp, has_clut,
      clut_x, clut_y, clut_width, clut_height.
    Only CLUT color data and pixel data come from input_path.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".tim":
        return _patch_tim_coords(input_path, ref, out_path)

    try:
        from PIL import Image as PILImage
    except ImportError:
        return False, "Pillow not installed — run: pip install Pillow"

    try:
        img = PILImage.open(str(input_path)).convert("RGBA")
    except Exception as e:
        return False, f"Could not open image: {e}"

    if img.size != (ref.image_width, ref.image_height):
        img = img.resize((ref.image_width, ref.image_height), PILImage.LANCZOS)

    buf = bytearray(b'\x10\x00\x00\x00')

    if ref.bpp == 4:
        bpp_code, n_colors = 0, 16
    elif ref.bpp == 8:
        bpp_code, n_colors = 1, 256
    elif ref.bpp == 16:
        bpp_code, n_colors = 2, 0
    else:
        bpp_code, n_colors = 3, 0

    buf += struct.pack("<I", bpp_code | (0x08 if ref.has_clut else 0))

    if n_colors:
        pixels, palette, has_transparency = _quantize_with_alpha(img, n_colors)

        if ref.has_clut:
            total = ref.clut_width * ref.clut_height
            words = []
            for i in range(total):
                if i < len(palette):
                    r8, g8, b8 = palette[i]
                    if i == 0 and has_transparency:
                        w = 0x0000  # transparent sentinel
                    else:
                        r5 = r8 >> 3
                        g5 = g8 >> 3
                        b5 = b8 >> 3
                        w = r5 | (g5 << 5) | (b5 << 10)
                        if w:
                            w |= 0x8000  # STP bit prevents accidental black→transparent
                else:
                    w = 0
                words.append(w)
            clut_data = struct.pack(f"<{len(words)}H", *words)
            buf += struct.pack("<I", 12 + len(clut_data))
            buf += struct.pack("<HH", ref.clut_x, ref.clut_y)
            buf += struct.pack("<HH", ref.clut_width, ref.clut_height)
            buf += clut_data

        row_bytes = ref.pixel_width * 2
        pix_data = bytearray(row_bytes * ref.image_height)
        for y in range(ref.image_height):
            for x in range(ref.image_width):
                idx = pixels[y * ref.image_width + x] & 0xFF
                if ref.bpp == 4:
                    bp = y * row_bytes + x // 2
                    pix_data[bp] |= (idx & 0x0F) << ((x & 1) * 4)
                else:
                    pix_data[y * row_bytes + x] = idx

    elif ref.bpp == 16:
        pix_list = list(img.getdata())
        row_bytes = ref.pixel_width * 2
        pix_data = bytearray(row_bytes * ref.image_height)
        for y in range(ref.image_height):
            for x in range(ref.image_width):
                rr, gg, bb, aa = pix_list[y * ref.image_width + x]
                if aa < 128:
                    word = 0
                else:
                    word = (rr >> 3) | ((gg >> 3) << 5) | ((bb >> 3) << 10) | 0x8000
                bp = y * row_bytes + x * 2
                pix_data[bp] = word & 0xFF
                pix_data[bp + 1] = (word >> 8) & 0xFF

    else:  # 24bpp
        pix_list = list(img.getdata())
        row_bytes = ref.pixel_width * 2
        pix_data = bytearray(row_bytes * ref.image_height)
        for y in range(ref.image_height):
            for x in range(ref.image_width):
                bp = y * row_bytes + x * 3
                if bp + 2 >= len(pix_data):
                    break
                rr, gg, bb, _ = pix_list[y * ref.image_width + x]
                pix_data[bp] = rr; pix_data[bp + 1] = gg; pix_data[bp + 2] = bb

    buf += struct.pack("<I", 12 + len(pix_data))
    buf += struct.pack("<HH", ref.pixel_x, ref.pixel_y)
    buf += struct.pack("<HH", ref.pixel_width, ref.image_height)
    buf += bytes(pix_data)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(buf))
    return True, str(out_path)


def _patch_tim_coords(input_path: Path, ref: TimInfo, out_path: Path) -> tuple[bool, str]:
    """Copy a TIM, patching all positional fields from ref."""
    try:
        data = bytearray(input_path.read_bytes())
    except OSError as e:
        return False, str(e)
    if len(data) < 12 or data[0] != 0x10:
        return False, "Not a valid TIM file"

    flag = struct.unpack_from("<I", data, 4)[0]
    has_clut = bool(flag & 0x08)
    offset = 8

    if has_clut and len(data) >= offset + 12:
        clut_block_len = struct.unpack_from("<I", data, offset)[0]
        struct.pack_into("<HH", data, offset + 4, ref.clut_x, ref.clut_y)
        offset += clut_block_len

    if len(data) >= offset + 12:
        struct.pack_into("<HH", data, offset + 4, ref.pixel_x, ref.pixel_y)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(data))
    return True, str(out_path)
