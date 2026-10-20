#!/usr/bin/env python3
"""shadowraven build pipeline — extract -> patch -> repack -> reinject.

Turns YOUR legally-owned SLUS-01323 disc image + this mod's components into a
patched disc image, using the AC1_RE toolchain (pinned `ac1_re/` submodule).
No game data is bundled; everything is extracted from the disc you supply.

Stages
  0. filemap    build disc_map/disc_files.json from a jPSXdec .idx of your disc
  1. extract    dump the .T containers the mod touches (entries/NNN.bin)
  2. build      assemble mod components into per-entry payloads      [RE-GATED]
                  missions : chunk-4 scripts    (ac1_re mission_script.assemble)
                  assets   : TIM / PA / audio    entry replacements
                  patches  : compiled overlay code baked into FDAT-202
  3. repack     swap patched entries into each .T + fix checksums (t_repack API)
  4. reinject   write each rebuilt .T back into a copy of your disc   (reinject)

Stages 0/1/3/4 are wired to already-RE'd AC1_RE tools. Stage 2 builders raise
NotImplementedError until the AC1_RE de-risk (Goals 0/A/B — see docs/) confirms
the mechanism. With an empty mod (no component files), the pipeline is a no-op
that simply round-trips your disc — proving the spine end to end.

Prerequisites
  * $AC1_BIN (or --bin) -> your SLUS-01323 v1.1 .bin
  * a jPSXdec .idx of that disc (--idx)   [jPSXdec is the indexer]
  * for overlay patches: an mipsel GCC toolchain (see ac1_re/docs/REFERENCE.md §7)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent
AC1_RE = REPO / "ac1_re"
TOOLS = AC1_RE / "tools"
EXTRACT_T = TOOLS / "extract" / "extract_t.py"
BUILD_FILEMAP = TOOLS / "extract" / "build_filemap.py"
REINJECT = TOOLS / "disc" / "reinject.py"
COMPILE_OVERLAY = TOOLS / "ghidra" / "compile_overlay.sh"
# t_repack is imported (its Python API handles multi-entry repack + checksums).
sys.path.insert(0, str(TOOLS / "extract"))

TARGET_SERIAL = "SLUS-01323"
DATA_SIZE = 2048  # .T sector payload


@dataclass
class EntryPatch:
    """One replacement: entry `index` of container `file_id` becomes `payload`."""
    file_id: str        # filemap id, e.g. "GG/MS/MIS.T" or the FDAT container
    index: int          # entry index within that .T
    payload: bytes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], env: dict | None = None) -> None:
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, env=env)


def require_submodule() -> None:
    if not EXTRACT_T.exists():
        sys.exit("AC1_RE submodule missing — run: "
                 "git submodule update --init --recursive")


def component_files(mod_dir: Path, sub: str) -> list[Path]:
    """Real files under a component dir (ignore .gitkeep placeholders)."""
    d = mod_dir / sub
    return [p for p in sorted(d.rglob("*")) if p.is_file() and p.name != ".gitkeep"]


# ---------------------------------------------------------------------------
# stage 0 — filemap
# ---------------------------------------------------------------------------
def stage_filemap(idx: Path, env: dict) -> None:
    print("[0] filemap: building disc_map/disc_files.json from", idx.name)
    # build_filemap writes to tools/extract/disc_map/disc_files.json (the path the
    # extract/repack tools read). reinject resolves its own copy; keep them in sync
    # if a first real build reports a missing filemap for reinject.
    run([sys.executable, str(BUILD_FILEMAP), str(idx)], env=env)


# ---------------------------------------------------------------------------
# stage 1 — extract
# ---------------------------------------------------------------------------
def stage_extract(containers: set[str], work: Path, env: dict) -> dict[str, Path]:
    print(f"[1] extract: {len(containers)} container(s)")
    out = {}
    for fid in sorted(containers):
        dest = work / "extracted" / fid.replace("/", "_")
        run([sys.executable, str(EXTRACT_T), "--file", fid,
             "--dump-entries", "--out", str(dest)], env=env)
        out[fid] = dest
    return out


# ---------------------------------------------------------------------------
# stage 2 — build components into EntryPatches   [RE-GATED STUBS]
# ---------------------------------------------------------------------------
def build_missions(mod_dir: Path, extracted: dict[str, Path]) -> list[EntryPatch]:
    """Assemble mods .../missions/* into chunk-4 payloads and splice them into the
    owning mission's FDAT entry (2N+1).

    Wiring (once the authoring format lands): `import mission_script`;
    `blob = mission_script.parse(orig_chunk4)`; edit the Blob; `payload =
    mission_script.assemble(blob)`; reinsert chunk 4 into the 2N+1 chunk stream;
    return that stream as the EntryPatch payload. A from-scratch *source* format
    for missions does not exist yet (mission_script currently round-trips bytes)."""
    files = component_files(mod_dir, "missions")
    if not files:
        return []
    raise NotImplementedError(
        f"missions/: {len(files)} file(s) present, but mission authoring is gated "
        "on AC1_RE Goal A (scripted events) + a source format. See docs/.")


def build_assets(mod_dir: Path, extracted: dict[str, Path]) -> list[EntryPatch]:
    """Turn mods .../assets/* (TIM/PA/audio) into entry replacements for their .T.
    Each asset declares its target container + entry index (a sidecar manifest to
    be defined). Payload = the encoded entry bytes."""
    files = component_files(mod_dir, "assets")
    if not files:
        return []
    raise NotImplementedError(
        f"assets/: {len(files)} file(s) present, but asset mapping (container+entry "
        "index per asset) is not defined yet. See docs/.")


def build_patches(mod_dir: Path, extracted: dict[str, Path], env: dict) -> list[EntryPatch]:
    """Compile mods .../patch/*.c|.s to overlay blobs and bake them into the
    in-mission overlay (FDAT entry 202 @ 0x80050000).

    Per patch: `compile_overlay.sh <src> <load_addr> <entry> <out.bin> [defsym...]`
    -> a flat blob; splice it into the extracted overlay image at the hook offset
    (plus any trampoline), then return the whole overlay as the EntryPatch payload.
    The hook sites (perception gate, sentry think-handler, scripted-strike) are the
    RE-GATED part — Goals 0/A pin down where/how they attach."""
    files = [p for p in component_files(mod_dir, "patch") if p.suffix in (".c", ".s")]
    if not files:
        return []
    raise NotImplementedError(
        f"patch/: {len(files)} source(s) present, but overlay bake points are gated "
        "on AC1_RE Goal 0 (perception) / Goal A. compile_overlay.sh is ready; the "
        "hook offsets into FDAT-202 are not. See docs/.")


# ---------------------------------------------------------------------------
# stage 3 — repack (t_repack Python API: multi-entry + checksum)
# ---------------------------------------------------------------------------
def stage_repack(patches: list[EntryPatch], work: Path) -> dict[str, Path]:
    import t_repack  # from ac1_re/tools/extract (on sys.path)

    by_file: dict[str, list[EntryPatch]] = {}
    for p in patches:
        by_file.setdefault(p.file_id, []).append(p)

    print(f"[3] repack: {len(by_file)} container(s)")
    fm = t_repack.load_filemap()
    out = {}
    rebuilt = work / "rebuilt"
    rebuilt.mkdir(parents=True, exist_ok=True)
    for fid, group in by_file.items():
        rec = t_repack.find_file(fm, fid)
        if rec is None:
            sys.exit(f"file not in filemap: {fid}")
        container = t_repack.unpack(t_repack.read_flat_from_disc(fm, rec))
        for p in group:
            t_repack.replace_entry(container, p.index, p.payload)  # fixes checksum
        dest = rebuilt / (fid.replace("/", "_"))
        dest.write_bytes(t_repack.pack(container))
        out[fid] = dest
        print(f"    {fid}: {len(group)} entry(ies) -> {dest.name}")
    return out


# ---------------------------------------------------------------------------
# stage 4 — reinject (chained: each rebuilt .T into the running output image)
# ---------------------------------------------------------------------------
def stage_reinject(rebuilt: dict[str, Path], src_bin: Path, out_bin: Path,
                   env: dict) -> None:
    print(f"[4] reinject: {len(rebuilt)} container(s) -> {out_bin.name}")
    shutil.copy2(src_bin, out_bin)  # start from a copy of the user's disc
    for fid, flat_t in rebuilt.items():
        tmp = out_bin.with_suffix(".next.bin")
        run([sys.executable, str(REINJECT), "--file", fid, "--t", str(flat_t),
             "--in", str(out_bin), "--out", str(tmp), "--no-cue"], env=env)
        os.replace(tmp, out_bin)
    # write the .cue for the final image
    run([sys.executable, str(REINJECT), "--file", next(iter(rebuilt)),
         "--t", str(next(iter(rebuilt.values()))),
         "--in", str(src_bin), "--out", str(out_bin)], env=env)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Build the shadowraven AC1 mod patch.")
    ap.add_argument("--bin", type=Path, help="YOUR SLUS-01323 .bin (else $AC1_BIN)")
    ap.add_argument("--idx", type=Path, required=True,
                    help="jPSXdec .idx of that disc (for the filemap)")
    ap.add_argument("--mod", type=Path, default=REPO / "mods" / "shadowraven")
    ap.add_argument("--out", type=Path, default=REPO / "build_out")
    args = ap.parse_args()

    require_submodule()

    disc = args.bin or (Path(os.environ["AC1_BIN"]) if os.environ.get("AC1_BIN") else None)
    if not disc or not disc.exists():
        sys.exit("disc image not found: pass --bin or set AC1_BIN to your SLUS-01323 .bin")
    if not args.idx.exists():
        sys.exit(f"idx not found: {args.idx}")
    if not (args.mod / "mod.toml").exists():
        sys.exit(f"not a mod folder (no mod.toml): {args.mod}")

    env = dict(os.environ, AC1_BIN=str(disc))
    args.out.mkdir(parents=True, exist_ok=True)

    stage_filemap(args.idx, env)

    # Stage 2 decides which containers Stage 1 must extract. Builders run first to
    # declare their targets; extraction then feeds any that need source entries.
    # (With an empty mod, all builders return [] and the pipeline round-trips.)
    extracted: dict[str, Path] = {}
    patches: list[EntryPatch] = []
    patches += build_missions(args.mod, extracted)
    patches += build_assets(args.mod, extracted)
    patches += build_patches(args.mod, extracted, env)

    if not patches:
        print("[2] build: no components -> no-op pipeline (round-trip only)")
        print("Nothing to patch. The spine is wired; add content under mods/ "
              "once the AC1_RE de-risk (Goals 0/A/B) lands.")
        return 0

    containers = {p.file_id for p in patches}
    extracted = stage_extract(containers, args.out, env)
    rebuilt = stage_repack(patches, args.out)
    stage_reinject(rebuilt, disc, args.out / f"{args.mod.name}.{TARGET_SERIAL}.bin", env)
    print("done ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
