"""
core/render.py — single-mesh viewport for AC1mod.

Solid rendering goes through the vectorized z-buffer rasterizer (core/raster.py) so
a single Mesh draws with the SAME correct per-pixel occlusion as the mission scenes —
no painter's-algorithm depth spikes, no walls poking through floors. Wireframe mode
keeps the lightweight QPainter line draw (a z-buffer adds nothing to wires).

PS1 geometry is two-sided by default here: the game culls back-faces per-camera with
NCLIP, but a static viewer can't reproduce that per winding, and culling on our side
makes inconsistently-wound walls turn see-through. The z-buffer makes two-sided
correct, so we render both sides and let depth sort it out.
"""
from __future__ import annotations
import math
import numpy as np
from PyQt6.QtGui import QImage, QPainter, QColor, QPolygonF, QPen
from PyQt6.QtCore import Qt, QPointF


def _mesh_arrays(mesh):
    """(V, VN, F, Fcol) numpy arrays from a core.pa_parser.Mesh."""
    from core.scene import smooth_normals
    V = np.array(mesh.vertices, np.float32) if mesh.vertices else np.zeros((0, 3), np.float32)
    F = np.array([f.verts for f in mesh.faces], np.int32) if mesh.faces else np.zeros((0, 3), np.int32)
    Fcol = np.array([f.color for f in mesh.faces], np.uint8) if mesh.faces else np.zeros((0, 3), np.uint8)
    VN = smooth_normals(V, F) if len(V) else V
    return V, VN, F, Fcol


def render_mesh(mesh, w=520, h=380, yaw=0.6, pitch=0.5, zoom=1.0,
                bg=(24, 26, 32), wire=False, cull=False):
    """Render `mesh` to a QImage(w,h). yaw/pitch in radians, zoom multiplier.

    Solid mode → z-buffered rasterizer (correct occlusion). wire=True → QPainter
    wireframe. cull=False renders two-sided (default; see module docstring)."""
    w, h = max(int(w), 8), max(int(h), 8)
    if not wire:
        from core import raster
        V, VN, F, Fcol = _mesh_arrays(mesh)
        if len(F) == 0:
            return _blank(w, h, bg, mesh)
        Fid = np.zeros(len(F), np.int32)
        img, _ = raster.render(V, VN, F, Fcol, Fid, w, h, yaw, pitch, zoom,
                               bg=bg, cull=cull)
        return img
    return _wire(mesh, w, h, yaw, pitch, zoom, bg)


def _blank(w, h, bg, mesh):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*bg))
    p = QPainter(img)
    p.setPen(QColor(150, 160, 175))
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "no geometry" if mesh else "—")
    p.end()
    return img


def _rot(yaw, pitch):
    cy, sy = math.cos(yaw), math.sin(yaw)
    cx, sx = math.cos(pitch), math.sin(pitch)
    def f(p):
        x, y, z = p
        x, z = x * cy + z * sy, -x * sy + z * cy
        y, z = y * cx - z * sx, y * sx + z * cx
        return (x, y, z)
    return f


def _wire(mesh, w, h, yaw, pitch, zoom, bg):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*bg))
    bb = mesh.bbox() if mesh else None
    if not bb or not mesh.faces:
        return _blank(w, h, bg, mesh)
    cx, cy, cz = (bb[0] + bb[3]) / 2, (bb[1] + bb[4]) / 2, (bb[2] + bb[5]) / 2
    extent = max(bb[3] - bb[0], bb[4] - bb[1], bb[5] - bb[2], 1.0)
    rot = _rot(yaw, pitch)
    scale = (min(w, h) * 0.42 / extent) * zoom

    def project(v):
        # No Y flip: AC1 world Y and screen Y both point down (see core/raster.py).
        x, y, z = rot((v[0] - cx, v[1] - cy, v[2] - cz))
        return (w / 2 + x * scale, h / 2 + y * scale, z)

    pv = [project(v) for v in mesh.vertices]
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    for fc in mesh.faces:
        try:
            tri = [pv[i] for i in fc.verts]
        except IndexError:
            continue
        col = QColor(*fc.color)
        p.setPen(QPen(col, 1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([QPointF(t[0], t[1]) for t in tri]))
    p.end()
    return img
