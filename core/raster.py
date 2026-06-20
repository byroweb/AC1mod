"""
core/raster.py — z-buffered triangle rasterizer for AC1mod (C hot loop).

A true per-pixel depth buffer (no painter's-algorithm z-fighting / wrong overlaps —
walls no longer poke through floors or vanish behind far geometry), smooth Gouraud
lighting from per-vertex normals, and a parallel object-ID buffer so the GUI can pick
the object under the cursor. Works headless (returns a QImage + id array).

The expensive per-pixel scan-conversion runs in C (core/raster.c, auto-compiled to
libraster.so and called via ctypes) — a full mission level (≈37k faces) renders in a
few milliseconds. Python only does the O(Nv) vertex transform + lighting in numpy.
If no C compiler is available we fall back to a vectorized-numpy rasterizer, so the C
file is a speed upgrade, not a hard dependency.

Inputs are flat arrays (build once per scene, re-render cheaply on orbit):
  V    (Nv,3) float  vertex positions (model space)
  VN   (Nv,3) float  per-vertex normals (smooth)
  F    (Nf,3) int    triangle vertex indices
  Fcol (Nf,3) uint8  per-triangle base RGB
  Fid  (Nf,)  int    object id per triangle (for picking; -1 = none)
"""
from __future__ import annotations
import os
import ctypes
import subprocess
import numpy as np
from PyQt6.QtGui import QImage

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "raster.c")
_SO = os.path.join(_HERE, "libraster.so")
_PIXEL_BUDGET = 2_000_000          # numpy-fallback batch size


# ----------------------------------------------------------- C lib loader -----

def _build_lib():
    """Compile raster.c -> libraster.so. Returns the .so path or None."""
    cc = os.environ.get("CC") or _which("cc") or _which("gcc") or _which("clang")
    if not cc:
        return None
    base = [cc, "-O3", "-ffast-math", "-fPIC", "-shared", _SRC, "-o", _SO, "-lm"]
    for extra in (["-march=native"], []):           # native first, then portable
        try:
            subprocess.run(base[:1] + extra + base[1:], check=True,
                           capture_output=True, timeout=60)
            return _SO
        except Exception:
            continue
    return None


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_LIB = None
_LIB_TRIED = False

_f32 = np.ctypeslib.ndpointer(dtype=np.float32, flags="C_CONTIGUOUS")
_i32 = np.ctypeslib.ndpointer(dtype=np.int32, flags="C_CONTIGUOUS")
_u8 = np.ctypeslib.ndpointer(dtype=np.uint8, flags="C_CONTIGUOUS")


def _lib():
    """Lazily build+load libraster.so. None if unavailable (use numpy fallback)."""
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
        lib.rasterize.restype = None
        lib.rasterize.argtypes = [
            ctypes.c_int, ctypes.c_int,                 # w, h
            _f32, _f32, _f32,                           # sx, sy, depth
            _f32,                                       # inten
            _i32, _u8, _i32,                            # F, Fcol, Fid
            ctypes.c_int, ctypes.c_int,                 # Nf, cull
            _u8, _i32, _f32,                            # img, idbuf, zbuf
        ]
        _LIB = lib
    except Exception:
        _LIB = None
    return _LIB


# --------------------------------------------------------------- render -------

def look_matrix(yaw, pitch):
    cy, sy = np.cos(yaw), np.sin(yaw)
    cx, sx = np.cos(pitch), np.sin(pitch)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    return Rx @ Ry


def render(V, VN, F, Fcol, Fid, w=800, h=600, yaw=0.6, pitch=0.4, zoom=1.0,
           bg=(24, 26, 32), light_dir=(0.4, 0.7, 0.6), ambient=0.35,
           cull=True, center=None, extent=None):
    """Rasterize and return (QImage, id_buffer[h,w] int32).

    cull=True keeps one winding (back-face cull, matching the game's NCLIP); set
    False to render two-sided (e.g. meshes with inconsistent winding).
    center/extent override the auto fit so the framing can be held stable.
    """
    w, h = int(w), int(h)
    if len(V) == 0 or len(F) == 0:
        img = np.empty((h, w, 3), np.float32); img[:] = np.array(bg, np.float32)
        return _to_qimage(img), np.full((h, w), -1, np.int32)

    R = look_matrix(yaw, pitch).astype(np.float32)
    c = np.asarray(center, np.float32) if center is not None else (V.min(0) + V.max(0)) * 0.5
    ext = float(extent) if extent is not None else (float(np.max(V.max(0) - V.min(0))) or 1.0)
    # AC1 world Y already points DOWN and screen Y points down too, so they agree —
    # no Y flip. (The old `[1,-1,1]` flip rendered everything UPSIDE DOWN: it put
    # level floors above ceilings and AC models on their heads, and it flipped
    # positions without flipping the lighting normals. Removed 2026-06-18.)
    P = (V - c)
    cam = P @ R.T
    scale = (min(w, h) * 0.42 / ext) * zoom
    sx = np.ascontiguousarray(w * 0.5 + cam[:, 0] * scale, np.float32)
    sy = np.ascontiguousarray(h * 0.5 + cam[:, 1] * scale, np.float32)
    depth = np.ascontiguousarray(cam[:, 2], np.float32)

    # per-vertex Gouraud intensity
    L = np.array(light_dir, np.float32); L /= (np.linalg.norm(L) or 1)
    nrm = VN @ R.T
    nl = np.linalg.norm(nrm, axis=1, keepdims=True); nl[nl == 0] = 1
    inten = np.ascontiguousarray(
        ambient + (1 - ambient) * np.clip(np.abs((nrm / nl) @ L), 0, 1), np.float32)

    Fc = np.ascontiguousarray(F, np.int32)
    Fcolc = np.ascontiguousarray(Fcol, np.uint8)
    Fidc = np.ascontiguousarray(Fid, np.int32)

    lib = _lib()
    if lib is not None:
        img = np.empty((h, w, 3), np.uint8)
        img[:] = np.array(bg, np.uint8)
        idbuf = np.full(h * w, -1, np.int32)
        zbuf = np.full(h * w, np.inf, np.float32)
        lib.rasterize(w, h, sx, sy, depth, inten, Fc.reshape(-1), Fcolc.reshape(-1),
                      Fidc, len(Fc), 1 if cull else 0,
                      img.reshape(-1), idbuf, zbuf)
        return _u8_to_qimage(img), idbuf.reshape(h, w)

    return _render_numpy(sx, sy, depth, inten, Fc, Fcolc, Fidc, w, h, bg, cull)


