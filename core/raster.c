/* core/raster.c — AC1mod software rasterizer hot loop (z-buffered, Gouraud).
 *
 * The per-pixel scan-conversion is the only expensive part of drawing an AC1
 * level (millions of covered pixels across ~37k triangles), so it lives here in
 * C. Python (core/raster.py) still does the O(Nv) vertex transform + lighting in
 * numpy and hands us flat screen-space arrays; we fill the RGB image, a parallel
 * object-id buffer (for click-picking) and a depth buffer.
 *
 * Built on demand by core/raster.py (cc -O3 -shared -fPIC -> libraster.so) and
 * called via ctypes. If no C compiler is present the Python side falls back to a
 * vectorized-numpy rasterizer, so this file is a speed upgrade, not a hard dep.
 */
#include <math.h>
#include <float.h>

/* Rasterize Nf triangles into img/idbuf/zbuf (caller pre-fills bg/-1/+INF).
 *   w,h            : framebuffer size
 *   sx,sy,depth    : per-vertex screen X/Y and view-space depth (Nv floats)
 *   inten          : per-vertex light intensity 0..1 (Nv floats)
 *   F              : Nf*3 vertex indices (int)
 *   Fcol           : Nf*3 base RGB (uint8)
 *   Fid            : Nf object ids (int, for picking)
 *   cull           : 1 = keep one winding (area<0), 0 = two-sided
 *   img            : h*w*3 uint8 out
 *   idbuf          : h*w int out
 *   zbuf           : h*w float scratch (nearest depth per pixel)
 */
void rasterize(int w, int h,
               const float *sx, const float *sy, const float *depth,
               const float *inten,
               const int *F, const unsigned char *Fcol, const int *Fid,
               int Nf, int cull,
               unsigned char *img, int *idbuf, float *zbuf)
{
    for (int t = 0; t < Nf; ++t) {
        const int ia = F[t * 3 + 0], ib = F[t * 3 + 1], ic = F[t * 3 + 2];
        const float x0 = sx[ia], y0 = sy[ia];
        const float x1 = sx[ib], y1 = sy[ib];
        const float x2 = sx[ic], y2 = sy[ic];

        const float area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0);
        if (area == 0.0f) continue;
        if (cull && area >= 0.0f) continue;           /* back-face */
        const float inv = 1.0f / area;

        /* clipped integer bbox */
        float fminx = x0 < x1 ? x0 : x1; if (x2 < fminx) fminx = x2;
        float fmaxx = x0 > x1 ? x0 : x1; if (x2 > fmaxx) fmaxx = x2;
        float fminy = y0 < y1 ? y0 : y1; if (y2 < fminy) fminy = y2;
        float fmaxy = y0 > y1 ? y0 : y1; if (y2 > fmaxy) fmaxy = y2;
        int minx = (int)floorf(fminx), maxx = (int)ceilf(fmaxx);
        int miny = (int)floorf(fminy), maxy = (int)ceilf(fmaxy);
        if (minx < 0) minx = 0; if (maxx > w - 1) maxx = w - 1;
        if (miny < 0) miny = 0; if (maxy > h - 1) maxy = h - 1;
        if (minx > maxx || miny > maxy) continue;

        const float z0 = depth[ia], z1 = depth[ib], z2 = depth[ic];
        const float i0 = inten[ia], i1 = inten[ib], i2 = inten[ic];
        const float cr = (float)Fcol[t * 3 + 0];
        const float cg = (float)Fcol[t * 3 + 1];
        const float cb = (float)Fcol[t * 3 + 2];
        const int   fid = Fid[t];

        for (int py = miny; py <= maxy; ++py) {
            const float fy = (float)py + 0.5f;
            int row = py * w;
            for (int px = minx; px <= maxx; ++px) {
                const float fx = (float)px + 0.5f;
                /* edge functions (e0->v0, e1->v1, e2->v2); same sign as area = inside */
                const float e0 = (x2 - x1) * (fy - y1) - (y2 - y1) * (fx - x1);
                const float e1 = (x0 - x2) * (fy - y2) - (y0 - y2) * (fx - x2);
                const float e2 = (x1 - x0) * (fy - y0) - (y1 - y0) * (fx - x0);
                const float l0 = e0 * inv, l1 = e1 * inv, l2 = e2 * inv;
                if (l0 < 0.0f || l1 < 0.0f || l2 < 0.0f) continue;

                const float z = l0 * z0 + l1 * z1 + l2 * z2;
                const int idx = row + px;
                if (z >= zbuf[idx]) continue;
                zbuf[idx] = z;

                float shade = l0 * i0 + l1 * i1 + l2 * i2;
                float r = cr * shade, g = cg * shade, b = cb * shade;
                if (r > 255.0f) r = 255.0f; if (g > 255.0f) g = 255.0f; if (b > 255.0f) b = 255.0f;
                unsigned char *p = img + idx * 3;
                p[0] = (unsigned char)r; p[1] = (unsigned char)g; p[2] = (unsigned char)b;
                idbuf[idx] = fid;
            }
        }
    }
}
