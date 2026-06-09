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


def _pick_mesh(proj, fid, a, b, entry, want_all):
    """entry N -> that block; --all -> contact sheet; default -> largest block."""
    if entry is not None:
        ents = pa_parser.read_container(proj.bin_path, a, b)
        return pa_parser.parse_block(ents[entry])
    if want_all:
        return pa_parser.contact_sheet(proj.bin_path, a, b)
    blocks = pa_parser.parse_pa_blocks(proj.bin_path, a, b)
    big = pa_parser.largest_block(blocks)
    return big[1] if big else pa_parser.Mesh()


def cmd_obj(proj, idx, args):
    fid, a, b = find_pa(idx, args.file)
    mesh = _pick_mesh(proj, fid, a, b, args.entry, getattr(args, "all", False))
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
    mesh = _pick_mesh(proj, fid, a, b, args.entry, getattr(args, "all", False))
    out = args.out or f"/tmp/{os.path.basename(fid).replace('.', '_')}.png"
    img = render_mesh(mesh, args.w, args.h,
                      yaw=math.radians(args.yaw), pitch=math.radians(args.pitch),
                      zoom=args.zoom, wire=args.wire)
    img.save(out)
    print(f"wrote {out}  ({len(mesh.faces)} faces, view yaw={args.yaw} pitch={args.pitch})")


def main():
    ap = argparse.ArgumentParser(description="AC1mod headless CLI")
    ap.add_argument("--project", help="path to a .ac1mod project")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list PA files + notes")
    sub.add_parser("notes", help="dump all notes")
    p = sub.add_parser("info", help="geometry stats for a PA file"); p.add_argument("file")
    p = sub.add_parser("note", help="get/set a PA note")
    p.add_argument("action", choices=["get", "set"]); p.add_argument("file")
    p.add_argument("text", nargs="?", default="")
    p = sub.add_parser("obj", help="export OBJ (default: largest object)")
    p.add_argument("file")
    p.add_argument("--entry", type=int); p.add_argument("--all", action="store_true",
                   help="whole-file contact sheet instead of the largest object")
    p.add_argument("-o", "--out")
    p = sub.add_parser("render", help="render a PNG (default: largest object)")
    p.add_argument("file")
    p.add_argument("--entry", type=int); p.add_argument("--all", action="store_true",
                   help="whole-file contact-sheet grid instead of the largest object")
    p.add_argument("--yaw", type=float, default=35); p.add_argument("--pitch", type=float, default=25)
    p.add_argument("--zoom", type=float, default=1.0)
    p.add_argument("--w", type=int, default=640); p.add_argument("--h", type=int, default=460)
    p.add_argument("--wire", action="store_true"); p.add_argument("-o", "--out")

    args = ap.parse_args()
    _, proj, idx = load(args.project)
    {"list": cmd_list, "notes": cmd_notes, "info": cmd_info, "note": cmd_note,
     "obj": cmd_obj, "render": cmd_render}[args.cmd](proj, idx, args)


if __name__ == "__main__":
    main()