# ------------------------------------------------- numpy fallback (vectorized)

def _render_numpy(sx, sy, depth, inten, F, Fcol, Fid, w, h, bg, cull):
    """Pure-numpy z-buffer rasterizer (used when no C compiler is present).
    Triangles are batched by a pixel budget so peak memory stays bounded."""
    img = np.empty((h * w, 3), np.float32); img[:] = np.array(bg, np.float32)
    zbuf = np.full(h * w, np.inf, np.float32)
    idbuf = np.full(h * w, -1, np.int32)

    i0, i1, i2 = F[:, 0], F[:, 1], F[:, 2]
    x0, y0 = sx[i0], sy[i0]; x1, y1 = sx[i1], sy[i1]; x2, y2 = sx[i2], sy[i2]
    area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
    rbx0 = np.minimum(np.minimum(x0, x1), x2); rbx1 = np.maximum(np.maximum(x0, x1), x2)
    rby0 = np.minimum(np.minimum(y0, y1), y2); rby1 = np.maximum(np.maximum(y0, y1), y2)
    vis = area != 0
    if cull:
        vis &= area < 0
    vis &= (rbx1 >= 0) & (rbx0 <= w - 1) & (rby1 >= 0) & (rby0 <= h - 1)
    bx0 = np.clip(np.floor(rbx0), 0, w - 1).astype(np.int64)
    bx1 = np.clip(np.ceil(rbx1), 0, w - 1).astype(np.int64)
    by0 = np.clip(np.floor(rby0), 0, h - 1).astype(np.int64)
    by1 = np.clip(np.ceil(rby1), 0, h - 1).astype(np.int64)

    tris = np.nonzero(vis)[0]
    if len(tris) == 0:
        return _to_qimage(img.reshape(h, w, 3)), idbuf.reshape(h, w)
    bw = bx1[tris] - bx0[tris] + 1; bh = by1[tris] - by0[tris] + 1
    bpx = bw * bh
    csum = np.cumsum(bpx)
    nbatch = int(csum[-1] // _PIXEL_BUDGET) + 1
    edges = np.searchsorted(csum, np.arange(1, nbatch) * _PIXEL_BUDGET)
    starts = np.concatenate(([0], edges)); ends = np.concatenate((edges, [len(tris)]))

    for s, e in zip(starts, ends):
        if e <= s:
            continue
        T = tris[s:e]; cnt = bpx[s:e]; total = int(cnt.sum())
        if total == 0:
            continue
        rep = np.repeat(np.arange(len(T)), cnt)
        off = np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        wrep = np.repeat(bw[s:e], cnt)
        px = np.repeat(bx0[T], cnt) + off % wrep
        py = np.repeat(by0[T], cnt) + off // wrep
        a = area[T][rep]
        x0r, y0r = x0[T][rep], y0[T][rep]
        x1r, y1r = x1[T][rep], y1[T][rep]
        x2r, y2r = x2[T][rep], y2[T][rep]
        fx = px.astype(np.float32) + 0.5; fy = py.astype(np.float32) + 0.5
        w0 = ((x1r - x0r) * (fy - y0r) - (y1r - y0r) * (fx - x0r)) / a
        w1 = ((x2r - x1r) * (fy - y1r) - (y2r - y1r) * (fx - x1r)) / a
        w2 = 1.0 - w0 - w1
        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue
        rep = rep[inside]; px = px[inside]; py = py[inside]
        w0 = w0[inside]; w1 = w1[inside]; w2 = w2[inside]
        z = w0 * depth[i2[T]][rep] + w1 * depth[i0[T]][rep] + w2 * depth[i1[T]][rep]
        shade = w0 * inten[i2[T]][rep] + w1 * inten[i0[T]][rep] + w2 * inten[i1[T]][rep]
        flat = py * w + px
        order = np.lexsort((z, flat))
        fs = flat[order]
        first = np.empty(len(order), bool); first[0] = True; first[1:] = fs[1:] != fs[:-1]
        win = order[first]
        fw = flat[win]; zw = z[win]
        better = zw < zbuf[fw]
        if not better.any():
            continue
        sel = fw[better]
        zbuf[sel] = zw[better]
        gtri = T[rep[win][better]]
        img[sel] = np.clip(Fcol[gtri].astype(np.float32) * shade[win][better][:, None], 0, 255)
        idbuf[sel] = Fid[gtri]

    return _to_qimage(img.reshape(h, w, 3)), idbuf.reshape(h, w)


def _to_qimage(arr):
    a = np.ascontiguousarray(np.clip(arr, 0, 255).astype(np.uint8))
    hh, ww, _ = a.shape
    return QImage(a.data, ww, hh, 3 * ww, QImage.Format.Format_RGB888).copy()


def _u8_to_qimage(a):
    a = np.ascontiguousarray(a)
    hh, ww, _ = a.shape
    return QImage(a.data, ww, hh, 3 * ww, QImage.Format.Format_RGB888).copy()
