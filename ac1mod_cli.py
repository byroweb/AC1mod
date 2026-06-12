#!/usr/bin/env python3
"""
ac1mod_cli.py — headless terminal interface to an AC1mod project.

Lets you (and Claude, over a terminal) browse the AC1 PA##.T 3D files, read/write
the per-file text notes stored in the .ac1mod project, export meshes to OBJ, and
render PNG previews — all without the GUI. Reuses core/pa_parser + core/render.

Project resolution order: --project PATH  >  ./recent_project.txt  >  a single
*.ac1mod in the current directory.

Examples:
  python3 ac1mod_cli.py list
  python3 ac1mod_cli.py info GG/P0/PA00.T
  python3 ac1mod_cli.py note set GG/P0/PA00.T "PA00 = light MT enemy, X-symmetric"
  python3 ac1mod_cli.py note get GG/P0/PA00.T
  python3 ac1mod_cli.py notes
  python3 ac1mod_cli.py obj GG/P0/PA00.T --entry 2 -o /tmp/pa00_e2.obj
  python3 ac1mod_cli.py render GG/P0/PA00.T -o /tmp/pa00.png        # whole-file contact sheet
  python3 ac1mod_cli.py render GG/P0/PA00.T --entry 2 --yaw 40 --pitch 25 -o /tmp/pa00_e2.png
"""
import os, sys, re, argparse
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
from core.project import Project          # noqa: E402
from core import pa_parser                 # noqa: E402

PA_RE = re.compile(r"/P[0-3]/PA\d{2}\.T$")


# ----------------------------------------------------------------- project ----

def resolve_project(arg):
    if arg:
        return Path(arg)
    rp = APP_DIR / "recent_project.txt"
    if rp.exists():
        p = Path(rp.read_text().strip())
        if p.exists():
            return p
    cand = list(APP_DIR.glob("*.ac1mod"))
    if len(cand) == 1:
        return cand[0]
    sys.exit("no project found — pass --project PATH (a .ac1mod file)")


def load(arg):
    path = resolve_project(arg)
    proj = Project.load(path)
    if not proj.bin_path or not Path(proj.bin_path).exists():
        sys.exit(f"project bin not found: {proj.bin_path}")
    idx = proj.index_path if proj.index_path and Path(proj.index_path).exists() \
        else (APP_DIR / "jpsxdec.idx")
    if not Path(idx).exists():
        sys.exit(f"index not found: {idx}")
    return path, proj, Path(idx)


def pa_files(index_path):
    """[(file_id, sector_start, sector_end)] for every PA##.T, in disc order."""
    out = []
    for line in Path(index_path).read_text(errors="replace").splitlines():
        if "|Type:File|" not in line or "/PA" not in line:
            continue
        fid = next((f[3:] for f in line.split("|") if f.startswith("ID:")), "")
        if not PA_RE.search(fid):
            continue
        m = re.search(r"Sectors:(\d+)-(\d+)", line)
        if m:
            out.append((fid, int(m.group(1)), int(m.group(2))))
    return out


def find_pa(index_path, file_id):
    fid = file_id if file_id.startswith("GG/") else None
    for (f, a, b) in pa_files(index_path):
        if f == file_id or f.endswith("/" + file_id) or os.path.basename(f) == file_id:
            return f, a, b
    sys.exit(f"PA file not found in index: {file_id}")


# ------------------------------------------------------------------ commands ---

def cmd_list(proj, idx, args):
    rows = pa_files(idx)
    print(f"{len(rows)} PA##.T files in {Path(proj.bin_path).name}\n")
    for (fid, a, b) in rows:
        note = proj.get_annotation(fid)
        flag = "*" if note else " "
        print(f" {flag} {fid:18s} sec {a}-{b}" + (f"   {note}" if note else ""))
    n = sum(1 for (f, _, _) in rows if proj.get_annotation(f))
    print(f"\n{n}/{len(rows)} annotated.  (* = has note)")


def cmd_info(proj, idx, args):
    fid, a, b = find_pa(idx, args.file)
    blocks = pa_parser.parse_pa_blocks(proj.bin_path, a, b)
    print(f"{fid}  sectors {a}-{b}  ({len(blocks)} geometry block(s))")
    note = proj.get_annotation(fid)
    if note:
        print(f"  note: {note}")
    tv = tf = 0
    for (ei, m) in blocks:
        bb = m.bbox(); st = m.stats(); tv += st["verts"]; tf += st["faces"]
        size = f"{bb[3]-bb[0]}x{bb[4]-bb[1]}x{bb[5]-bb[2]}" if bb else "-"
        print(f"  entry {ei:3d}: {st['verts']:4d} v  {st['faces']:4d} f  "
              f"{st['groups']:2d} sub  bbox {size}")
    print(f"  TOTAL: {tv} verts, {tf} faces")


