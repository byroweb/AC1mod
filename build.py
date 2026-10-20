#!/usr/bin/env python3
"""shadowraven build pipeline (STUB).

Produces a patched disc image from YOUR legally-owned SLUS-01323 copy plus this
mod's components. No game data is bundled; everything is extracted from the disc
you supply, patched, and repacked locally.

Pipeline (to be implemented, using the pinned AC1_RE submodule toolchain):

    1. extract   your BIN  -> work dir        (ac1_re/tools/extract/)
    2. patch     overlay FDAT 202 with compiled patch/*.c|.s  (needs mipsel toolchain)
                 + mission scripts (ac1_re/tools/mission/mission_script.py)
                 + assets (TIM/PA/audio)
    3. repack    the .T container + checksum   (ac1_re/tools/extract/t_repack.py)
    4. reinject  back into a copy of your BIN  (ac1_re/tools/disc/reinject.py)

Authoring of the components is gated on the AC1_RE de-risk (Goals 0/A/B) — see docs/.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
AC1_RE = REPO / "ac1_re"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the shadowraven AC1 mod patch.")
    ap.add_argument("--bin", required=True, type=Path,
                    help="path to YOUR SLUS-01323 disc image (never committed)")
    ap.add_argument("--mod", default=REPO / "mods" / "shadowraven", type=Path,
                    help="mod folder to build")
    ap.add_argument("--out", default=REPO / "build_out", type=Path,
                    help="output directory for the patched image")
    args = ap.parse_args()

    if not (AC1_RE / "tools").is_dir():
        sys.exit("AC1_RE submodule missing — run: git submodule update --init --recursive")
    if not args.bin.exists():
        sys.exit(f"disc image not found: {args.bin}")

    raise SystemExit(
        "build pipeline not implemented yet — the mod's core systems are blocked on "
        "the AC1_RE de-risk (Goals 0/A/B). See docs/ and mod.toml [systems]."
    )


if __name__ == "__main__":
    sys.exit(main())
