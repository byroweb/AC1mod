"""
core/scene.py — assemble a renderable Scene (stage + placed objects) for AC1mod.

A Scene is a list of SceneObject (a Mesh + a world transform + metadata for the
info dropdown). It flattens to the numpy arrays core/raster.py consumes, computing
smooth per-vertex normals and tagging every triangle with its object id for picking.
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
import math


def smooth_normals(V, F):
    """Per-vertex normals by area-weighted face-normal accumulation."""
    VN = np.zeros_like(V)
    if len(F):
        a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        fn = np.cross(b - a, c - a)
        for k in range(3):
            np.add.at(VN, F[:, k], fn)
    n = np.linalg.norm(VN, axis=1, keepdims=True)
    n[n == 0] = 1
    return (VN / n).astype(np.float32)


def _flat_arrays(V, F, Fcol, Fid):
    """Explode to one vertex triple per face, each carrying the face normal — flat
    (faceted) shading with no cross-face interpolation. Fcol/Fid stay per-face."""
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    fn = np.cross(b - a, c - a)
    nl = np.linalg.norm(fn, axis=1, keepdims=True); nl[nl == 0] = 1.0
    fn = (fn / nl).astype(np.float32)
    n = len(F)
    V2 = np.ascontiguousarray(np.concatenate([a, b, c]), np.float32)
    VN2 = np.ascontiguousarray(np.concatenate([fn, fn, fn]), np.float32)
    F2 = np.ascontiguousarray(
        np.stack([np.arange(n), np.arange(n, 2 * n), np.arange(2 * n, 3 * n)], 1), np.int32)
    return V2, VN2, F2, np.ascontiguousarray(Fcol, np.uint8), np.ascontiguousarray(Fid, np.int32)


def mesh_arrays(mesh):
    """(V, F, Fcol) from a core.pa_parser.Mesh."""
    V = np.array(mesh.vertices, np.float32) if mesh.vertices else np.zeros((0, 3), np.float32)
    F = np.array([f.verts for f in mesh.faces], np.int32) if mesh.faces else np.zeros((0, 3), np.int32)
    Fcol = np.array([f.color for f in mesh.faces], np.uint8) if mesh.faces else np.zeros((0, 3), np.uint8)
    return V, F, Fcol


def _xform(V, rot_y=0.0, scale=1.0, trans=(0, 0, 0)):
    if len(V) == 0:
        return V
    c, s = math.cos(rot_y), math.sin(rot_y)
    Ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)
    return (V * scale) @ Ry.T + np.array(trans, np.float32)


@dataclass
class SceneObject:
    name: str
    mesh: object
    rot_y: float = 0.0
    scale: float = 1.0
    trans: tuple = (0, 0, 0)
    color: tuple | None = None         # override base colour (else mesh's)
    meta: dict = field(default_factory=dict)   # shown in the info dropdown


@dataclass
class Scene:
    objects: list = field(default_factory=list)

    def add(self, obj: SceneObject):
        self.objects.append(obj)

    def to_arrays(self, flat=False):
        """Flatten to (V, VN, F, Fcol, Fid) for core.raster.render.

        flat=True flat-shades (per-face normals), matching AC1's flat polygons and
        avoiding the cross-face smoothing of smooth_normals — which, on the level's
        dense coplanar instanced geometry, turns harmless coplanar z-fighting into
        visible salt-and-pepper. Use it for the level/map view; leave it off for
        AC/MT models, whose gouraud surfaces want the smooth normals."""
        Vs, Fs, Cs, Ids = [], [], [], []
        base = 0
        for oid, o in enumerate(self.objects):
            V, F, Fcol = mesh_arrays(o.mesh)
            if len(V) == 0:
                continue
            V = _xform(V, o.rot_y, o.scale, o.trans)
            if o.color is not None and len(Fcol):
                Fcol = np.tile(np.array(o.color, np.uint8), (len(Fcol), 1))
            Vs.append(V)
            Fs.append(F + base)
            Cs.append(Fcol)
            Ids.append(np.full(len(F), oid, np.int32))
            base += len(V)
        if not Vs:
            z = np.zeros((0, 3), np.float32)
            return z, z, np.zeros((0, 3), np.int32), np.zeros((0, 3), np.uint8), np.zeros((0,), np.int32)
        V = np.concatenate(Vs)
        F = np.concatenate(Fs)
        Fcol = np.concatenate(Cs)
        Fid = np.concatenate(Ids)
        if flat:
            return _flat_arrays(V, F, Fcol, Fid)
        VN = smooth_normals(V, F)
        return V, VN, F, Fcol, Fid