def cmd_note(proj, idx, args):
    fid, _, _ = find_pa(idx, args.file)
    if args.action == "get":
        print(proj.get_annotation(fid) or "(no note)")
    else:
        proj.set_annotation(fid, args.text)
        proj.save(proj.project_path)
        print(f"saved note for {fid}")


def cmd_notes(proj, idx, args):
    if not proj.pa_annotations:
        print("(no notes yet)"); return
    for fid, txt in sorted(proj.pa_annotations.items()):
        print(f"{fid}: {txt}")


def _pick_mesh(proj, fid, a, b, entry, want_all, want_scene=False):
    """entry N -> that block; --scene -> assembled stage; --all -> contact sheet;
    default -> largest block."""
    if entry is not None:
        ents = pa_parser.read_container(proj.bin_path, a, b)
        return pa_parser.parse_block(ents[entry])
    if want_scene:
        return pa_parser.scene_mesh(proj.bin_path, a, b)
    if want_all:
        return pa_parser.contact_sheet(proj.bin_path, a, b)
    blocks = pa_parser.parse_pa_blocks(proj.bin_path, a, b)
    big = pa_parser.largest_block(blocks)
    return big[1] if big else pa_parser.Mesh()


def cmd_obj(proj, idx, args):
    fid, a, b = find_pa(idx, args.file)
    mesh = _pick_mesh(proj, fid, a, b, args.entry, getattr(args,"all",False), getattr(args,"scene",False))
    out = args.out or f"/tmp/{os.path.basename(fid).replace('.', '_')}.obj"
    lines = ["# AC1mod OBJ export", f"# {fid}"]
    for v in mesh.vertices:
        lines.append(f"v {v[0]} {v[1]} {v[2]}")
    for fc in mesh.faces:
        lines.append("f " + " ".join(str(i + 1) for i in fc.verts))
    Path(out).write_text("\n".join(lines) + "\n")
    print(f"wrote {out}  ({len(mesh.vertices)} verts, {len(mesh.faces)} faces)")


def cmd_render(proj, idx, args):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from core.render import render_mesh
    import math
    fid, a, b = find_pa(idx, args.file)
    mesh = _pick_mesh(proj, fid, a, b, args.entry, getattr(args,"all",False), getattr(args,"scene",False))
    out = args.out or f"/tmp/{os.path.basename(fid).replace('.', '_')}.png"
    img = render_mesh(mesh, args.w, args.h,
                      yaw=math.radians(args.yaw), pitch=math.radians(args.pitch),
                      zoom=args.zoom, wire=args.wire)
    img.save(out)
    print(f"wrote {out}  ({len(mesh.faces)} faces, view yaw={args.yaw} pitch={args.pitch})")


