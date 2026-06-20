"""
core/xa.py — CD-ROM XA-ADPCM audio decoder for AC1mod (C hot loop).

Decodes an XA audio stream straight out of the disc image, with no jPSXdec /JVM
round-trip — so previewing audio is a few-ms in-process decode instead of a
~2.8s subprocess per item (and it scales to bigger titles with more streams).

numpy does the vectorised part: read the raw 2352-byte sectors, pick this
stream's audio sectors by subheader channel + submode, and concatenate their
2304-byte ADPCM regions. The serial ADPCM IIR runs in C (core/xa.c, auto-built
to libxa.so and called via ctypes); a pure-Python fallback covers the
no-compiler case, and callers can fall back to jPSXdec if this raises.

XA sector (Mode-2): subheader at +16 (file,channel,submode,coding), then the
2304-byte audio data area at +24 = 18 sound groups of 128 bytes.
"""
from __future__ import annotations
import os
import wave
import ctypes
import subprocess
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "xa.c")
_SO = os.path.join(_HERE, "libxa.so")

RAW = 2352           # raw CD sector
SUBHEAD = 16         # subheader offset (file, channel, submode, coding, then copy)
DATA_OFF = 24        # user-data / ADPCM start
ADPCM_LEN = 2304     # 18 sound groups * 128 bytes
SUBMODE_AUDIO = 0x04
SUBMODE_FORM2 = 0x20


# ----------------------------------------------------------- C lib loader -----

def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _build_lib():
    cc = os.environ.get("CC") or _which("cc") or _which("gcc") or _which("clang")
    if not cc:
        return None
    tail = ["-O3", "-fPIC", "-shared", _SRC, "-o", _SO, "-lm"]
    for extra in (["-march=native"], []):
        try:
            subprocess.run([cc] + extra + tail, check=True,
                           capture_output=True, timeout=60)
            return _SO
        except Exception:
            continue
    return None


_LIB = None
_LIB_TRIED = False
_u8 = np.ctypeslib.ndpointer(dtype=np.uint8, flags="C_CONTIGUOUS")
_i16 = np.ctypeslib.ndpointer(dtype=np.int16, flags="C_CONTIGUOUS")


def _lib():
    """Lazily build+load libxa.so. None if unavailable (use python fallback)."""
    global _LIB, _LIB_TRIED
    if _LIB is not None or _LIB_TRIED:
        return _LIB
    _LIB_TRIED = True
    so = _SO
    if not (os.path.exists(so) and os.path.getmtime(so) >= os.path.getmtime(_SRC)):
        so = _build_lib()
    if not so:
        return None
    try:
        lib = ctypes.CDLL(so)
        lib.xa_decode.restype = None
        lib.xa_decode.argtypes = [_u8, ctypes.c_int, ctypes.c_int, _i16]
        _LIB = lib
    except Exception:
        _LIB = None
    return _LIB


# --------------------------------------------------------- sector unpack ------

def _read_groups(bin_path, sector_start, sector_end, channel):
    """numpy: read the sector range, keep this channel's audio sectors, return
    their concatenated 128-byte sound groups as (n_groups, 128) uint8."""
    with open(bin_path, "rb") as f:
        f.seek(sector_start * RAW)
        raw = f.read((sector_end - sector_start + 1) * RAW)
    nsec = len(raw) // RAW
    if nsec == 0:
        return np.empty((0, 128), np.uint8)
    sec = np.frombuffer(raw, np.uint8, count=nsec * RAW).reshape(nsec, RAW)
    chan = sec[:, SUBHEAD + 1]
    submode = sec[:, SUBHEAD + 2]
    keep = (chan == channel) & (submode & SUBMODE_AUDIO).astype(bool) \
        & (submode & SUBMODE_FORM2).astype(bool)
    audio = sec[keep, DATA_OFF:DATA_OFF + ADPCM_LEN]
    if audio.size == 0:
        return np.empty((0, 128), np.uint8)
    return np.ascontiguousarray(audio.reshape(-1, 128))


# --------------------------------------------------------------- decode -------

def decode_xa(bin_path, sector_start, sector_end, channel=0, stereo=True):
    """Decode an XA stream to interleaved int16 PCM.
    Returns (channels, samples_int16) or (channels, None) if no audio found."""
    groups = _read_groups(bin_path, sector_start, sector_end, channel)
    n = groups.shape[0]
    channels = 2 if stereo else 1
    if n == 0:
        return channels, None
    frames_per_group = 112 if stereo else 224
    out = np.empty(n * frames_per_group * (2 if stereo else 1), np.int16)
    lib = _lib()
    if lib is not None:
        lib.xa_decode(groups.reshape(-1), n, channels, out)
    else:
        _decode_py(groups, channels, out)
    return channels, out


_XA_TAB = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))


def _decode_py(groups, channels, out):
    """Pure-Python reference (no compiler). Matches the C path / jPSXdec: float
    predictor, output = round(clip(sp)), feedback = UN-clamped sp. Correct but
    slow; callers can fall back to jPSXdec for large streams when the C lib is
    unavailable."""
    def sx4(v):
        v &= 0xF
        return v - 16 if v >= 8 else v

    def clip(s):
        return -32768.0 if s < -32768.0 else (32767.0 if s > 32767.0 else s)

    s1l = s2l = s1r = s2r = 0.0
    o = out
    for g in range(groups.shape[0]):
        hdr = groups[g, :16]
        dat = groups[g, 16:]
        base = g * (112 * 2 if channels == 2 else 224)
        for i in range(4):
            p0 = int(hdr[4 + i * 2]); sh0 = 12 - (p0 & 0xF)
            f0a, f1a = _XA_TAB[min(p0 >> 4, 4)]
            p1 = int(hdr[5 + i * 2]); sh1 = 12 - (p1 & 0xF)
            f0b, f1b = _XA_TAB[min(p1 >> 4, 4)]
            if channels == 2:
                s1, s2 = s1l, s2l
                for j in range(28):
                    t = sx4(int(dat[i + j * 4]))
                    sp = (t << sh0) + (s1 * f0a + s2 * f1a) / 64.0
                    s2, s1 = s1, sp
                    o[base + (i * 28 + j) * 2] = int(round(clip(sp)))
                s1l, s2l = s1, s2
                s1, s2 = s1r, s2r
                for j in range(28):
                    t = sx4(int(dat[i + j * 4]) >> 4)
                    sp = (t << sh1) + (s1 * f0b + s2 * f1b) / 64.0
                    s2, s1 = s1, sp
                    o[base + (i * 28 + j) * 2 + 1] = int(round(clip(sp)))
                s1r, s2r = s1, s2
            else:
                s1, s2 = s1l, s2l
                for j in range(28):
                    t = sx4(int(dat[i + j * 4]))
                    sp = (t << sh0) + (s1 * f0a + s2 * f1a) / 64.0
                    s2, s1 = s1, sp
                    o[base + i * 56 + j] = int(round(clip(sp)))
                for j in range(28):
                    t = sx4(int(dat[i + j * 4]) >> 4)
                    sp = (t << sh1) + (s1 * f0b + s2 * f1b) / 64.0
                    s2, s1 = s1, sp
                    o[base + i * 56 + 28 + j] = int(round(clip(sp)))
                s1l, s2l = s1, s2


def decode_xa_to_wav(bin_path, sector_start, sector_end, out_path,
                     channel=0, stereo=True, sample_rate=37800):
    """Decode an XA stream and write a 16-bit PCM WAV. Returns out_path or None."""
    channels, pcm = decode_xa(bin_path, sector_start, sector_end, channel, stereo)
    if pcm is None:
        return None
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return out_path
