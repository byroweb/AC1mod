/* core/xa.c — CD-ROM XA-ADPCM decode hot loop for AC1mod.
 *
 * The ADPCM reconstruction is a serial IIR (each sample depends on the previous
 * two, with filter/shift changing every 28-sample sound unit and the predictor
 * state chaining for the whole channel), so it doesn't vectorise in numpy. The
 * Python side (core/xa.py) does the vectorised part — reading the disc image and
 * selecting this stream's audio sectors — then hands us the concatenated 128-byte
 * sound groups; we emit interleaved int16 PCM.
 *
 * Algorithm: per 128-byte group, 16-byte header then 28×4 data bytes. For each of
 * the 4 byte-columns, the low nibble feeds the left channel and the high nibble
 * the right (mono: low then high are consecutive sound units). Params live at
 * header bytes 4..11 (in[4+i*2] = low/left, in[5+i*2] = high/right).
 *
 * Predictor is kept in double precision to match jPSXdec bit-for-bit (verified
 * against its WAV output): sp = (t<<shift) + (s1*f0 + s2*f1)/64. The emitted
 * sample is rint(clip(sp)) but the *feedback* history is the UN-clamped sp — at
 * saturation jPSXdec keeps predicting past the rail (clamping the feedback drifts
 * by up to ~3500 LSB on loud voice clips). The integer (...+32)>>6 path also
 * drifts (~18 LSB), so both rounding and feedback must stay in float.
 *
 * Built on demand by core/xa.py (cc -O3 -shared -fPIC -> libxa.so) and called via
 * ctypes; a pure-Python fallback in xa.py covers the no-compiler case.
 */
#include <math.h>

static const int XA_TAB[5][2] = {
    {0, 0}, {60, 0}, {115, -52}, {98, -55}, {122, -60}
};

static inline double clipf(double s) {
    if (s > 32767.0) return 32767.0;
    if (s < -32768.0) return -32768.0;
    return s;
}

static inline int sx4(int n) {        /* sign-extend low 4 bits */
    n &= 0xF;
    return n >= 8 ? n - 16 : n;
}

/* Decode n_groups XA sound groups (128 bytes each) into interleaved int16 PCM.
 *   in        : n_groups*128 bytes of concatenated sound groups
 *   channels  : 1 (mono) or 2 (stereo)
 *   out       : caller-allocated; stereo -> n_groups*112 frames (*2 shorts),
 *               mono -> n_groups*224 shorts.
 * Predictor state carries across all groups, as XA requires.
 */
void xa_decode(const unsigned char *in, int n_groups, int channels, short *out)
{
    double s1l = 0, s2l = 0, s1r = 0, s2r = 0;

    for (int g = 0; g < n_groups; ++g) {
        const unsigned char *hdr = in + g * 128;
        const unsigned char *dat = hdr + 16;
        short *o = out + (long)g * (channels == 2 ? 112 * 2 : 224);

        for (int i = 0; i < 4; ++i) {
            int p0 = hdr[4 + i * 2];
            int sh0 = 12 - (p0 & 0x0F);
            int fl0 = (p0 >> 4) & 0x0F; if (fl0 > 4) fl0 = 0;
            double f0a = XA_TAB[fl0][0], f1a = XA_TAB[fl0][1];

            int p1 = hdr[5 + i * 2];
            int sh1 = 12 - (p1 & 0x0F);
            int fl1 = (p1 >> 4) & 0x0F; if (fl1 > 4) fl1 = 0;
            double f0b = XA_TAB[fl1][0], f1b = XA_TAB[fl1][1];

            if (channels == 2) {
                /* low nibble -> left */
                double s1 = s1l, s2 = s2l;
                for (int j = 0; j < 28; ++j) {
                    int t = sx4(dat[i + j * 4]);
                    double sp = (double)(t << sh0) + (s1 * f0a + s2 * f1a) / 64.0;
                    s2 = s1; s1 = sp;            /* feed back UNCLAMPED prediction */
                    o[(i * 28 + j) * 2] = (short)rint(clipf(sp));
                }
                s1l = s1; s2l = s2;
                /* high nibble -> right */
                s1 = s1r; s2 = s2r;
                for (int j = 0; j < 28; ++j) {
                    int t = sx4(dat[i + j * 4] >> 4);
                    double sp = (double)(t << sh1) + (s1 * f0b + s2 * f1b) / 64.0;
                    s2 = s1; s1 = sp;
                    o[(i * 28 + j) * 2 + 1] = (short)rint(clipf(sp));
                }
                s1r = s1; s2r = s2;
            } else {
                /* mono: low nibble then high nibble are consecutive sound units */
                double s1 = s1l, s2 = s2l;
                for (int j = 0; j < 28; ++j) {
                    int t = sx4(dat[i + j * 4]);
                    double sp = (double)(t << sh0) + (s1 * f0a + s2 * f1a) / 64.0;
                    s2 = s1; s1 = sp;
                    o[i * 56 + j] = (short)rint(clipf(sp));
                }
                for (int j = 0; j < 28; ++j) {
                    int t = sx4(dat[i + j * 4] >> 4);
                    double sp = (double)(t << sh1) + (s1 * f0b + s2 * f1b) / 64.0;
                    s2 = s1; s1 = sp;
                    o[i * 56 + 28 + j] = (short)rint(clipf(sp));
                }
                s1l = s1; s2l = s2;
            }
        }
    }
}