def cmd_level(proj, idx, args):
    """Render / export a mission's ASSEMBLED walkable level (real placement)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import math
    from core.level import level_mesh, level_obj_lines
    n = args.mission
    ceilings = not args.no_ceiling
    if args.obj:
        lines, nv, counts = level_obj_lines(proj.bin_path, n, str(idx))
        Path(args.obj).write_text("\n".join(lines) + "\n")
        print(f"wrote {args.obj}  ({nv} verts, {counts})")
    mesh = level_mesh(proj.bin_path, n, str(idx), ceilings=ceilings)
    if not mesh.vertices:
        sys.exit(f"mission {n}: no chunk-7 placement table (shared-scene mission?)")
    if args.spawns:
        from PyQt6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from core.mission import mission_scene
        from core import raster
        sc, sp = mission_scene(proj.bin_path, n, str(idx),
                               level=True, ceilings=ceilings)
        V, VN, F, Fcol, Fid = sc.to_arrays()
        img, _ = raster.render(V, VN, F, Fcol, Fid, args.w, args.h,
                               yaw=math.radians(args.yaw), pitch=math.radians(args.pitch),
                               zoom=args.zoom)
    else:
        from core.render import render_mesh
        img = render_mesh(mesh, args.w, args.h, yaw=math.radians(args.yaw),
                          pitch=math.radians(args.pitch), zoom=args.zoom,
                          wire=args.wire)
    out = args.out or f"/tmp/ac1_level_{n:02d}.png"
    img.save(out)
    print(f"wrote {out}  (mission {n}, {len(mesh.groups)} sections, "
          f"{len(mesh.faces)} faces, ceilings={'on' if ceilings else 'OFF'})")


def cmd_missions(proj, idx, args):
    """Batch-render every mission's populated 3D scene to PNGs."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import math
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from core.mission import mission_scene, mission_names
    from core import raster
    out_dir = args.out_dir or "/tmp/ac1_missions"
    os.makedirs(out_dir, exist_ok=True)
    names = mission_names(proj.bin_path, str(idx))
    done = 0
    for n in range(args.first, args.last + 1):
        try:
            sc, sp = mission_scene(proj.bin_path, n, str(idx),
                                   level=getattr(args, "level", False),
                                   ceilings=not getattr(args, "no_ceiling", False))
        except Exception as ex:
            print(f"  mission {n}: error {ex}"); continue
        if not sp and not sc.objects:
            continue
        V, VN, F, Fcol, Fid = sc.to_arrays()
        img, _ = raster.render(V, VN, F, Fcol, Fid, args.w, args.h,
                               yaw=math.radians(args.yaw), pitch=math.radians(args.pitch),
                               zoom=args.zoom)
        nm = names.get(n, "").replace("/", "-").strip()
        path = os.path.join(out_dir, f"mission_{n:02d}.png")
        img.save(path)
        print(f"  mission {n:02d}  {len(sp):3d} objects  {nm}  -> {path}")
        done += 1
    print(f"rendered {done} missions to {out_dir}")


def cmd_mistim(proj, idx, args):
    """Extract every embedded MIS.T mission-preview TIM (all ~194), optionally to PNG."""
    from core import mis
    out_dir = args.out_dir or "/tmp/ac1_mis_tims"
    paths = mis.extract_tims(proj.bin_path, out_dir)
    print(f"extracted {len(paths)} MIS.T TIMs -> {out_dir}")
    if args.png:
        from core.jpsxdec import decode_tim_to_rgba
        from PIL import Image
        n = 0
        for i, p in enumerate(paths):
            r = decode_tim_to_rgba(p, 0)
            if r:
                w, h, rgba = r
                Image.frombytes("RGBA", (w, h), rgba).save(os.path.join(out_dir, f"MIS_{i:03d}.png"))
                n += 1
        print(f"decoded {n} previews to PNG in {out_dir}")


DEFAULT_CARD = ("/home/byron/.local/share/duckstation/memcards/"
                "Armored Core (USA) (Reprint)_1.mcd")


def cmd_memcard(proj, idx, args):
    """Browse a PS1 memory card; view/export/import AC1 emblems."""
    from core import memcard as M
    from PIL import Image
    if args.pix_off is None:
        args.pix_off = M.EMBLEM_PIX_OFF
    card = M.read_card(args.card or DEFAULT_CARD)
    if args.action == "list":
        print(f"{Path(args.card or DEFAULT_CARD).name}: {len(card.saves)} save(s)")
        for s in card.saves:
            tag = "AC1" if s.is_ac1 else "   "
            blank = ""
            if s.is_ac1:
                blank = " (emblem: blank)" if M.is_emblem_blank(card.block_bytes(s.slot)) else " (emblem: drawn)"
            print(f"  [{tag}] slot{s.slot} {s.code:14s} {s.label}{blank}")
        return
    # actions below need an AC1 save
    sv = next((s for s in card.saves if s.is_ac1 and (args.slot is None or s.slot == args.slot)), None)
    if not sv:
        sys.exit("no AC1 save found on card")
    blk = card.block_bytes(sv.slot)
    if args.action == "icon":
        w, h, rgba = card.icon_rgba(sv)
        out = args.out or "/tmp/ac1_save_icon.png"
        Image.frombytes("RGBA", (w, h), rgba).resize((128, 128), Image.NEAREST).save(out)
        print("wrote", out)
    elif args.action == "emblem-export":
        w, h, rgba = M.decode_emblem(blk, args.pix_off)
        out = args.out or "/tmp/ac1_emblem.png"
        Image.frombytes("RGBA", (w, h), rgba).resize((256, 256), Image.NEAREST).save(out)
        print(f"wrote {out}  (blank={M.is_emblem_blank(blk, args.pix_off)})")
    elif args.action == "emblem-import":
        if not args.image:
            sys.exit("emblem-import needs --image PATH")
        data = M.encode_emblem(args.image, blk)
        card.patch(sv.slot, args.pix_off, data)
        card.save(args.out)         # writes back to card (or --out copy)
        print(f"imported {args.image} -> emblem of slot{sv.slot} "
              f"({'in place' if not args.out else args.out})")


def main():
    ap = argparse.ArgumentParser(description="AC1mod headless CLI")
    ap.add_argument("--project", help="path to a .ac1mod project")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list PA files + notes")
    sub.add_parser("notes", help="dump all notes")
    p = sub.add_parser("mistim", help="extract all MIS.T mission-preview TIMs")
    p.add_argument("--out-dir"); p.add_argument("--png", action="store_true",
                   help="also decode each preview to PNG")
    p = sub.add_parser("memcard", help="browse PS1 card; view/import/export AC1 emblem")
    p.add_argument("action", choices=["list", "icon", "emblem-export", "emblem-import"])
    p.add_argument("--card", help="path to .mcd/.mcr card (default: DuckStation AC1 card)")
    p.add_argument("--slot", type=int, help="directory slot (default: first AC1 save)")
    p.add_argument("--image", help="GIF/PNG to import (emblem-import)")
    p.add_argument("--pix-off", type=lambda s: int(s, 0), default=None,
                   help="emblem pixel offset override (DuckStation-confirmed)")
    p.add_argument("-o", "--out", help="output file / write card copy here instead of in place")
    p = sub.add_parser("missions", help="batch-render every mission's populated scene")
    p.add_argument("--out-dir"); p.add_argument("--first", type=int, default=0)
    p.add_argument("--last", type=int, default=49)
    p.add_argument("--yaw", type=float, default=30); p.add_argument("--pitch", type=float, default=42)
    p.add_argument("--zoom", type=float, default=1.4)
    p.add_argument("--w", type=int, default=900); p.add_argument("--h", type=int, default=700)
    p.add_argument("--level", action="store_true",
                   help="include the assembled walkable level under the spawns")
    p.add_argument("--no-ceiling", action="store_true",
                   help="drop ceiling faces (see into indoor levels)")
    p = sub.add_parser("level", help="render/export a mission's assembled walkable level")
    p.add_argument("mission", type=int)
    p.add_argument("--no-ceiling", action="store_true",
                   help="drop ceiling faces (see into indoor levels)")
    p.add_argument("--spawns", action="store_true", help="overlay spawn markers")
    p.add_argument("--obj", help="also export OBJ (o floor/ceiling/wall groups)")
    p.add_argument("--yaw", type=float, default=30); p.add_argument("--pitch", type=float, default=55)
    p.add_argument("--zoom", type=float, default=1.0)
    p.add_argument("--w", type=int, default=1000); p.add_argument("--h", type=int, default=800)
    p.add_argument("--wire", action="store_true"); p.add_argument("-o", "--out")
    p = sub.add_parser("info", help="geometry stats for a PA file"); p.add_argument("file")
    p = sub.add_parser("note", help="get/set a PA note")
    p.add_argument("action", choices=["get", "set"]); p.add_argument("file")
    p.add_argument("text", nargs="?", default="")
    p = sub.add_parser("obj", help="export OBJ (default: largest object)")
    p.add_argument("file")
    p.add_argument("--entry", type=int); p.add_argument("--all", action="store_true",
                   help="whole-file contact sheet instead of the largest object")
    p.add_argument("--scene", action="store_true", help="assembled stage (world coords)")
    p.add_argument("-o", "--out")
    p = sub.add_parser("render", help="render a PNG (default: largest object)")
    p.add_argument("file")
    p.add_argument("--entry", type=int); p.add_argument("--all", action="store_true",
                   help="whole-file contact-sheet grid instead of the largest object")
    p.add_argument("--scene", action="store_true", help="assembled stage (world coords)")
    p.add_argument("--yaw", type=float, default=35); p.add_argument("--pitch", type=float, default=25)
    p.add_argument("--zoom", type=float, default=1.0)
    p.add_argument("--w", type=int, default=640); p.add_argument("--h", type=int, default=460)
    p.add_argument("--wire", action="store_true"); p.add_argument("-o", "--out")

    args = ap.parse_args()
    _, proj, idx = load(args.project)
    {"list": cmd_list, "notes": cmd_notes, "info": cmd_info, "note": cmd_note,
     "obj": cmd_obj, "render": cmd_render, "missions": cmd_missions,
     "level": cmd_level,
     "mistim": cmd_mistim, "memcard": cmd_memcard}[args.cmd](proj, idx, args)


if __name__ == "__main__":
    main()
